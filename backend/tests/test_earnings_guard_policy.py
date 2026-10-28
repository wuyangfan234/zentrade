"""Earnings guard policy: approved rules (entry block = only the trading day before earnings; reduce before event)."""
from datetime import date, datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.services import earnings_guard as eg


@pytest.fixture(autouse=True)
def weekday_calendar(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


@pytest.mark.parametrize("event_day,timing,today,blocked", [
    # 用户拍板 (2026-10-01, 原文): "仅财报前 1 个交易日禁开（最宽松）"
    ("2026-10-02", "bmo", "2026-10-01", True),     # 财报前一交易日 禁开
    ("2026-10-02", "amc", "2026-10-01", True),
    ("2026-10-02", "unknown", "2026-10-01", True),
    ("2026-10-05", "bmo", "2026-10-02", True),     # 周五 → 下周一财报: 周五是前一交易日
    ("2026-10-01", "amc", "2026-10-01", False),    # 财报当天 允许开 (原文只禁前一交易日)
    ("2026-10-01", "bmo", "2026-10-01", False),
    ("2026-10-05", "amc", "2026-10-01", False),    # 两个交易日后 可开
    ("2026-09-30", "amc", "2026-10-01", False),    # 已过去
])
def test_entry_block_is_only_the_trading_day_before_earnings(event_day, timing, today, blocked):
    event = {"date": event_day, "timing": timing}
    assert eg.entry_blocked(event, date.fromisoformat(today)) is blocked


@pytest.mark.parametrize("event_day,timing,expected", [
    ("2026-10-02", "bmo", "2026-10-01"),       # 盘前 → 前一交易日收盘前
    ("2026-10-02", "unknown", "2026-10-01"),   # 未知按盘前
    ("2026-10-02", "amc", "2026-10-02"),       # 盘后 → 当天收盘前
    ("2026-10-05", "bmo", "2026-10-02"),       # 周一盘前 → 上周五
])
def test_reduce_day_follows_timing(event_day, timing, expected):
    assert eg.reduce_day({"date": event_day, "timing": timing}) == date.fromisoformat(expected)


def test_reduction_applies_only_when_event_falls_inside_the_holding_period():
    entry = date(2026, 9, 28)
    # 第 7 个持仓交易日 = 10-06; 10-08 的财报在时间止损之后 → 不减半
    assert eg.reduction_needed({"date": "2026-10-08", "timing": "bmo"}, entry, 7) is False
    assert eg.reduction_needed({"date": "2026-10-02", "timing": "bmo"}, entry, 7) is True
    assert eg.reduction_needed(None, entry, 7) is False


def test_reduce_window_is_the_pre_close_half_hour_of_the_reduce_day():
    event = {"date": "2026-10-02", "timing": "bmo"}
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=us_clock.tz)
    assert eg.in_reduce_window(event, at("2026-10-01T15:30:00"))
    assert eg.in_reduce_window(event, at("2026-10-01T15:59:00"))
    assert not eg.in_reduce_window(event, at("2026-10-01T15:29:00"))
    assert not eg.in_reduce_window(event, at("2026-10-02T15:45:00"))   # 窗口已过, 不次日补做
    assert eg.reduce_window_missed(event, at("2026-10-01T16:00:00"))
    assert not eg.reduce_window_missed(event, at("2026-10-01T15:45:00"))
