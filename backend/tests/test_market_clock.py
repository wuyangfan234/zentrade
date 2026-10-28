"""MarketClock 单元测试（美股语义，零网络）。

日历用注入的内存提供者; 验证单段时段、夏令时、节假日、
fail-open、session_progress 边界、days_held 交易日计数。
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

import pytest

from app.market_clock.clock import MarketClock, Session

ET = ZoneInfo("America/New_York")

# 2026-09 的部分 NYSE 交易日（9/7 Labor Day 休市, 9/5-6 周末）
SEP_DAYS = [date(2026, 9, d) for d in range(1, 31)
            if date(2026, 9, d).weekday() < 5 and d != 7]


def _clock(days: list[date] | None = None) -> MarketClock:
    provider = (lambda s, e: [d for d in (days or []) if s <= d <= e]) if days is not None else None
    return MarketClock(
        tz=ET,
        sessions=[Session(dt_time(9, 30), dt_time(16, 0))],
        calendar_provider=provider,
    )


# ── 基础时段 ─────────────────────────────────────────────

def test_total_minutes_single_session():
    assert _clock().total_minutes == 390


def test_in_session_boundaries():
    clk = _clock(SEP_DAYS)
    d = date(2026, 9, 8)  # 周二
    assert clk.in_session(datetime(2026, 9, 8, 9, 30, tzinfo=ET)) is True   # 开盘边界
    assert clk.in_session(datetime(2026, 9, 8, 16, 0, tzinfo=ET)) is True   # 收盘边界
    assert clk.in_session(datetime(2026, 9, 8, 9, 29, tzinfo=ET)) is False
    assert clk.in_session(datetime(2026, 9, 8, 16, 1, tzinfo=ET)) is False
    assert clk.in_session(datetime(2026, 9, 8, 12, 0, tzinfo=ET)) is True   # 午间不休市


def test_session_progress():
    clk = _clock(SEP_DAYS)
    assert clk.session_progress(datetime(2026, 9, 8, 9, 0, tzinfo=ET)) == 0.0
    assert clk.session_progress(datetime(2026, 9, 8, 9, 30, tzinfo=ET)) == 0.0
    assert clk.session_progress(datetime(2026, 9, 8, 12, 45, tzinfo=ET)) == pytest.approx(195.0)
    assert clk.session_progress(datetime(2026, 9, 8, 16, 0, tzinfo=ET)) == 390.0
    assert clk.session_progress(datetime(2026, 9, 8, 20, 0, tzinfo=ET)) == 390.0
    # 周末恒为全天 (避免量比折算成 0)
    assert clk.session_progress(datetime(2026, 9, 12, 12, 0, tzinfo=ET)) == 390.0


# ── 交易日 ───────────────────────────────────────────────

def test_weekend_never_trading():
    clk = _clock(SEP_DAYS)
    assert clk.is_trading_day(date(2026, 9, 12)) is False  # 周六
    assert clk.is_trading_day(date(2026, 9, 13)) is False  # 周日


def test_holiday_from_calendar():
    clk = _clock(SEP_DAYS)
    assert clk.is_trading_day(date(2026, 9, 7)) is False   # Labor Day
    assert clk.is_trading_day(date(2026, 9, 8)) is True


def test_no_provider_weekday_approximation():
    clk = _clock()  # 无日历提供者
    assert clk.is_trading_day(date(2026, 9, 8)) is True    # 周二按工作日近似
    assert clk.is_trading_day(date(2026, 9, 7)) is True    # 节假日不可知, 近似 True
    assert clk.is_trading_day(date(2026, 9, 12)) is False


def test_calendar_failure_fails_open(monkeypatch):
    def _boom(s, e):
        raise RuntimeError("network down")
    clk = MarketClock(
        tz=ET,
        sessions=[Session(dt_time(9, 30), dt_time(16, 0))],
        calendar_provider=_boom,
    )
    # 拉取失败: 保守视为交易日, 不阻塞管线
    assert clk.is_trading_day(date(2026, 9, 8)) is True
    assert clk.is_trading_day(date(2026, 9, 12)) is False  # 周末仍直判


# ── 夏令时（EDT vs EST 同一时钟语义不变） ─────────────────

def test_dst_summer_and_winter_same_wallclock():
    clk = _clock(SEP_DAYS)
    # 夏令时 (9月 EDT, UTC-4)
    assert clk.in_session(datetime(2026, 9, 8, 9, 30, tzinfo=ET)) is True
    # 冬令时 (1月 EST, UTC-5) — ZoneInfo 自动处理, 墙钟语义不变
    jan_days = [d for d in range(1, 32) if date(2026, 1, d).weekday() < 5]
    clk_jan = _clock(jan_days)
    assert clk_jan.in_session(datetime(2026, 1, 5, 9, 30, tzinfo=ET)) is True
    assert clk_jan.in_session(datetime(2026, 1, 5, 16, 1, tzinfo=ET)) is False


# ── days_held（时间止损的交易日计数） ─────────────────────

def test_days_held_counts_trading_days():
    clk = _clock(SEP_DAYS)
    # 9/8(周二) 入场: 当天=1, 9/9=2, 周末不计, 9/14(周一)=5
    assert clk.days_held(date(2026, 9, 8), date(2026, 9, 8)) == 1
    assert clk.days_held(date(2026, 9, 8), date(2026, 9, 9)) == 2
    assert clk.days_held(date(2026, 9, 8), date(2026, 9, 14)) == 5
    # 跨 Labor Day 假日不计: [9/4, 9/8] = 2
    assert clk.days_held(date(2026, 9, 4), date(2026, 9, 8)) == 2
    # 防御: today < entry
    assert clk.days_held(date(2026, 9, 8), date(2026, 9, 7)) == 0


def test_trading_days_between():
    clk = _clock(SEP_DAYS)
    days = clk.trading_days_between(date(2026, 9, 4), date(2026, 9, 9))
    assert days == [date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]  # 跳过周末+假日


# ── 生产单例接线 ─────────────────────────────────────────

def test_us_clock_singleton_shape():
    from app.market_clock import us_clock
    assert us_clock.total_minutes == 390
    assert str(us_clock.tz) == "America/New_York"
    assert len(us_clock.sessions) == 1
