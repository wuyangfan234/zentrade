"""Earnings calendar retries recover protection after a transient network outage."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.market_clock import us_clock
from app.services import earnings_calendar as ec
from app.services.earnings_calendar import EarningsCalendar

pytestmark = pytest.mark.real_earnings_calendar

FRI = date(2026, 11, 13)


def at(hour, minute=0):
    return datetime(FRI.year, FRI.month, FRI.day, hour, minute, tzinfo=us_clock.tz)


@pytest.fixture(autouse=True)
def weekday_calendar(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "_cal_cache", {})


def make_scheduler(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline as dp

    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return dp.start_scheduler(repo, MagicMock())


def prime_calendar(tmp_path):
    def payload(url, headers=None):
        return {"data": {"rows": [{"symbol": "MU", "time": "time-after-hours"}]}}

    cal = EarningsCalendar(tmp_path)
    assert cal.refresh(at(8, 30), nasdaq_get=payload, use_finnhub=False)["status"] == "connected"
    return cal


def fail_sources(monkeypatch, calls):
    def down(*args, **kwargs):
        calls.append(us_clock.now())
        raise RuntimeError("network down")

    def backup_down(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(ec, "fetch_nasdaq", down)
    monkeypatch.setattr(ec, "fetch_finnhub", backup_down)
    monkeypatch.setattr(ec, "_configured_finnhub_token", lambda: "placeholder")


def test_retry_job_is_registered_and_has_a_chinese_label(monkeypatch, tmp_path):
    sched = make_scheduler(monkeypatch, tmp_path)
    job = sched.get_job("zen_earnings_retry")
    assert job is not None
    assert "0:15:00" in str(job.trigger)
    source = (Path(__file__).resolve().parents[2] / "frontend/src/pages/SystemStatus.tsx").read_text(
        encoding="utf-8"
    )
    assert "zen_earnings_retry:" in source


def test_retry_recovers_real_calendar_after_sources_return(monkeypatch, tmp_path):
    cal = prime_calendar(tmp_path)
    sched = make_scheduler(monkeypatch, tmp_path)
    current = [at(17, 0)]
    monkeypatch.setattr(us_clock, "now", lambda: current[0])
    calls = []
    fail_sources(monkeypatch, calls)
    assert sched.get_job("zen_earnings_postclose").func()["status"] == "unavailable"
    assert cal.status()["status"] == "unavailable"

    retry = sched.get_job("zen_earnings_retry").func
    current[0] = at(17, 14)
    assert retry() is None

    recovered = []

    def ok(start, end, http_get=None):
        recovered.append((start, end))
        return [{"symbol": "MU", "date": "2026-11-16", "timing": "amc"}]

    monkeypatch.setattr(ec, "fetch_nasdaq", ok)
    current[0] = at(17, 15)
    result = retry()
    assert result["status"] == "connected"
    assert cal.status()["status"] == "connected"
    assert cal.status()["fetched_at"] == at(17, 15).isoformat()
    assert cal.event_for("MU") == {"date": "2026-11-16", "timing": "amc"}
    assert len(recovered) == 1

    current[0] = at(17, 30)
    assert retry() is None
    assert len(recovered) == 1


def test_retry_backs_off_and_keeps_retrying_during_a_long_outage(monkeypatch, tmp_path):
    prime_calendar(tmp_path)
    sched = make_scheduler(monkeypatch, tmp_path)
    current = [at(17, 0)]
    monkeypatch.setattr(us_clock, "now", lambda: current[0])
    calls = []
    fail_sources(monkeypatch, calls)
    sched.get_job("zen_earnings_postclose").func()
    retry = sched.get_job("zen_earnings_retry").func

    expected_attempts = [at(17, 0)]
    for hour, minute, should_run in (
        (17, 14, False),
        (17, 15, True),
        (17, 30, False),
        (17, 45, True),
        (18, 45, True),
        (19, 45, False),
        (20, 45, True),
        (22, 45, True),
    ):
        current[0] = at(hour, minute)
        result = retry()
        if should_run:
            expected_attempts.append(at(hour, minute))
            assert result["status"] in {"unavailable", "stale"}
            assert not EarningsCalendar(tmp_path).usable(current[0])
        else:
            assert result is None
        assert calls == expected_attempts
