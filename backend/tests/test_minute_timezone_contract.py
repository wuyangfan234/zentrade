"""分钟 K datetime 美东墙钟契约测试 (P1-2, 原北京墙钟)。

契约: kline_minute.datetime 必须是美东墙钟 naive。
守卫 _enforce_minute_market_wallclock 在源头入口强制:
- _normalize_minute (timestamp 毫秒为 UTC 基准)
- _try_custom_minute (插件/自定义源帧)

覆盖: epoch/tz-aware 显式转换 / naive 美东墙钟直通 / 盘前盘后过滤。
无时区时间绝不能依据小时分布猜测平移，否则会制造假的盘中价格。

时间锚点: 2026-01-15 为冬令时 (EST = UTC-5); 2026-08-15 为夏令时 (EDT = UTC-4)。
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import polars as pl

from app.services import kline_sync

ET = ZoneInfo("America/New_York")


def _minute_frame(datetimes: list, symbol: str = "AAPL") -> pl.DataFrame:
    n = len(datetimes)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": datetimes,
        "open": [10.0] * n,
        "high": [10.5] * n,
        "low": [9.5] * n,
        "close": [10.2] * n,
        "volume": [100.0] * n,
        "amount": [1020.0] * n,
    })


def _et_day() -> list[datetime]:
    """一个正常交易日墙钟样本: 开盘/午后/收盘 (美股无午休)。"""
    return [
        datetime(2026, 1, 15, 9, 30),
        datetime(2026, 1, 15, 12, 30),
        datetime(2026, 1, 15, 16, 0),
    ]


# ---------- 上游路径: timestamp 毫秒 (UTC 基准) → 美东墙钟 ----------

def test_timestamp_normalizes_to_et_wallclock():
    """09:30 美东(冬) = 14:30 UTC; 毫秒时间戳归一后必须回到 09:30。"""
    ts_ms = [
        int(datetime(2026, 1, 15, 14, 30, tzinfo=UTC).timestamp() * 1000),   # 09:30 ET
        int(datetime(2026, 1, 15, 17, 30, tzinfo=UTC).timestamp() * 1000),   # 12:30 ET
        int(datetime(2026, 1, 15, 21, 0, tzinfo=UTC).timestamp() * 1000),    # 16:00 ET
    ]
    df = pl.DataFrame({
        "symbol": ["AAPL"] * 3,
        "timestamp": ts_ms,
        "open": [10.0] * 3, "high": [10.5] * 3,
        "low": [9.5] * 3, "close": [10.2] * 3,
        "volume": [100.0] * 3, "amount": [1020.0] * 3,
    })
    out = kline_sync._normalize_minute(df)
    assert out["datetime"].to_list() == [
        datetime(2026, 1, 15, 9, 30),
        datetime(2026, 1, 15, 12, 30),
    ]


def test_timestamp_summer_dst_normalizes_correctly():
    """夏令时 (8月, UTC-4): 13:30 UTC = 09:30 美东。"""
    df = pl.DataFrame({
        "symbol": ["AAPL"],
        "timestamp": [int(datetime(2026, 8, 17, 13, 30, tzinfo=UTC).timestamp() * 1000)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    })
    out = kline_sync._normalize_minute(df)
    assert out["datetime"].to_list() == [datetime(2026, 8, 17, 9, 30)]


# ---------- 守卫: 各口径分类 ----------

def test_guard_et_naive_passthrough():
    df = _minute_frame(_et_day())
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == _et_day()


def test_guard_naive_winter_values_are_not_reinterpreted():
    """naive 只有 ET 一种契约；调用方必须显式携带 UTC 时区。"""
    df = _minute_frame([
        datetime(2026, 1, 15, 14, 30),
        datetime(2026, 1, 15, 17, 30),
        datetime(2026, 1, 15, 21, 0),
    ])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == df["datetime"].to_list()


def test_guard_naive_afterhours_is_not_shifted_summer():
    """18:00 ET 是盘后，不得伪造为14:00。"""
    df = _minute_frame([datetime(2026, 8, 17, 18, 0)])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 8, 17, 18, 0)]


def test_guard_ambiguous_overlap_hours_passthrough():
    """重叠区 (13-16 时) naive 帧按美东墙钟直通 — 与 UTC 平移语义不可分, 保守口径。"""
    df = _minute_frame([datetime(2026, 8, 17, 13, 30)])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 8, 17, 13, 30)]


def test_guard_selfheal_is_idempotent():
    df = _minute_frame([datetime(2026, 1, 15, 14, 30)])
    once = kline_sync._enforce_minute_market_wallclock(df, source="t")
    twice = kline_sync._enforce_minute_market_wallclock(once, source="t")
    assert once["datetime"].to_list() == twice["datetime"].to_list()


def test_guard_tzaware_utc_converted():
    """tz-aware UTC 14:30 → 美东墙钟 09:30 (naive, 冬令时)。"""
    df = _minute_frame([datetime(2026, 1, 15, 14, 30, tzinfo=UTC)])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].dtype == pl.Datetime("us")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30)]


def test_guard_tzaware_et_converted():
    """tz-aware 美东 09:30 → 美东墙钟 09:30 (naive), 数值不变。"""
    df = _minute_frame([datetime(2026, 1, 15, 9, 30, tzinfo=ET)])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 9, 30)]


def test_guard_unrecognized_convention_fails_closed():
    """02:30/03:15 (既非美股时段也非 UTC 平移特征) → 拒收。"""
    df = _minute_frame([datetime(2026, 1, 15, 2, 30), datetime(2026, 1, 15, 3, 15)])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == df["datetime"].to_list()
    assert kline_sync._filter_regular_session(out).is_empty()


def test_guard_all_null_datetimes_passthrough():
    """全 null datetime 维持原行为 (下游落盘过滤), 不误伤。"""
    df = _minute_frame([None, None])
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out.height == 2
    assert out["datetime"].null_count() == 2


def test_guard_string_datetimes_classified_after_parse():
    """不带时区的字符串依然按ET解释，不猜UTC。"""
    df = pl.DataFrame({
        "symbol": ["AAPL"],
        "datetime": ["2026-01-15 19:30:00"],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    })
    out = kline_sync._enforce_minute_market_wallclock(df, source="t")
    assert out["datetime"].to_list() == [datetime(2026, 1, 15, 19, 30)]


# ---------- 路由级: 自定义源契约违规 → 回退 ----------

def _setup_custom_provider(monkeypatch, provider: object) -> None:
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "mock_src")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)


def test_custom_provider_afterhours_frame_filtered(monkeypatch):
    """插件返回 ET 盘后帧 → 过滤为空，不能平移伪造盘中价格。"""
    mock_provider = MagicMock()
    mock_provider.get_minute = MagicMock(return_value=_minute_frame(
        [datetime(2026, 1, 15, 19, 30), datetime(2026, 1, 15, 21, 0)]))
    _setup_custom_provider(monkeypatch, mock_provider)

    df, fallback = kline_sync._try_custom_minute(
        ["AAPL"], datetime(2026, 1, 15, 9, 25), datetime(2026, 1, 15, 16, 5),
        asset_type="stock",
    )
    assert fallback is False
    assert df.is_empty()


def test_custom_provider_overnight_minutes_filtered(monkeypatch):
    """插件只有隔夜时间时，返回成功的空集。"""
    mock_provider = MagicMock()
    mock_provider.get_minute = MagicMock(return_value=_minute_frame(
        [datetime(2026, 1, 15, 2, 30)]))
    _setup_custom_provider(monkeypatch, mock_provider)

    df, fallback = kline_sync._try_custom_minute(
        ["AAPL"], datetime(2026, 1, 15, 9, 25), datetime(2026, 1, 15, 16, 5),
        asset_type="stock",
    )
    assert fallback is False
    assert df.is_empty()
