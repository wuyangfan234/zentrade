"""0.26.0 第④批 (10-18 产品决策“策略自主发现显然方案”): 挖掘候选按机会板卡回放验收, 赢过“4h 门内随便挑”才标候选。"""
import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import swing_candidates as sc


def test_account_matches_production_admission_order():
    # 同票持仓 -> 冷却 -> 仓位 -> 当日新开 -> 同板块
    picks = [(1, 3, "A", 0, 1.0, 9.0), (1, 3, "A", 0, 1.0, 8.0),       # 同票第二张: 挡
             (1, 2, "B", 0, 0.5, 7.0), (1, 2, "C", 0, 0.5, 6.0),
             (4, 6, "B", 3, 0.5, 5.0)]                                   # B 2 号平仓, 4 号再开: 冷却 3 天内 -> 挡
    out = sc.account(picks, max_pos=12, day_cap=10, sect_cap=4, yellow_cap=6, yellow={}, sectors={})
    assert [x["sym"] for x in out] == ["A", "B", "C"]
    sect = {s: "Tech" for s in "ABCDEF"}
    many = [(1, 5, s, 0, 0.1, 10 - i) for i, s in enumerate("ABCDEF")]
    assert len(sc.account(many, max_pos=12, day_cap=10, sect_cap=4, yellow_cap=6, yellow={}, sectors=sect)) == 4
    assert len(sc.account(many, max_pos=12, day_cap=3, sect_cap=9, yellow_cap=6, yellow={}, sectors=sect)) == 3
    assert len(sc.account(many, max_pos=12, day_cap=10, sect_cap=9, yellow_cap=2, yellow={1: True}, sectors=sect)) == 2


def test_judge_passes_only_when_ci_lower_above_zero():
    rng = np.random.default_rng(0)
    base = [[{"t": t, "r": float(rng.normal(0.1, 1.0))} for t in range(400)] for _ in range(5)]
    good = [{"t": t, "r": float(rng.normal(0.6, 1.0))} for t in range(400)]
    same = [{"t": t, "r": float(rng.normal(0.1, 1.0))} for t in range(400)]
    g = sc.judge(good, base, years=400 / 252)
    s = sc.judge(same, base, years=400 / 252)
    assert g["passed"] is True and g["excess_ci"][0] > 0
    assert s["passed"] is False
    assert sc.judge(good[:5], base, years=1.0)["passed"] is False          # 样本太少: 没有区间 -> 不过关


def test_candidate_ids_and_rules_api_tag(tmp_path):
    p = sc.candidates_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"contract": sc.CONTRACT, "rules": {
        "custom_a": {"passed": True, "excess": 0.2, "excess_ci": [0.05, 0.35], "n": 120, "r_per_year": 30.0,
                     "base_r_per_year": 20.0, "first_passed": "2026-10-23"},
        "custom_b": {"passed": False, "excess": 0.05, "excess_ci": [-0.1, 0.2], "n": 80},
        "custom_c": {"error": "no_watchlist_data"}}}))
    assert sc.candidate_ids(tmp_path) == {"custom_a"}


def test_discovered_ids_only_custom_and_ai_matrix():
    engine = SimpleNamespace(list_strategies=lambda: [
        {"id": "macd_golden", "source": "builtin", "execution_backend": "matrix_native"},
        {"id": "mined_x", "source": "custom", "execution_backend": "matrix_native"},
        {"id": "ai_y", "source": "ai", "execution_backend": "matrix_native"},
        {"id": "ai_draft", "source": "ai", "execution_backend": "matrix_native", "research_only": True},
        {"id": "ai_rows", "source": "ai", "execution_backend": "minute_filter"},
        {"id": "ai_expr", "source": "ai", "execution_backend": "polars_expr"},
        {"id": "etf_only", "source": "custom", "execution_backend": "matrix_native", "asset_types": ["etf"]},
    ])
    assert sc.discovered_ids(engine) == ["ai_expr", "ai_y", "mined_x"]


def test_run_after_mining_without_discovered_strategies_writes_note(tmp_path, monkeypatch):
    from app.services import strategy_evaluation_runner as runner
    monkeypatch.setattr(runner, "_strategy_engine", lambda d: SimpleNamespace(list_strategies=lambda: []))
    out = sc.run_after_mining(tmp_path, object(), end=date(2026, 10, 16))
    assert out["note"] == "no_discovered_strategies" and sc.read_candidates(tmp_path)["rules"] == {}


def test_evaluate_end_to_end_on_synthetic_market(tmp_path, monkeypatch):
    """合成 12 只票 x 60 天: 候选排序把后来真赚钱的 5 只排前面; 随机基线每天从 12 只里随便挑 5 只 -> 候选超额为正。
    (候选与基线都过同一套账户约束; 全流程只写 research/。)"""
    from app.services import strategy_evaluation_runner as runner
    from app.services import swing_settlement as ss
    days = [date(2026, 6, 1) + __import__("datetime").timedelta(days=i) for i in range(90)]
    days = [d for d in days if d.weekday() < 5][:60]
    syms = [f"S{i:02d}" for i in range(12)]
    n_days, n_syms = len(days), len(syms)
    close = np.full((n_days, n_syms), 100.0)
    md = SimpleNamespace(timestamp_labels=[d.isoformat() for d in days], symbols=syms, close=close,
                         high=close + 1.0, low=close - 1.0)
    entry = np.ones((n_days, n_syms), bool)
    score = np.tile(np.arange(12, 0, -1, dtype=float), (n_days, 1))           # 候选总是先挑 S00..S04
    monkeypatch.setattr(runner, "_strategy_engine", lambda d: SimpleNamespace(list_strategies=lambda: []))
    monkeypatch.setattr(sc, "_signals_any", lambda sid, svc, engine, s, e, syms_: (md, entry, score))
    monkeypatch.setattr(sc, "_gate_matrix", lambda data_dir, repo, symbols, days: np.ones((len(days), len(symbols)), bool))

    def fake_cells(data_dir, repo, symbols, days, close, atr, gate):
        out = {}
        for t in range(len(days) - 6):
            for j, _s in enumerate(symbols):
                out[(t, j)] = (t + 1, t + 2, 1.0 if j < 5 else -0.5)        # 前 5 只赚, 其他亏
        return out
    monkeypatch.setattr(sc, "_cells", fake_cells)
    monkeypatch.setattr(sc, "_spy_below_ma200", lambda repo, days: {})
    monkeypatch.setattr(sc, "_sectors", lambda repo: {})
    import app.backtest.engine as be
    import app.backtest.strategy as bs
    monkeypatch.setattr(be, "BacktestEngine", lambda repo: None)
    monkeypatch.setattr(bs, "StrategyBacktestService", lambda engine, se: None)
    out = sc.evaluate(tmp_path, object(), ["mined_x"], end=days[-1], watchlist=set(syms))
    r = out["rules"]["mined_x"]
    assert r["passed"] is True and r["excess"] > 0 and r["first_passed"] == days[-1].isoformat()
    assert sc.candidate_ids(tmp_path) == {"mined_x"}
    assert list((tmp_path).rglob("*")) == [p for p in tmp_path.rglob("*") if "research" in str(p)]
    _ = ss


def test_worker_task_kind_swing_candidates_encodes(tmp_path):
    from app.backtest.worker import make_worker_task
    t = make_worker_task("swing_candidates", tmp_path, {"strategy_ids": ["x"], "end": "2026-10-16", "watchlist": ["A"]})
    assert t["kind"] == "swing_candidates" and t["config"]["strategy_ids"] == ["x"]


def test_run_after_mining_uses_worker_and_reads_file(tmp_path, monkeypatch):
    """生产路径: 在子进程里跑 (不占主进程内存), 主进程只读结果文件。"""
    from app.backtest import worker as w
    from app.services import strategy_evaluation_runner as runner
    monkeypatch.setattr(runner, "_strategy_engine", lambda d: SimpleNamespace(list_strategies=lambda: [
        {"id": "mined_x", "source": "custom", "execution_backend": "matrix_native"}]))
    from app.services import decision_scan_job as job
    monkeypatch.setattr(job, "_watchlist", lambda: {"AAA"})
    seen = {}

    def fake_run(task, *a, **k):
        seen.update(task)
        p = sc.candidates_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"rules": {"mined_x": {"passed": True}}}))
        return {"rules": {"mined_x": {"passed": True}}}
    monkeypatch.setattr(w, "run_worker_task", fake_run)
    out = sc.run_after_mining(tmp_path, object(), end=date(2026, 10, 16))
    assert seen["kind"] == "swing_candidates" and seen["config"]["watchlist"] == ["AAA"]
    assert out["rules"]["mined_x"]["passed"] is True


def test_signals_any_matrix_runs_full_market_then_keeps_watchlist(monkeypatch):
    """自审 F2: 截面排序策略 (挖掘发布的因子排名, 分数 = 截面百分位、入场取前 20 名) 必须在全市场上算再取自选,
    只在几十只自选上算会换掉分母和名次。矩阵策略调用 _signals_for 时不传 symbols (= 全市场)。"""
    from app.services import strategy_evaluation_runner as runner
    seen = {}
    syms = ["AAA", "BBB", "CCC", "DDD"]
    md = SimpleNamespace(timestamp_labels=["2026-10-15", "2026-10-16"], symbols=syms,
                         close=np.arange(8.0).reshape(2, 4), high=np.arange(8.0).reshape(2, 4) + 1,
                         low=np.arange(8.0).reshape(2, 4) - 1)
    entry = np.array([[1, 0, 1, 0], [0, 1, 1, 1]], bool)
    score = np.array([[4.0, 3.0, 2.0, 1.0], [1.0, 2.0, 3.0, 4.0]])

    def fake(sid, svc, engine, start, end, *a, **k):
        seen["args"], seen["kw"] = a, k
        return md, entry, score, -0.08
    monkeypatch.setattr(runner, "_signals_for", fake)
    engine = SimpleNamespace(get=lambda sid: SimpleNamespace(execution_backend="matrix_native"))
    sub, e2, s2 = sc._signals_any("mined_factor_x", None, engine, date(2026, 10, 15), date(2026, 10, 16), ["CCC", "AAA"])
    assert seen["args"] == () and "symbols" not in seen["kw"]                 # 全市场
    assert sub.symbols == ["AAA", "CCC"] and e2.tolist() == [[True, True], [False, True]]
    assert s2.tolist() == [[4.0, 2.0], [1.0, 3.0]] and sub.close.shape == (2, 2)


def test_worker_run_builds_matrix_in_memory_without_enriched_rebuild(tmp_path, monkeypatch):
    """子进程配置与参考部署实测一致: 不写回测磁盘缓存, 不在子进程里重建日K指标缓存。"""
    from app.config import settings
    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", True)
    repo = SimpleNamespace(_enriched_warming=False)
    got = {}

    def fake_eval(data_dir, repo_, ids, *, end, watchlist):
        got.update(disk=settings.backtest_matrix_disk_cache_enabled, warming=repo_._enriched_warming, ids=ids,
                   end=end, watchlist=watchlist)
        return {"rules": {"x": {"passed": False, "n": 3, "excess": 0.1, "excess_ci": [-1, 1]}}}
    monkeypatch.setattr(sc, "evaluate", fake_eval)
    out = sc.worker_run(tmp_path, repo, {"strategy_ids": ["x"], "end": "2026-10-16", "watchlist": ["A"]})
    assert got == {"disk": False, "warming": True, "ids": ["x"], "end": date(2026, 10, 16), "watchlist": {"A"}}
    assert out == {"rules": {"x": {"passed": False, "n": 3, "excess": 0.1}}}


@pytest.mark.real_swing_candidate_check
def test_publish_schedules_background_candidate_check(tmp_path, monkeypatch):
    """自审 F3: 挖掘永不自动发布, 人点“发布”后才进策略目录 —— 发布后立即在后台验收一次, 不等下周挖掘。"""
    from app.api import mining as api_mining
    calls = []
    monkeypatch.setattr(sc, "can_run_now", lambda now=None: True)      # 0.30.0: in-session requests queue instead
    monkeypatch.setattr(sc, "run_after_mining", lambda data_dir, repo, *, end: calls.append((data_dir, end)) or {})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    th = sc.schedule_check(tmp_path, request.app.state.repo, reason="t")
    th.join(timeout=5)
    assert calls and calls[0][0] == tmp_path
    calls.clear()
    monkeypatch.setattr(sc, "schedule_check", lambda data_dir, repo, *, reason: calls.append(reason))
    api_mining._schedule_swing_candidate_check(request, "mining_publish")
    assert calls == ["mining_publish"]


@pytest.mark.real_swing_candidate_check
def test_ai_publish_endpoint_schedules_candidate_check(tmp_path, monkeypatch):
    from test_strategy_publish import _save_ai

    from app.api.strategy import publish_ai_strategy
    calls = []
    monkeypatch.setattr(sc, "can_run_now", lambda now=None: True)      # 0.30.0: in-session requests queue instead
    monkeypatch.setattr(sc, "schedule_check", lambda data_dir, repo, *, reason: calls.append(reason))
    request, _ = _save_ai(tmp_path, "ai_draft")
    assert publish_ai_strategy("ai_draft", request)["ok"] is True
    assert calls == ["ai_publish"]


@pytest.mark.real_swing_candidate_check
def test_schedule_check_failure_is_soft(tmp_path, monkeypatch, caplog):
    """后台验收失败只记日志, 不抛到发布请求 (发布结果不受影响)。"""
    def boom(data_dir, repo, *, end):
        raise RuntimeError("worker died")
    monkeypatch.setattr(sc, "run_after_mining", boom)
    with caplog.at_level("WARNING"):
        th = sc.schedule_check(tmp_path, object(), reason="t")
        th.join(timeout=5)
    assert not th.is_alive()
    assert any("swing candidate check after t failed" in r.getMessage() for r in caplog.records)
