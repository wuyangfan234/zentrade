"""Earnings protection is observable in production wiring (scheduler jobs + status API)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.market_clock import us_clock
from app.services.earnings_calendar import EarningsCalendar

pytestmark = pytest.mark.real_earnings_calendar


def test_scheduler_registers_premarket_and_postclose_earnings_refresh(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    sched = dp.start_scheduler(repo, MagicMock())
    calls = []
    monkeypatch.setattr(EarningsCalendar, "refresh", lambda self, *a, **kw: calls.append(self.dir) or {})
    for job_id, hour in (("zen_earnings_premarket", "8"), ("zen_earnings_postclose", "17")):
        job = sched.get_job(job_id)
        assert job is not None
        assert str(job.trigger.timezone) == "America/New_York"
        assert f"hour='{hour}'" in str(job.trigger)
        job.func()
    assert calls == [tmp_path / "reference" / "earnings"] * 2


def test_status_api_reports_the_real_calendar_state(monkeypatch, tmp_path):
    from app.api import trading
    now = datetime(2026, 9, 30, 10, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        trading_loop=None, scheduler=None, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    body = trading.status(request)
    assert body["protections"]["earnings_auto"] == "unavailable"
    cal = EarningsCalendar(tmp_path)
    cal._write(cal.cache_path, {"fetched_at": now.isoformat(), "source": "nasdaq", "window": [], "events": {}})
    cal._write(cal.state_path, {"last_attempt_at": now.isoformat(), "ok": True, "error": None})
    body = trading.status(request)
    assert body["protections"]["earnings_auto"] == "connected"
    assert body["protections"]["earnings_calendar"]["source"] == "nasdaq"


def test_boot_refresh_failure_does_not_block_trading_loop_boot(monkeypatch, tmp_path):
    from app.ledger.core import Account
    from app.services.trading_loop import TradingLoop
    import polars as pl

    def boom(self, *a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(EarningsCalendar, "refresh", boom)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": []}))
    loop = TradingLoop(repo, Account())
    loop.boot()
    assert loop.status()["booted"] is True
