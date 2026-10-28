"""Weekly evidence refresh: Saturday 10:00 ET, window = 2022-01-13 .. last Friday, holds the heavy-job slot,
keeps the S1/S2 minute-replay results, and never runs in a trading session."""
from datetime import date, datetime
from types import SimpleNamespace

from app.market_clock import us_clock
from app.services import evaluation_schedule as es


def test_window_ends_one_week_back_for_complete_7_day_exits():
    start, end = es.window(date(2026, 10, 3))
    assert start == date(2022, 1, 13) and end == date(2026, 9, 25)


def test_run_uses_runner_with_heavy_slot_and_keeps_replay(tmp_path, monkeypatch):
    calls = {}

    def fake_run_all(data_dir, start, end, **kw):
        calls.update(start=start, end=end, **kw)
        return {"sources": {"bullish_alignment": {"n": 10}}, "evaluated": ["bullish_alignment"], "elapsed_s": 1.0}

    monkeypatch.setattr(es.runner, "run_all", fake_run_all)
    monkeypatch.setattr(es, "run_v2", lambda *a, **k: {"status": "stubbed"})
    monkeypatch.setattr(es, "run_v3", lambda *a, **k: {"status": "stubbed"})
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 3, 10, 0, tzinfo=us_clock.tz))
    out = es.run(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None))
    assert out["status"] == "succeeded" and out["evaluated"] == 1
    assert calls["end"] == date(2026, 9, 25)
    assert "replay_trades" not in calls or calls["replay_trades"] is None  # S1/S2 replay results stay as already stored
    assert (tmp_path / "decision_desk" / "evaluation_runs.jsonl").exists()


def test_refuses_during_session(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 2, 11, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(us_clock, "in_session", lambda *a: True)
    out = es.run(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None))
    assert out["status"] == "skipped" and "交易时段" in out["reason"]


def test_v3_degrades_to_production_data_when_research_data_unavailable(tmp_path, monkeypatch):
    """研究数据 (退市股/事件) 拉不到 → 用生产数据评估并标注 with_delisted=False; 不抛、不影响 v1/v2。"""
    from app.services import strategy_evaluation_v3 as v3
    seen = {}

    def fake_run_v3(data_dir, start, end, **kw):
        seen.update(data_dir=data_dir, **kw)
        out = kw["out_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sources": {"x": {"tier_v3": "negative", "excess": {}}}, "evaluated": ["x"]}
        out.write_text(__import__("json").dumps(payload))
        return payload

    monkeypatch.setattr(v3, "run_v3", fake_run_v3)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 3, 10, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(es, "_available_memory_mb", lambda: None)

    def in_process(eval_dir, start, end, ev_dir, out_path, hyp_dir):   # 测试里不起子进程, 直接同进程调用
        p = v3.run_v3(eval_dir, start, end, engine=None, earn_dir=ev_dir / "earnings", sec_dir=ev_dir / "sec",
                      out_path=out_path, hypothesis_dir=hyp_dir)
        return {"evaluated": p["evaluated"], "sources": p["sources"]}
    monkeypatch.setattr(es, "_run_v3_isolated", in_process)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None)
    out = es.run_v3(state)
    assert out["status"] == "succeeded" and out["with_delisted"] is False
    assert seen["data_dir"] == tmp_path                          # 退回生产数据目录
    saved = __import__("json").loads((tmp_path / "decision_desk" / "evaluation_v3.json").read_text())
    assert saved["with_delisted"] is False and saved["research"]["delisted"]["status"] in ("skipped", "failed")


def test_v3_skips_when_memory_is_tight_and_never_raises(tmp_path, monkeypatch):
    """NAS 只有 7.5G: 可用内存不足时不跑全市场矩阵 (保护交易进程), 结果记 failed + 原因, 不抛。"""
    from app.services import research_data
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 3, 10, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(es, "_available_memory_mb", lambda: 800)
    built = []
    monkeypatch.setattr(research_data, "build_v3_dataset", lambda d: built.append(1) or {"status": "ok"})
    called = []
    monkeypatch.setattr(es, "_run_v3_isolated", lambda *a, **k: called.append(1))
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None)
    out = es.run_v3(state, refresh=False)
    assert out["status"] == "failed" and "可用内存" in out["error"] and not called and not built   # 连数据集都不建


def test_v3_child_crash_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 3, 10, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(es, "_available_memory_mb", lambda: None)

    def crash(*a, **k):
        raise RuntimeError("v3 子进程异常退出 (exitcode=-9; -9 通常为内存不足被系统终止)")
    monkeypatch.setattr(es, "_run_v3_isolated", crash)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None)
    out = es.run_v3(state, refresh=False)
    assert out["status"] == "failed" and "-9" in out["error"] and es.v3_status()["running"] is False
