"""Deployment safety: startup never waits on network-bound protection feeds; data errors name the symbol."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.ledger.core import Account
from app.services.adjustment_events import events_for_symbols
from app.services.corporate_actions import CorporateActionService
from app.services.earnings_calendar import EarningsCalendar
from app.services.trading_loop import TradingLoop

pytestmark = [pytest.mark.real_earnings_calendar, pytest.mark.real_corporate_actions]


def _repo(tmp_path):
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": []}))


def test_boot_never_calls_the_network_bound_protection_refresh(tmp_path, monkeypatch):
    """Nasdaq is fetched day by day with a 20s timeout; doing that inside app startup can hold
    the lifespan (and the deploy health check) for minutes. Boot must only replay the ledger."""
    calls = []
    monkeypatch.setattr(EarningsCalendar, "refresh", lambda self, *a, **kw: calls.append("earnings") or {})
    monkeypatch.setattr(CorporateActionService, "refresh",
                        lambda self, *a, **kw: calls.append("corp") or {"status": "unavailable"})
    loop = TradingLoop(_repo(tmp_path), Account())
    loop.boot()
    assert calls == []
    loop.ensure_protection_feeds()
    assert calls == ["earnings", "corp"]


def test_scheduler_runs_the_protection_refresh_once_right_after_start(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    loop = MagicMock()
    monkeypatch.setattr(dp, "_app_state_ref", SimpleNamespace(trading_loop=loop))
    sched = dp.start_scheduler(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), MagicMock())
    job = sched.get_job("zen_protection_boot_refresh")
    assert job is not None
    job.func()
    loop.ensure_protection_feeds.assert_called_once()


def _bar(day, close):
    return {"t": f"{day}T04:00:00Z", "c": close, "v": 100.0}


def test_inconsistent_adjustment_data_names_the_offending_symbol():
    raw = [("GOOD", _bar("2024-06-07", 10.0)), ("BAD", _bar("2024-06-07", 10.0)), ("BAD", _bar("2024-06-10", 10.0))]
    adjusted = [("GOOD", _bar("2024-06-07", 10.0)), ("BAD", _bar("2024-06-07", 10.0))]
    with pytest.raises(ValueError, match="BAD"):
        events_for_symbols(raw, adjusted)


def test_non_finite_close_names_the_offending_symbol():
    raw = [("NAN1", _bar("2024-06-07", float("nan")))]
    with pytest.raises(ValueError, match="NAN1"):
        events_for_symbols(raw, raw)
