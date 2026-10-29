"""0.30.0 (10-20 产品决策): 齿轮里新策略没有回放数字 -> 写清原因 + 可主动重跑挖掘 / AI 策略的卡回放。
0.30.1 (10-21 产品决策「内置策略的回放重新算」): 内置策略的数字按当前自选与最新数据重算,
来源 (窗口 / 名单 / 数据截至) 写进回放文件本身, 齿轮的来源行读它; 成交 < 5 笔的只给计数 (thin=True)。

- 内置策略的数字来自 config/swing_rule_history.json (离线回放, 机会板同一张卡)。
- 挖掘 / AI 策略的数字来自 research/swing_candidates.json; 验收失败带 detail; 交易时段的请求排队到盘后。
- 做空列表用空头卡名 (META 名是做多名, 如「吊灯止损翻多」)。
"""
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import swing_candidates as sc
from app.services import swing_desk as sd

TUE = date(2026, 10, 20)        # trading day (conftest: weekday calendar)


def at(h, m, d=TUE):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=us_clock.tz)


# ---------- 回放历史文件: 0.30.1 按当前自选与最新数据重算 ----------

THIN = {"long_lower_shadow_reversal", "macd_below_zero_revival", "n_day_low_reversal", "oversold_bounce",
        "oversold_reversal", "tv_wavetrend:short",
        # 0.37.0 追加 (同窗口同名单, --only/--merge): 动量RSI2回调命中 541 次, 过 4h 门只 2 次
        "momentum_rsi2_pullback"}


def test_history_file_is_self_describing():
    """来源 (窗口 / 名单 / 数据截至) 必须写在文件里, 齿轮的来源行读它, 不再写死日期与票数。"""
    h = json.loads(sd.RULE_HISTORY_PATH.read_text(encoding="utf-8"))
    assert h["contract"] == "swing-desk-tight-v1"
    assert h["window"][0] == "2023-01-03" and len(h["window"]) == 2
    assert h["data_end"] and h["data_end"] >= h["window"][1]
    assert h["universe"].startswith("watchlist ") and h["universe"].endswith(f"({h['data_end']})")
    assert h["generated"].endswith("+08:00") or h["generated"].endswith("+00:00")


def test_history_file_lists_the_thin_rules_without_r():
    rules = json.loads(sd.RULE_HISTORY_PATH.read_text(encoding="utf-8"))["rules"]
    for k in THIN:
        x = rules[k]
        assert x["thin"] is True and x["n"] < 5, k
        assert "mean_r" not in x and "excess" not in x, k          # 不足 5 笔不给 R
        assert x["raw"] >= x["signals"] >= x["n"] >= 0, k


def test_history_file_full_entries_are_consistent_with_themselves():
    """39 条够样本的规则: fill = 成交 / 过门卡; 其余字段齐全 (与旧 38 条同一口径)。"""
    rules = json.loads(sd.RULE_HISTORY_PATH.read_text(encoding="utf-8"))["rules"]
    full = {k: v for k, v in rules.items() if not v.get("thin")}
    assert len(rules) == len(full) + len(THIN) == 46
    for k, v in full.items():
        assert {"n", "fill", "mean_r", "excess", "p"} <= set(v), k
        assert v["n"] >= 5 and 0 < v["fill"] <= 1 and 0 <= v["p"] <= 1, k


# ---------- 交易时段排队, 收盘后才跑 ----------

@pytest.mark.parametrize("now, ok", [
    (at(8, 59), True), (at(9, 0), False), (at(12, 0), False), (at(16, 59), False), (at(17, 0), True),
    (at(23, 0), True), (at(12, 0, date(2026, 10, 24)), True),      # 周六任何时刻都能跑
])
def test_can_run_now_window(now, ok):
    assert sc.can_run_now(now) is ok


@pytest.mark.real_swing_candidate_check
def test_request_check_queues_during_session_and_starts_off_hours(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(sc, "schedule_check", lambda data_dir, repo, *, reason: started.append(reason))
    out = sc.request_check(tmp_path, object(), reason="manual", now=at(11, 0))
    assert out["queued"] is True and out["started"] is False and started == []
    assert sc.read_pending(tmp_path)["reason"] == "manual"
    assert sc.status(tmp_path)["pending_at"] == out["requested_at"]
    # 收盘后再点 = 立即跑, 并清掉排队
    out = sc.request_check(tmp_path, object(), reason="manual", now=at(20, 0))
    assert out["started"] is True and started == ["manual"] and sc.read_pending(tmp_path) is None


@pytest.mark.real_swing_candidate_check
def test_run_pending_at_1705(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(sc, "schedule_check", lambda data_dir, repo, *, reason: started.append(reason))
    assert sc.run_pending(tmp_path, object(), now=at(17, 5)) is False           # 没有排队: 什么都不做
    sc.request_check(tmp_path, object(), reason="ai_publish", now=at(16, 55))   # 管道跑完之后点的也排队
    assert sc.run_pending(tmp_path, object(), now=at(16, 58)) is False          # 还在忙: 继续排队
    assert sc.read_pending(tmp_path) is not None
    assert sc.run_pending(tmp_path, object(), now=at(17, 5)) is True
    assert started == ["queued"] and sc.read_pending(tmp_path) is None
    assert sc.run_pending(tmp_path, object(), now=at(17, 6)) is False           # 只跑一次


@pytest.mark.real_swing_candidate_check
def test_is_running_counts_from_start_to_end(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(sc, "run_after_mining", lambda data_dir, repo, *, end: gate.wait(5) and {})
    th = sc.schedule_check(tmp_path, object(), reason="t")
    assert sc.is_running() is True                       # POST 返回时齿轮就能看到「回放中」
    out = sc.request_check(tmp_path, object(), reason="manual", now=at(20, 0), if_idle=True)
    assert out == {"started": False, "queued": False, "running": True}      # 正在跑: 不叠第二次
    gate.set()
    th.join(timeout=5)
    assert sc.is_running() is False


def test_queue_job_registered_at_1705_and_runs_pending(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    job = sched.get_job("zen_swing_candidates_queue")
    assert job is not None
    trig = str(job.trigger)
    assert "day_of_week='mon-fri'" in trig and "hour='17'" in trig and "minute='5'" in trig
    calls = []
    monkeypatch.setattr(sc, "run_pending", lambda data_dir, repo: calls.append(data_dir) or True)
    assert job.func() is True and calls == [tmp_path]


def test_pipeline_task_no_longer_starts_candidate_checks(monkeypatch, tmp_path):
    """排队回放只走 17:05 定时任务一条路径, 盘后管道不再顺手起验收。"""
    from app.jobs import daily_pipeline as dp
    calls = []
    monkeypatch.setattr(dp, "_run_tracked", lambda *_a: True)
    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", lambda state: {})
    monkeypatch.setattr(sc, "run_pending", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(dp, "_app_state_ref", SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), trading_loop=None))
    monkeypatch.setattr("app.services.data_quality.run", lambda *a, **k: None)
    dp._scheduled_pipeline_task(lambda: None)
    assert calls == []


def test_publish_hook_uses_request_check(monkeypatch, tmp_path):
    from app.api import mining as api_mining
    calls = []
    monkeypatch.setattr(sc, "request_check", lambda data_dir, repo, *, reason: calls.append(reason))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    api_mining._schedule_swing_candidate_check(request, "ai_publish")
    assert calls == ["ai_publish"]


# ---------- 验收失败带原因 ----------

def test_evaluate_keeps_error_detail(tmp_path, monkeypatch):
    from app.services import strategy_evaluation_runner as runner

    def boom(*a, **k):
        raise ValueError("策略引用了不存在的信号: ['csg_f_momentum_5d']")
    monkeypatch.setattr(runner, "_strategy_engine", lambda d: SimpleNamespace(list_strategies=lambda: []))
    monkeypatch.setattr(sc, "_signals_any", boom)
    import app.backtest.engine as be
    import app.backtest.strategy as bs
    monkeypatch.setattr(be, "BacktestEngine", lambda repo: None)
    monkeypatch.setattr(bs, "StrategyBacktestService", lambda engine, se: None)
    out = sc.evaluate(tmp_path, object(), ["ai_x"], end=date(2026, 10, 19), watchlist={"A"})
    assert out["rules"]["ai_x"] == {"error": "ValueError", "detail": "策略引用了不存在的信号: ['csg_f_momentum_5d']"}


# ---------- /rules 接口: 做空名 + 候选失败原因 + 重跑入口 ----------

def _client(tmp_path, engine=None):
    from app.api import opportunities as opp_api
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = engine
    return TestClient(app)


def _engine():
    metas = [{"id": "tv_chandelier_exit", "name": "吊灯止损翻多", "source": "builtin"},
             {"id": "tv_squeeze_momentum", "name": "挤压释放做多", "source": "builtin"},
             {"id": "macd_golden", "name": "MACD 金叉放量", "source": "builtin"},
             {"id": "ai_mv5k1eot", "name": "跳空追击", "source": "ai"}]
    return SimpleNamespace(list_strategies=lambda: metas)


def test_rules_api_short_names_and_candidate_errors(tmp_path):
    p = sc.candidates_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"contract": sc.CONTRACT, "generated_at": "2026-10-21T00:41:34+08:00", "rules": {
        "ai_mv5k1eot": {"error": "ValueError", "detail": "策略引用了不存在的信号: ['csg_f_momentum_5d']"}}}))
    g = _client(tmp_path, _engine()).get("/api/opportunities/rules").json()
    by = {r["id"]: r for r in g["catalog"]}
    assert by["tv_chandelier_exit"]["name"] == "吊灯止损翻多"                     # 做多名不变
    assert by["tv_chandelier_exit"]["name_short"] == "吊灯翻空"                   # 做空列表 = 机会板空头卡名
    assert by["tv_squeeze_momentum"]["name_short"] == "挤压动能转弱"
    assert by["macd_golden"]["name_short"] is None                               # 不能做空
    assert by["ai_mv5k1eot"]["candidate"] == {"passed": False, "error": "ValueError",
                                              "detail": "策略引用了不存在的信号: ['csg_f_momentum_5d']"}
    assert g["candidates"]["generated_at"] == "2026-10-21T00:41:34+08:00" and g["candidates"]["running"] is False
    # 0.30.1: 来源行读回放文件自己的元数据 (窗口 / 名单 / 数据截至)
    assert g["history_meta"]["window"][0] == "2023-01-03" and g["history_meta"]["data_end"]
    assert g["history_meta"]["universe"].startswith("watchlist ")


def test_rules_api_candidate_pass_keeps_old_fields_and_adds_mean_r(tmp_path):
    p = sc.candidates_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"rules": {"ai_mv5k1eot": {"passed": False, "n": 171, "mean_r": 0.0654, "excess": 0.0272,
                                                       "excess_ci": [-0.18, 0.23], "r_per_year": 3.0,
                                                       "base_r_per_year": 77.6, "first_passed": None}}}))
    g = _client(tmp_path, _engine()).get("/api/opportunities/rules").json()
    c = {r["id"]: r for r in g["catalog"]}["ai_mv5k1eot"]["candidate"]
    assert c["n"] == 171 and c["mean_r"] == 0.0654 and c["excess"] == 0.0272 and c["passed"] is False


@pytest.mark.real_swing_candidate_check
def test_rerun_endpoint_queues_in_session_and_writes_only_research(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(sc, "schedule_check", lambda data_dir, repo, *, reason: started.append(reason))
    monkeypatch.setattr(sc, "can_run_now", lambda now=None: False)
    r = _client(tmp_path).post("/api/opportunities/rules/candidates/run")
    assert r.status_code == 200 and r.json()["queued"] is True and started == []
    assert [x.relative_to(tmp_path).as_posix() for x in Path(tmp_path).rglob("*") if x.is_file()] == \
        ["research/swing_candidates_pending.json"]
    monkeypatch.setattr(sc, "can_run_now", lambda now=None: True)
    r = _client(tmp_path).post("/api/opportunities/rules/candidates/run")
    assert r.json()["started"] is True and started == ["manual"]


def test_history_file_added_rules_share_the_window():
    """0.37.0: 追加的规则 (--only/--merge) 记在 added 里, 名单只数与文件头一致 (同口径才能和其它规则比)。"""
    h = json.loads(sd.RULE_HISTORY_PATH.read_text(encoding="utf-8"))
    added = h.get("added") or {}
    assert "momentum_rsi2_pullback" in added
    n = h["universe"].split()[1]
    for rule, meta in added.items():
        assert rule in h["rules"], rule
        assert meta["universe"] == f"watchlist {n}", rule
        assert meta["data_end"] >= h["window"][1], rule
