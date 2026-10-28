"""P0 补丁回归 (审计 #1 #2 #4 #6, 2026-10-12)。"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.market_clock import us_clock
from app.services import setup_status as ss

ET = us_clock.tz
SCHED = {"hour": 16, "minute": 35}


# ── #1 降级覆盖 strategy:* ──
def _trips(setup, n, r, exit_day=date(2026, 10, 9)):
    return [SimpleNamespace(setup_type=setup, exit_date=exit_day, r_multiple=r, backfill=False) for _ in range(n)]


def test_weekly_evaluation_degrades_strategy_rules(tmp_path):
    store = ss.SetupStatusStore(tmp_path)
    trips = _trips("strategy:phantom_4h_short", 10, -0.5) + _trips("strategy:tv_ut_bot", 10, 0.4)
    changed = ss.run_weekly_evaluation(store, trips, date(2026, 10, 11))
    assert changed == ["strategy:phantom_4h_short"]
    assert store.is_degraded("strategy:phantom_4h_short") and not store.is_degraded("strategy:tv_ut_bot")
    rows = {r["setup"]: r["status"] for r in store.summary()}
    assert rows == {"breakout": "active", "pullback": "active", "strategy:phantom_4h_short": "degraded"}


def test_restore_api_accepts_degraded_strategy_rule(tmp_path):
    from fastapi import HTTPException

    from app.api import trading
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    ss.SetupStatusStore(tmp_path).degrade("strategy:tv_ut_bot", {"samples": 10}, at="2026-10-11T09:00:00-04:00")
    out = trading.restore_setup("strategy:tv_ut_bot", trading.RestoreBody(note="人工复核"), req)
    assert out["setup"]["status"] == "active"
    with pytest.raises(HTTPException):
        trading.restore_setup("strategy:never_seen", trading.RestoreBody(note=""), req)


# ── #6 盘后管道补跑判定 ──
def _ok(started):
    return {"status": "succeeded", "started_at": started, "result": {"zen_trade": {"status": "completed"}}}  # 0.16.1: 须 completed


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def test_catchup_skips_when_last_trading_day_pipeline_succeeded(cal):
    from app.jobs.daily_pipeline import pipeline_catchup_target as t
    sun = datetime(2026, 10, 11, 21, 0, tzinfo=ET)
    jobs = [_ok("2026-10-09T20:35:00Z"), {"status": "succeeded", "started_at": "2026-10-10T13:10:00Z", "result": {}}]
    assert t(jobs, sun, SCHED) is None


def test_catchup_targets_interrupted_day(cal):
    from app.jobs.daily_pipeline import pipeline_catchup_target as t
    fri_night = datetime(2026, 10, 9, 17, 5, tzinfo=ET)
    jobs = [{"status": "failed", "started_at": "2026-10-09T20:35:00Z", "result": None,
             "error": "后端重启,任务中断(启动时补录)"}, _ok("2026-10-08T20:35:00Z")]
    assert t(jobs, fri_night, SCHED) == date(2026, 10, 9)
    # 周一凌晨重启: 目标仍是上周五
    assert t(jobs, datetime(2026, 10, 12, 3, 0, tzinfo=ET), SCHED) == date(2026, 10, 9)


def test_catchup_never_runs_during_trading_hours(cal):
    from app.jobs.daily_pipeline import pipeline_catchup_target as t
    assert t([], datetime(2026, 10, 12, 10, 30, tzinfo=ET), SCHED) is None
    assert t([], datetime(2026, 10, 12, 16, 20, tzinfo=ET), SCHED) is None
    assert t([], datetime(2026, 10, 12, 8, 30, tzinfo=ET), SCHED) == date(2026, 10, 9)


def test_catchup_job_registered(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    assert sched.get_job("daily_pipeline_catchup") is not None
