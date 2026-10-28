"""Bug hunt 2026-10-16 (baseline 0d479df): regression tests for confirmed defects."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_instruments_schedule_put_without_scheduler_returns_saved_schedule(monkeypatch):
    # main.py sets app.state.scheduler = None when the scheduler fails to start.
    # The preference is saved first, so the route must still return it (like
    # update_pipeline_schedule does) instead of None -> HTTP 500.
    from app.api import settings as sapi
    from app.services import preferences

    saved = []
    monkeypatch.setattr(preferences, "set_instruments_schedule",
                        lambda h, m: saved.append((h, m)) or {"hour": h, "minute": m})
    app = FastAPI()
    app.state.scheduler = None
    app.include_router(sapi.router)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.put("/api/settings/preferences/instruments-schedule", json={"hour": 9, "minute": 0})
    assert saved == [(9, 0)]
    assert r.status_code == 200
    assert r.json() == {"hour": 9, "minute": 0}


# --- half-day sessions (Alpaca calendar: 2026-11-27 and 2026-12-24 close 13:00 ET) ---

def _half_day_clock():
    from datetime import date
    from datetime import time as dt_time

    from app.market_clock.clock import US_TZ, MarketClock, Session
    days = [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    return MarketClock(tz=US_TZ, sessions=[Session(dt_time(9, 30), dt_time(16, 0))],
                       calendar_provider=lambda s, e: [d for d in days if s <= d <= e],
                       close_provider=lambda d: dt_time(13, 0) if d == date(2026, 11, 27) else None)


def test_half_day_session_ends_at_calendar_close():
    from datetime import datetime

    from app.market_clock.clock import US_TZ
    c = _half_day_clock()
    at = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=US_TZ)  # noqa: E731
    assert c.in_session(at(12, 0)) is True
    assert c.in_session(at(13, 30)) is False
    # entry window and pre-close window move with the early close (close - 30 min)
    assert c.in_entry_window(at(12, 29)) is True
    assert c.in_entry_window(at(12, 31)) is False
    assert c.in_pre_close_window(at(12, 40)) is True
    assert c.in_pre_close_window(at(15, 35)) is False


def test_half_day_time_stop_fires_before_early_close():
    from datetime import date, datetime

    from app.market_clock.clock import US_TZ
    c = _half_day_clock()
    # entry 11-25, max_days=2: day 2 = 11-27 (half day) -> must exit at 12:30, not 15:30
    assert c.holding_exit_due(date(2026, 11, 25), 2, datetime(2026, 11, 27, 12, 35, tzinfo=US_TZ)) is True
    assert c.holding_exit_due(date(2026, 11, 25), 2, datetime(2026, 11, 27, 12, 0, tzinfo=US_TZ)) is False


def test_full_day_unchanged_with_close_provider():
    from datetime import datetime

    from app.market_clock.clock import US_TZ
    c = _half_day_clock()
    at = lambda h, m: datetime(2026, 11, 25, h, m, tzinfo=US_TZ)  # noqa: E731
    assert c.in_session(at(15, 59)) is True
    assert c.in_entry_window(at(15, 30)) is True and c.in_entry_window(at(15, 31)) is False
    assert c.in_pre_close_window(at(15, 30)) is True


def test_calendar_cache_records_early_closes(tmp_path, monkeypatch):
    from datetime import date
    from datetime import time as dt_time

    from app.market_clock import calendar as cal
    rows = [{"date": "2026-11-25", "open": "09:30", "close": "16:00"},
            {"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
    monkeypatch.setattr(cal, "_cache_path", lambda: tmp_path / "cal.json")
    monkeypatch.setattr(cal, "_fetch_calendar_rows", lambda s, e: rows)
    assert cal.alpaca_trading_days(date(2026, 11, 25), date(2026, 11, 27)) == [date(2026, 11, 25), date(2026, 11, 27)]
    assert cal.alpaca_session_close(date(2026, 11, 27)) == dt_time(13, 0)
    assert cal.alpaca_session_close(date(2026, 11, 25)) is None


# --- earnings calendar: Nasdaq returns nothing for the whole window ---

@pytest.mark.real_earnings_calendar
def test_empty_nasdaq_window_is_cross_checked_with_finnhub(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.earnings_calendar import EarningsCalendar
    now = datetime(2026, 10, 14, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    cal = EarningsCalendar(tmp_path)
    st = cal.refresh(now, nasdaq_get=lambda url, headers=None: {"data": None, "status": {"rCode": 200}},
                     finnhub_token="t",
                     finnhub_get=lambda url: {"earningsCalendar": [{"symbol": "AAPL", "date": "2026-10-20", "hour": "amc"}]})
    assert st["source"] == "finnhub"
    assert cal.event_for("AAPL") == {"date": "2026-10-20", "timing": "amc"}


@pytest.mark.real_earnings_calendar
def test_nasdaq_rows_present_do_not_call_finnhub(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.earnings_calendar import EarningsCalendar
    now = datetime(2026, 10, 14, 9, 45, tzinfo=ZoneInfo("America/New_York"))

    def boom(url):
        raise AssertionError("finnhub must not be called")
    st = EarningsCalendar(tmp_path).refresh(
        now, finnhub_token="t", finnhub_get=boom,
        nasdaq_get=lambda url, headers=None: {"data": {"rows": [{"symbol": "MU", "time": "time-after-hours"}]}})
    assert st["source"] == "nasdaq"


# --- positions page: off-session, yesterday's last tick must not be labelled realtime ---

def _portfolio_client(tmp_path, monkeypatch, now, tick_ts):
    from datetime import date
    from types import SimpleNamespace

    import polars as pl

    from app.api.portfolio import router
    from app.ledger.core import Account, Ledger
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "today", lambda: now.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    at = "2026-10-01T10:00:00-04:00"
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10.0, "fee": 1.0, "trade_date": "2026-10-01",
                        "card_id": "opp-2026-09-30-AAA-breakout-1", "setup_type": "breakout", "stop_price": 9.0,
                        "invalidation": 9.0, "target_zone": [12.0, 13.0], "risk_per_share": 1.0, "max_holding_days": 7,
                        "quote_ts": at}, at=at)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                                     get_daily=lambda s, a, b: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [10.5]}))
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {"AAA": (11.0, tick_ts, "tickflow")})
    return TestClient(app)


def test_offsession_old_tick_labelled_last_close(tmp_path, monkeypatch):
    from datetime import datetime

    from app.market_clock import us_clock
    friday_close = datetime(2026, 10, 2, 16, 0, tzinfo=us_clock.tz)
    saturday = datetime(2026, 10, 3, 10, 0, tzinfo=us_clock.tz)
    p = _portfolio_client(tmp_path, monkeypatch, saturday, int(friday_close.timestamp() * 1000)).get(
        "/api/portfolio/live").json()["positions"][0]
    assert p["last_price"] == 11.0               # price unchanged (last trade = close)
    assert p["price_source"] == "last_close"     # label no longer claims realtime


def test_insession_quiet_tick_keeps_realtime_label(tmp_path, monkeypatch):
    from datetime import datetime

    from app.market_clock import us_clock
    now = datetime(2026, 10, 2, 11, 0, tzinfo=us_clock.tz)
    p = _portfolio_client(tmp_path, monkeypatch, now, int(now.timestamp() * 1000) - 600_000).get(
        "/api/portfolio/live").json()["positions"][0]
    assert p["price_source"] == "tickflow" and p["price_age_s"] == 600
