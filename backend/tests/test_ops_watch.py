"""运维盯盘: 运行台账、当日应跑清单、补跑、盘中心跳、报警去重 (2026-10-12)。"""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.market_clock import us_clock
from app.services import ops_watch as ow

ET = us_clock.tz
MON = datetime(2026, 10, 12, tzinfo=ET)


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


class Sched:
    def __init__(self, jobs):
        self.jobs = jobs

    def get_job(self, jid):
        return self.jobs.get(jid)


def _job(hour, minute, func=lambda: None):
    return SimpleNamespace(trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York"),
                           func=func, args=(), kwargs={})


def at(h, m):
    return MON.replace(hour=h, minute=m)


# ── 清单 ──
def test_checklist_states(tmp_path):
    s = Sched({"zen_market_mood": _job(8, 35), "zen_premarket_gap": _job(9, 0), "daily_pipeline": _job(16, 35)})
    ow.record_run(tmp_path, "zen_market_mood", "ok", scheduled=at(8, 35), now=at(8, 36))
    items = {i["job"]: i["state"] for i in ow.checklist(tmp_path, s, at(9, 10))}
    assert items == {"zen_market_mood": "ok", "zen_premarket_gap": "pending"}     # 16:35 还没到, 不列
    items = {i["job"]: i["state"] for i in ow.checklist(tmp_path, s, at(9, 30))}
    assert items["zen_premarket_gap"] == "missing"
    ow.record_run(tmp_path, "zen_premarket_gap", "error", scheduled=at(9, 0), error="Boom", now=at(9, 31))
    assert {i["job"]: i["state"] for i in ow.checklist(tmp_path, s, at(9, 32))}["zen_premarket_gap"] == "error"


def test_checklist_empty_on_weekend(tmp_path):
    s = Sched({"zen_market_mood": _job(8, 35)})
    assert ow.checklist(tmp_path, s, datetime(2026, 10, 11, 12, 0, tzinfo=ET)) == []


def test_runs_filtered_by_day(tmp_path):
    ow.record_run(tmp_path, "zen_market_mood", "ok", scheduled=at(8, 35) - timedelta(days=3), now=at(8, 36) - timedelta(days=3))
    s = Sched({"zen_market_mood": _job(8, 35)})
    assert ow.checklist(tmp_path, s, at(9, 0))[0]["state"] == "missing"


# ── 补跑 ──
def test_catchup_runs_once_and_respects_deadline(tmp_path):
    calls = []
    s = Sched({"zen_market_mood": _job(8, 35, lambda: calls.append(1)), "zen_premarket_gap": _job(9, 0, lambda: calls.append(2))})
    out = ow.check(tmp_path, s, None, now=at(9, 15), enqueue=lambda k, p: True)
    ow.wait_catchups(5)                                                            # 0.7.1: 补跑在独立线程
    assert out["caught_up"] == ["zen_market_mood"] and calls == [1]                # 09:00 那个还在 20 分钟宽限内
    out = ow.check(tmp_path, s, None, now=at(9, 16), enqueue=lambda k, p: True)
    assert out["caught_up"] == [] and calls == [1]                                 # 已成功, 不再补
    out = ow.check(tmp_path, s, None, now=at(9, 45), enqueue=lambda k, p: True)
    assert out["caught_up"] == [] and calls == [1]                                 # 09:30 后跳空预警不补


def test_catchup_failure_recorded_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: at(8, 31))   # 补跑线程用真实时钟记账; 固定到 MON, 否则过了 10-12 就落在窗口外
    def boom():
        raise RuntimeError("x")
    s = Sched({"zen_corporate_actions": _job(8, 0, boom)})
    out = ow.check(tmp_path, s, None, now=at(8, 30), enqueue=lambda k, p: True)
    ow.wait_catchups(5)                                                            # 0.7.1: 补跑在独立线程
    out = ow.check(tmp_path, s, None, now=at(8, 33), enqueue=lambda k, p: True)
    assert out["checklist"][0]["state"] == "error"
    assert out["caught_up"] == []


def test_pipeline_not_caught_up_here(tmp_path):
    calls = []
    s = Sched({"daily_pipeline": _job(16, 35, lambda: calls.append(1))})
    ow.check(tmp_path, s, None, now=at(17, 30), enqueue=lambda k, p: True)
    assert calls == []


# ── 心跳 ──
class Loop:
    def __init__(self, run=None, quote=None, err=None, symbols=("AAPL",)):
        self.last_run_at = run.isoformat() if run else None
        self.last_quote_at = quote.isoformat() if quote else None
        self.last_error = err
        self.last_quote_count = 3
        self.ledger = SimpleNamespace(_lock=threading.Lock())
        self._symbols = set(symbols)
        self.last_execution_symbol_count = len(self._symbols)   # 0.7.1: 交易环本轮执行池计数

    def execution_symbols(self):
        return self._symbols


def test_heartbeat_detects_each_problem():
    now = at(10, 0)
    assert ow.heartbeat_problems(Loop(now - timedelta(seconds=20), now - timedelta(seconds=30)), now) == []
    assert [p["reason"] for p in ow.heartbeat_problems(Loop(now - timedelta(minutes=5), now), now)] == ["loop_not_running"]
    assert [p["reason"] for p in ow.heartbeat_problems(Loop(now, now - timedelta(minutes=4)), now)] == ["quotes_stale"]
    assert [p["reason"] for p in ow.heartbeat_problems(Loop(now, now, err="Timeout"), now)] == ["loop_error"]
    assert ow.heartbeat_problems(Loop(now, None, symbols=()), now) == []          # 执行池为空, 不要求报价
    assert [p["reason"] for p in ow.heartbeat_problems(None, now)] == ["loop_not_running"]


def test_heartbeat_quiet_outside_session_and_first_minutes():
    assert ow.heartbeat_problems(None, at(8, 0)) == []
    assert ow.heartbeat_problems(None, at(9, 31)) == []
    assert ow.heartbeat_problems(None, datetime(2026, 10, 11, 11, 0, tzinfo=ET)) == []


# ── 报警 ──
def test_alert_once_then_recovery_then_cap(tmp_path):
    sent = []

    def enq(k, p):
        sent.append((k, p["title"]))
        return True
    prob = [{"reason": "quotes_stale", "detail": "x"}]
    ow.evaluate_alerts(tmp_path, prob, [], at(10, 0), enq)
    ow.evaluate_alerts(tmp_path, prob, [], at(10, 3), enq)                        # 仍在, 不重复
    ow.evaluate_alerts(tmp_path, [], [], at(10, 6), enq)                          # 恢复
    assert [t for _, t in sent] == ["ZenTrade · 盘中异常", "ZenTrade · 盘中恢复"]
    for i in range(10):
        ow.evaluate_alerts(tmp_path, prob, [], at(11, i * 2), enq)
        ow.evaluate_alerts(tmp_path, [], [], at(11, i * 2 + 1), enq)
    assert sum(1 for _, t in sent if t.endswith("盘中异常")) == ow.MAX_ALERTS_PER_REASON


def test_missing_job_alert_key_is_stable(tmp_path):
    keys = []
    item = [{"job": "zen_market_mood", "label": "盘前大盘情绪", "due": at(8, 35).isoformat(), "state": "missing", "last": None}]
    ow.evaluate_alerts(tmp_path, [], item, at(9, 0), lambda k, p: keys.append(k) or True)
    ow.evaluate_alerts(tmp_path, [], item, at(9, 3), lambda k, p: keys.append(k) or True)
    assert keys == ["ops:2026-10-12:job:zen_market_mood"] * 2                    # outbox 按键去重, 只发一次


def test_listener_records_watched_jobs_only(tmp_path):
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
    captured = {}

    class S:
        def add_listener(self, fn, mask):
            captured["fn"] = fn
    ow.attach_listener(S(), tmp_path)
    fn = captured["fn"]
    fn(JobExecutionEvent(EVENT_JOB_EXECUTED, "zen_market_mood", None, at(8, 35)))
    fn(JobExecutionEvent(EVENT_JOB_ERROR, "zen_premarket_gap", None, at(9, 0), exception=ValueError("x")))
    fn(JobExecutionEvent(EVENT_JOB_EXECUTED, "zen_hot_quotes", None, at(9, 0)))
    runs = ow.load_runs(tmp_path, MON.date())
    assert [(r["job"], r["status"]) for r in runs] == [("zen_market_mood", "ok"), ("zen_premarket_gap", "error")]
    assert runs[1]["error"] == "ValueError"


def test_ops_job_registered(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    assert sched.get_job("zen_ops_check") is not None
    for jid in ow.WATCHED:
        assert sched.get_job(jid) is not None, jid                               # 受管清单里的 id 都真实存在


# ── 0.7.1 风险 1: 正点任务还在跑, 不判缺跑、不补跑; 补跑不阻塞盯盘 ──
def test_running_job_not_missing_nor_caught_up(tmp_path):
    calls = []
    s = Sched({"zen_corporate_actions": _job(8, 0, lambda: calls.append(1))})
    ow.mark_running("zen_corporate_actions", at(8, 0))
    try:
        out = ow.check(tmp_path, s, None, now=at(8, 40), enqueue=lambda k, p: True)
        assert out["checklist"][0]["state"] == "running"
        assert out["caught_up"] == [] and out["alerts"] == [] and calls == []
    finally:
        ow.clear_running("zen_corporate_actions")


def test_stale_running_marker_expires(tmp_path):
    s = Sched({"zen_corporate_actions": _job(8, 0)})
    ow.mark_running("zen_corporate_actions", at(8, 0) - ow.RUNNING_TTL - timedelta(minutes=1))
    try:
        assert ow.checklist(tmp_path, s, at(8, 30))[0]["state"] == "missing"
    finally:
        ow.clear_running("zen_corporate_actions")


def test_catchup_does_not_block_check_and_no_concurrent_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: at(8, 31))   # 补跑线程用真实时钟记账; 固定到 MON, 否则过了 10-12 就落在窗口外
    gate = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        gate.wait(5)
    s = Sched({"zen_corporate_actions": _job(8, 0, slow)})
    out = ow.check(tmp_path, s, None, now=at(8, 30), enqueue=lambda k, p: True)     # 立即返回
    assert out["caught_up"] == ["zen_corporate_actions"]
    out2 = ow.check(tmp_path, s, None, now=at(8, 33), enqueue=lambda k, p: True)
    assert out2["caught_up"] == [] and out2["checklist"][0]["state"] == "running"
    gate.set()
    ow.wait_catchups(5)
    assert calls == [1]
    assert ow.checklist(tmp_path, s, at(8, 36))[0]["state"] == "ok"


def test_listener_marks_running_on_submit(tmp_path):
    from apscheduler.events import (
        EVENT_JOB_EXECUTED,
        EVENT_JOB_SUBMITTED,
        JobExecutionEvent,
        JobSubmissionEvent,
    )

    class S:
        def add_listener(self, fn, mask):
            self.fn = fn
    sch = S()
    ow.attach_listener(sch, tmp_path)
    sch.fn(JobSubmissionEvent(EVENT_JOB_SUBMITTED, "zen_corporate_actions", None, [at(8, 0)]))
    assert ow.is_running("zen_corporate_actions", at(8, 5))
    sch.fn(JobExecutionEvent(EVENT_JOB_EXECUTED, "zen_corporate_actions", None, at(8, 0)))
    assert not ow.is_running("zen_corporate_actions", at(8, 6))


# ── 0.7.1 风险 2: 心跳不在台账锁里调 execution_symbols ──
def test_heartbeat_does_not_take_ledger_lock_or_call_execution_symbols():
    now = at(10, 0)
    loop = Loop(now, now - timedelta(minutes=4))
    loop.last_execution_symbol_count = 2

    def nope():
        raise AssertionError("execution_symbols must not be called")
    loop.execution_symbols = nope
    box = {}
    loop.ledger._lock.acquire()                    # 交易环正占着锁
    try:
        t = threading.Thread(target=lambda: box.setdefault("p", ow.heartbeat_problems(loop, now)), daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive(), "heartbeat blocked on ledger lock"
    finally:
        loop.ledger._lock.release()
    assert [p["reason"] for p in box["p"]] == ["quotes_stale"]


# ── 0.7.1 风险 3: 收盘后不误报"已恢复" ──
def test_no_false_recovery_after_close(tmp_path):
    sent = []
    prob = [{"reason": "quotes_stale", "detail": "x"}]
    ow.evaluate_alerts(tmp_path, prob, [], at(15, 50), lambda k, p: sent.append(p["title"]) or True)
    ow.evaluate_alerts(tmp_path, [], [], at(16, 3), lambda k, p: sent.append(p["title"]) or True)
    ow.evaluate_alerts(tmp_path, [], [], at(16, 6), lambda k, p: sent.append(p["title"]) or True)
    assert sent == ["ZenTrade · 盘中异常"]
    assert ow._alert_state(tmp_path, at(16, 6).date())["reasons"]["quotes_stale"]["open"] is False


# ── 顺手: 推送原文脱敏 ──
def test_redact_secrets_in_loop_error():
    now = at(10, 0)
    err = "HTTPError: 401 for https://api.x.com/v1/q?symbols=AAPL&apikey=SEKRIT123 token=abcDEF456 Authorization: Bearer zzz999"
    detail = ow.heartbeat_problems(Loop(now, now, err=err), now)[0]["detail"]
    for s in ("SEKRIT123", "abcDEF456", "zzz999", "symbols=AAPL"):
        assert s not in detail
    assert "HTTPError: 401" in detail and "api.x.com/v1/q" in detail
