"""Swing 时段权威契约：仅 MarketClock 持有 ET 时段和交易日规则。"""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.market_clock.clock import MarketClock, Session


def clock():
    return MarketClock(tz=ZoneInfo("America/New_York"), sessions=[Session(time(9, 30), time(16))],
                       calendar_provider=lambda start, end: [
                           date(2026, 9, d) for d in (4, 8, 9, 10, 11, 14, 15, 16)
                           if start <= date(2026, 9, d) <= end])


def test_market_datetime_normalizes_iso_utc_and_naive_et():
    c = clock()
    assert c.market_datetime("2026-09-08T13:45:00Z") == c.market_datetime("2026-09-08T09:45:00")
    assert c.market_datetime(datetime(2026, 1, 5, 14, 45, tzinfo=timezone.utc)).hour == 9


def test_entry_window_respects_calendar_and_boundaries():
    c = clock()
    assert c.in_entry_window(c.market_datetime("2026-09-08T09:45:00"))
    assert c.in_entry_window(c.market_datetime("2026-09-08T15:30:00"))
    assert not c.in_entry_window(c.market_datetime("2026-09-08T09:44:59"))
    assert not c.in_entry_window(c.market_datetime("2026-09-08T15:30:01"))
    assert not c.in_entry_window(c.market_datetime("2026-09-07T10:00:00"))


def test_holding_exit_due_skips_holiday_and_exits_overdue_at_open():
    c = clock()
    entry = date(2026, 9, 4)
    assert not c.holding_exit_due(entry, 7, c.market_datetime("2026-09-14T15:30:00"))
    assert not c.holding_exit_due(entry, 7, c.market_datetime("2026-09-15T15:29:59"))
    assert c.holding_exit_due(entry, 7, c.market_datetime("2026-09-15T19:30:00Z"))
    assert c.holding_exit_due(entry, 7, c.market_datetime("2026-09-16T09:30:00"))
    assert not c.holding_exit_due(entry, 7, c.market_datetime("2026-09-16T09:29:59"))
    assert not c.holding_exit_due(entry, 7, c.market_datetime("2026-09-16T16:00:01"))


def test_next_trading_day_skips_weekend_and_holiday():
    c = clock()
    assert c.next_trading_day(date(2026, 9, 4)) == date(2026, 9, 8)   # Fri → Tue (Mon holiday)
    assert c.next_trading_day(date(2026, 9, 8)) == date(2026, 9, 9)
    assert c.next_trading_day(date(2026, 9, 5)) == date(2026, 9, 8)   # Sat → Tue


def test_previous_trading_day_skips_weekend_and_holiday():
    c = clock()
    assert c.previous_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)
    assert c.previous_trading_day(date(2026, 9, 14)) == date(2026, 9, 11)


def test_earnings_reduce_window_is_pre_close_half_hour():
    c = clock()
    assert not c.in_pre_close_window(c.market_datetime("2026-09-08T15:29:59"))
    assert c.in_pre_close_window(c.market_datetime("2026-09-08T15:30:00"))
    assert c.in_pre_close_window(c.market_datetime("2026-09-08T15:59:59"))
    assert not c.in_pre_close_window(c.market_datetime("2026-09-08T16:00:00"))
    assert not c.in_pre_close_window(c.market_datetime("2026-09-07T15:45:00"))  # holiday
