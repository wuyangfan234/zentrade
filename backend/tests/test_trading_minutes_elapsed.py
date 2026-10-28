"""当日已交易分钟数 — 量比时间折算的分母来源 (美股语义, P1-2)。

compute_enriched_today 用 `time_factor = 390 / elapsed_minutes` 把盘中的部分
成交量折算到全天量级。周末/非交易日按全天 390 算, 否则手动刷新会把量比
放大若干倍而误触发「放量」。经 market_time 门面 → us_clock。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.market_time import (
    trading_minutes_elapsed_from_dt,
    trading_minutes_elapsed_from_ts,
)

ET = ZoneInfo("America/New_York")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.mark.parametrize(
    ("moment", "label"),
    [
        (datetime(2026, 9, 12, 9, 31, tzinfo=ET), "周六 开盘后 1 分钟"),
        (datetime(2026, 9, 12, 10, 0, tzinfo=ET), "周六 上午"),
        (datetime(2026, 9, 13, 14, 0, tzinfo=ET), "周日 下午"),
    ],
)
def test_weekend_counts_as_a_full_session(moment, label):
    assert trading_minutes_elapsed_from_dt(moment) == 390.0, label


def test_weekend_timestamp_also_counts_as_full_session():
    """行情戳落在周末 (源侧异常戳) 时同样按全天算。"""
    assert trading_minutes_elapsed_from_ts(_ms(datetime(2026, 9, 12, 10, 0, tzinfo=ET))) == 390.0


@pytest.mark.parametrize(
    ("moment", "expected", "label"),
    [
        (datetime(2026, 9, 11, 9, 0, tzinfo=ET), 0.0, "周五 开盘前"),
        (datetime(2026, 9, 11, 9, 31, tzinfo=ET), 1.0, "周五 9:31"),
        (datetime(2026, 9, 11, 12, 30, tzinfo=ET), 180.0, "周五 午后 (无午休)"),
        (datetime(2026, 9, 11, 16, 0, tzinfo=ET), 390.0, "周五 收盘"),
        (datetime(2026, 9, 11, 18, 30, tzinfo=ET), 390.0, "周五 收盘后"),
    ],
)
def test_weekday_session_progress_unchanged(moment, expected, label):
    """交易日的折算 (回归保护): 单段 9:30-16:00, 无午休。"""
    assert trading_minutes_elapsed_from_dt(moment) == expected, label


def test_missing_timestamp_still_counts_as_full_session():
    assert trading_minutes_elapsed_from_ts(None) == 390.0
    assert trading_minutes_elapsed_from_ts(0) == 390.0
