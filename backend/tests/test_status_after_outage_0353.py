"""0.35.3 断网后的状态页自愈 (10-22 实例: 14:59-16:36 ET NAS 外网故障, 网络恢复后状态页仍挂 3 红 3 黄到次日)。

- 交易环 / 热报价: 非盘中时 last_error 是上一交易时段遗留, 降为休市 (盘中仍标红)。
- 飞书推送: 失败早于最近一次成功发送 = 通道已恢复, 不再标黄。
- 盘中强势卡: 取数失败之后有一次成功 = 已恢复, 不再标黄。
- 盘后管道: 失败后自动重试 (退避 15/30/60/120 分钟); 补跑 / 手动补跑成功后补跑日K数据质量并补记运维台账。
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.trading import router
from app.market_clock import us_clock
from app.services import ops_watch

ET = us_clock.tz
THU = date(2026, 10, 22)
SCHED = {"hour": 16, "minute": 35}


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def et(d, h, m=0, s=0):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=ET)


def _client(tmp_path, monkeypatch, now, loop_st, hq=None):
    from app.services import hot_quotes
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(ops_watch, "status", lambda d, s, lp, now=None: {"checklist": [], "problems": [], "trading_day": True})
    if hq is not None:
        monkeypatch.setattr(hot_quotes, "status", lambda: dict(hq))
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = SimpleNamespace(status=lambda: loop_st)
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [], get_job=lambda j: None)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _c(body, cid):
    return next(c for c in body["components"] if c["id"] == cid)


LOOP_ERR = {"booted": True, "last_error": "URLError: <urlopen error [Errno 104] Connection reset by peer>",
            "last_run_at": et(THU, 15, 59, 37).isoformat(), "execution_source": {"active": "iex_fallback"}}
HQ_ERR = {"at": None, "count": 44, "pool": 44, "error": "URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>",
          "error_at": et(THU, 15, 59, 50).timestamp()}


# ── 交易环 / 热报价 ──
def test_postmarket_stale_loop_and_hot_quote_errors_are_closed_not_red(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, et(THU, 21, 27), LOOP_ERR, HQ_ERR).get("/api/trading/system-status").json()
    for cid in ("paper_loop", "hot_quotes"):
        c = _c(body, cid)
        assert c["state"] == "idle" and c["idle_kind"] == "closed" and c["stale"], cid
        assert "上一交易时段最后一次报错" in c["summary"] and "action" not in c, cid
    assert "15:59 ET" in _c(body, "hot_quotes")["summary"]                     # 报错时刻写明
    assert _c(body, "paper_loop")["since"].startswith("2026-10-22T15:59")


def test_premarket_stale_errors_are_waiting(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, et(date(2026, 10, 23), 8, 0), LOOP_ERR, HQ_ERR).get(
        "/api/trading/system-status").json()
    assert {_c(body, cid)["idle_kind"] for cid in ("paper_loop", "hot_quotes")} == {"waiting"}


def test_in_session_errors_stay_red(tmp_path, monkeypatch):
    now = et(date(2026, 10, 23), 10, 0)
    st = {**LOOP_ERR, "last_run_at": (now - timedelta(seconds=10)).isoformat()}
    hq = {**HQ_ERR, "error_at": (now - timedelta(seconds=5)).timestamp()}
    body = _client(tmp_path, monkeypatch, now, st, hq).get("/api/trading/system-status").json()
    for cid in ("paper_loop", "hot_quotes"):
        assert _c(body, cid)["state"] == "error" and _c(body, cid)["action"], cid


def test_hot_quotes_records_error_time_and_clears_on_success(monkeypatch):
    from app.services import hot_quotes as hq
    monkeypatch.setattr(us_clock, "now", lambda: et(THU, 10, 0))
    calls = []

    class P:
        def poll_hot_quotes(self, syms):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("reset")
            return len(syms)
    monkeypatch.setattr(hq, "provider", lambda: P())
    monkeypatch.setattr(hq, "pool", lambda loop=None: ["NVDA"])
    monkeypatch.setattr(hq, "_last", {"at": None, "count": 0, "pool": 0, "error": None, "error_at": None})
    assert hq.tick()["error"] and hq.status()["error_at"]
    st = hq.tick()
    assert st["error"] is None and st["error_at"] is None and st["count"] == 1


# ── 飞书推送 ──
def _queue(tmp_path, now, ok):
    from app.services.notification_outbox import NotificationOutbox
    return NotificationOutbox(tmp_path, sender=lambda p: ok[0], configured=lambda: True, now=lambda: now[0], max_attempts=1)


def test_old_failures_before_a_later_success_mean_channel_recovered(tmp_path):
    now, ok = [100.0], [False]
    q = _queue(tmp_path, now, ok)
    q.enqueue("ops:2026-10-22:loop_error:1", {"title": "t", "body": "b"})
    q.flush()
    assert q.status()["status"] == "failed" and q.status()["unresolved_failed"] == 1   # 断网中: 失败
    now[0], ok[0] = 200.0, True
    q.enqueue("daily:2026-10-22", {"title": "t", "body": "b"})
    q.flush()
    st = q.status()
    assert st["counts"]["failed"] == 1 and st["unresolved_failed"] == 0 and st["status"] == "sent"
    assert st["last_sent_at"] == 200.0 and st["last_failed_at"] == 100.0
    now[0], ok[0] = 300.0, False                                                         # 恢复后又坏: 再报
    q.enqueue("ops:2026-10-23:x", {"title": "t", "body": "b"})
    q.flush()
    assert q.status()["status"] == "failed" and q.status()["unresolved_failed"] == 1


def test_status_row_notifications_ok_after_recovery(tmp_path, monkeypatch):
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "notification_status", lambda d: {
        "configured": True, "status": "sent", "label": "成功", "counts": {"unconfigured": 0, "pending": 0, "sent": 106, "failed": 2},
        "labels": {"unconfigured": "未配置", "pending": "待发送", "sent": "成功", "failed": "失败"},
        "unresolved_failed": 0, "last_sent_at": et(THU, 20, 57).timestamp(), "last_failed_at": et(THU, 15, 2).timestamp()})
    body = _client(tmp_path, monkeypatch, et(THU, 21, 27), {**LOOP_ERR, "last_error": None}).get("/api/trading/system-status").json()
    n = _c(body, "notifications")
    assert n["state"] == "ok" and "其中 2 条失败早于最近一次成功发送 2026-10-22 20:57 ET" in n["summary"]


# ── 盘中强势卡 ──
def test_intraday_strength_error_then_success_is_recovered(tmp_path, monkeypatch):
    from app.services import intraday_strength as ids
    d0 = date(2026, 10, 19)
    ids._note_error(tmp_path, d0, et(d0, 15, 0), None, TimeoutError("x"))
    monkeypatch.setattr(us_clock, "now", lambda: et(d0, 15, 10))
    st = ids.status(tmp_path)
    assert st["errors"] == 1 and st["unresolved"] == 1 and st["recovered_at"] is None
    ids._note_ok(tmp_path, d0, et(d0, 16, 25))
    st = ids.status(tmp_path)
    assert st["errors"] == 1 and st["unresolved"] == 0 and st["recovered_at"].startswith("2026-10-19T16:25")
    ids._note_error(tmp_path, d0, et(d0, 16, 30), None, TimeoutError("y"))     # 恢复后再失败: 再标黄
    assert ids.status(tmp_path)["unresolved"] == 1


def test_note_ok_does_not_write_on_clean_day(tmp_path):
    from app.services import intraday_strength as ids
    d0 = date(2026, 10, 19)
    ids._note_ok(tmp_path, d0, et(d0, 10, 0))
    assert not ids.day_path(tmp_path, d0).exists()


def test_status_row_intraday_recovered_is_ok(tmp_path, monkeypatch):
    from app.services import intraday_strength as ids
    ids._note_error(tmp_path, THU, et(THU, 15, 30), None, OSError("x"))
    rec = ids.load_day(tmp_path, THU)
    rec["checks"] = [{"at": et(THU, 10, 0).isoformat(), "check": "10:00"}]
    ids._save_day(tmp_path, rec)
    ids._note_ok(tmp_path, THU, et(THU, 16, 25))
    row = _c(_client(tmp_path, monkeypatch, et(THU, 21, 27), {**LOOP_ERR, "last_error": None}).get(
        "/api/trading/system-status").json(), "intraday_strength")
    assert row["state"] == "ok" and "16:25 ET 已恢复" in row["summary"]


# ── 盘后管道失败自动重试 ──
def _failed(started="2026-10-22T20:35:00Z", finished="2026-10-22T20:35:40Z", **kw):
    return {"id": "f", "status": "failed", "stage": "sync_daily", "started_at": started, "finished_at": finished,
            "result": None, "error": "scheduled daily_pipeline failed: URLError: reset", **kw}


def _ok(started="2026-10-22T20:35:00Z"):
    return {"id": "o", "status": "succeeded", "stage": "done", "started_at": started, "finished_at": started,
            "result": {"zen_trade": {"status": "completed"}}, "error": None}


def test_retry_due_after_backoff_only(cal):
    from app.jobs.daily_pipeline import PIPELINE_RETRY_GAPS_MIN
    from app.jobs.daily_pipeline import pipeline_retry_due as due
    jobs = [_failed()]
    assert due(jobs, et(THU, 16, 45), SCHED) is None                           # 失败 16:35:40, 不到 15 分钟
    assert due(jobs, et(THU, 16, 51), SCHED) == THU
    assert due(jobs, et(THU, 16, 51), SCHED, active=True) is None              # 已有任务在跑
    assert due(jobs, et(THU, 17, 0), SCHED, attempts=1) is None                # 第 2 次要等 30 分钟
    assert due(jobs, et(THU, 17, 6), SCHED, attempts=1) == THU
    assert due(jobs, et(THU, 23, 0), SCHED, attempts=len(PIPELINE_RETRY_GAPS_MIN)) is None   # 次数用完


def test_retry_not_after_success_nor_into_next_morning(cal):
    from app.jobs.daily_pipeline import pipeline_retry_due as due
    assert due([_ok("2026-10-22T21:00:00Z"), _failed()], et(THU, 22, 0), SCHED) is None
    jobs = [_failed()]
    assert due(jobs, et(date(2026, 10, 23), 3, 0), SCHED) == THU               # 夜里网络恢复: 照样补
    assert due(jobs, et(date(2026, 10, 23), 8, 45), SCHED) is None             # 08:30 起不再起新重试
    assert due(jobs, et(date(2026, 10, 23), 11, 0), SCHED) is None             # 盘中不跑


def test_retry_ignores_other_jobs_and_never_ran(cal):
    from app.jobs.daily_pipeline import pipeline_retry_due as due
    inst = {"id": "i", "status": "failed", "stage": "sync_instruments", "started_at": "2026-10-22T20:40:00Z",
            "finished_at": "2026-10-22T20:40:05Z", "result": None, "error": "boom"}
    assert due([inst], et(THU, 18, 0), SCHED) is None                          # 维表同步失败不算管道失败
    assert due([], et(THU, 18, 0), SCHED) is None                              # 没跑过交给开机补跑
    assert due([inst, _failed()], et(THU, 18, 0), SCHED) == THU


def test_retry_job_registered(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    j = sched.get_job("daily_pipeline_retry")
    assert j is not None and "0:15:00" in str(j.trigger)
    src = (Path(__file__).resolve().parents[2] / "frontend/src/pages/SystemStatus.tsx").read_text(encoding="utf-8")
    assert "daily_pipeline_retry:" in src                                      # 状态页任务名有中文


def test_retry_wrapper_runs_once_per_backoff(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    from app.services import pipeline_jobs
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    monkeypatch.setattr(dp._prefs, "get_pipeline_schedule", lambda: dict(SCHED))
    runs = []
    monkeypatch.setattr(dp, "_scheduled_pipeline_task", lambda fn, catchup=False, **k: runs.append(catchup) or True)
    monkeypatch.setattr(pipeline_jobs.job_store, "list_recent", lambda n=20: [_failed()])
    monkeypatch.setattr(pipeline_jobs.job_store, "active_id", lambda: None)
    sched = dp.start_scheduler(state.repo, MagicMock())
    fn = sched.get_job("daily_pipeline_retry").func
    monkeypatch.setattr(us_clock, "now", lambda: et(THU, 16, 51))
    assert fn() is True and runs == [True]
    assert fn() is None and runs == [True]                                     # 第 2 次要等 30 分钟
    monkeypatch.setattr(us_clock, "now", lambda: et(THU, 17, 6))
    assert fn() is True and runs == [True, True]


# ── 补跑成功后: 数据质量 + 运维台账 ──
def test_record_pipeline_catchup_turns_checklist_ok(tmp_path, monkeypatch):
    from apscheduler.triggers.cron import CronTrigger

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp._prefs, "get_pipeline_schedule", lambda: dict(SCHED))
    ops_watch.record_run(tmp_path, "daily_pipeline", "error", scheduled=et(THU, 16, 35), error="URLError", now=et(THU, 16, 35, 40))
    s = SimpleNamespace(get_job=lambda j: SimpleNamespace(trigger=CronTrigger(
        day_of_week="mon-fri", hour=16, minute=35, timezone="America/New_York")) if j == "daily_pipeline" else None)
    assert ops_watch.checklist(tmp_path, s, et(THU, 21, 0))[0]["state"] == "error"
    assert dp.record_pipeline_catchup(tmp_path, et(THU, 16, 0)) is False       # 还没到调度时刻
    assert dp.record_pipeline_catchup(tmp_path, et(THU, 20, 57)) is True
    assert dp.record_pipeline_catchup(tmp_path, et(THU, 21, 0)) is False       # 只记一次
    assert ops_watch.checklist(tmp_path, s, et(THU, 21, 0))[0]["state"] == "ok"
    assert dp.record_pipeline_catchup(tmp_path, et(date(2026, 10, 24), 12, 0)) is False   # 周六


def test_manual_success_runs_data_quality_and_ledger(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline as dp
    calls = []
    monkeypatch.setattr("app.services.data_quality.run", lambda d, loop=None: calls.append(("dq", d)))
    monkeypatch.setattr(dp, "record_pipeline_catchup", lambda d, now=None, **k: calls.append(("ops", d)) or True)
    st = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), trading_loop=None)
    dp.after_manual_pipeline_success(st, {"zen_trade": {"status": "skipped"}})
    assert calls == []                                                         # zen_trade 没完成: 不收尾
    dp.after_manual_pipeline_success(st, {"zen_trade": {"status": "completed"}})
    assert calls == [("dq", tmp_path), ("ops", tmp_path)]


def test_scheduled_catchup_records_ledger_and_plain_run_does_not(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline as dp
    calls = []
    monkeypatch.setattr(dp, "_run_tracked", lambda *_a: True)
    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", lambda state: {})
    monkeypatch.setattr("app.services.data_quality.run", lambda *a, **k: calls.append("dq"))
    monkeypatch.setattr(dp, "record_pipeline_catchup", lambda d, now=None, **k: calls.append("ops") or True)
    monkeypatch.setattr(dp, "_app_state_ref", SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                                                              trading_loop=None))
    assert dp._scheduled_pipeline_task(lambda: None) is True and calls == ["dq"]
    calls.clear()
    assert dp._scheduled_pipeline_task(lambda: None, catchup=True) is True and calls == ["dq", "ops"]


def test_manual_api_calls_post_success_hook(tmp_path, monkeypatch):
    from app.api import pipeline
    from app.services import heavy_job_limiter, pipeline_jobs, preferences
    from app.services.heavy_job_limiter import HeavyJobLimiter
    from app.services.pipeline_jobs import JobStore
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005))
    monkeypatch.setattr(pipeline.daily_pipeline, "run_now", lambda *a, **k: {"zen_trade": {"status": "completed"}})
    hooked = []
    monkeypatch.setattr(pipeline.daily_pipeline, "after_manual_pipeline_success", lambda st, res: hooked.append(res))
    app = FastAPI()
    app.include_router(pipeline.router)
    app.state.repo = SimpleNamespace(refresh_cache=lambda: None)
    app.state.capabilities = object()
    import time
    with TestClient(app) as client:
        jid = client.post("/api/pipeline/run").json()["job_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not hooked:
            time.sleep(0.01)
    assert store.get(jid)["status"] == "succeeded" and hooked == [{"zen_trade": {"status": "completed"}}]


def test_notifications_action_points_at_real_place():
    from app.services.system_status_view import ACTION
    assert "outbox.sqlite3" in ACTION["notifications"] and "发送测试" in ACTION["notifications"]
    assert json.dumps(ACTION, ensure_ascii=False).count("查看推送失败原因") == 0
