"""时区契约测试 — 分时拉取窗口必须按北京时间解释, 与服务器本地时区无关。

fetch_minute_single 构造的 naive datetime 会被 _datetime_to_ms 的 .timestamp()
按服务器本地时区解释: UTC 容器 (Docker 默认) 上窗口偏移 8 小时, 补拉必为空。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.market_time import CN_TZ
from app.services import kline_sync
from app.data_providers.models import Cap, CapabilityLimits, CapabilitySet



def _capture_minute_window(monkeypatch, tmp_path, local_dt: datetime, **kwargs) -> dict:
    """桩掉 sync_and_persist_minute 的外部依赖, 只截获传给取数层的时间窗口。

    local_dt 模拟 kline_minute 里的极值 (落盘口径为北京墙钟 naive)。
    """
    captured: dict = {}

    def _fake_sync_minute_batch(symbols, **call_kwargs):
        captured.update(call_kwargs)
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda repo: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda repo: None)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *a, **kw: SimpleNamespace(batch=100, rpm=30))
    monkeypatch.setattr(kline_sync, "sync_minute_batch", _fake_sync_minute_batch)

    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.execute_one = lambda sql: (local_dt,)
    kline_sync.sync_and_persist_minute(
        ["JPM"],
        repo,
        CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}),
        **kwargs,
    )
    return captured


def test_sync_and_persist_minute_incremental_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """增量分钟同步: 起点取自本地分钟 K 的北京墙钟, 终点必须同口径。

    终点用服务器本地墙钟时, _datetime_to_ms 会把两端按同一本地时区换算 →
    UTC 容器上起点(北京 14:30)反而晚于终点, 一个请求都发不出去, 分钟 K 永远
    停在首次拉取的位置。
    """
    local_latest = datetime(2026, 8, 14, 14, 30)  # 本地最新分钟 K: 美东墙钟 naive (P1-2)

    captured = _capture_minute_window(monkeypatch, tmp_path, local_latest)
    start, end = captured["start_time"], captured["end_time"]

    expected_offset = local_latest.replace(tzinfo=CN_TZ).utcoffset()
    assert start.utcoffset() == expected_offset
    assert end.utcoffset() == expected_offset
    assert start < end
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(start) / 1000, tz=CN_TZ
    ) == local_latest.replace(hour=9, minute=30, tzinfo=CN_TZ)


def test_sync_and_persist_minute_extend_backward_window_is_beijing_wall_clock(monkeypatch, tmp_path):
    """向前扩展: 终点取自本地最早分钟 K 的北京墙钟, 同样不能被服务器时区重解释。"""
    local_earliest = datetime(2026, 8, 14, 9, 30)

    captured = _capture_minute_window(
        monkeypatch, tmp_path, local_earliest, days=5, extend_backward=True,
    )
    start, end = captured["start_time"], captured["end_time"]

    expected_offset = local_earliest.replace(tzinfo=CN_TZ).utcoffset()
    assert start.utcoffset() == expected_offset
    assert end.utcoffset() == expected_offset
    assert end - start == timedelta(days=7)  # 5 交易日 x 7/5
    assert datetime.fromtimestamp(
        kline_sync._datetime_to_ms(end) / 1000, tz=CN_TZ
    ) == local_earliest.replace(tzinfo=CN_TZ)



def test_fetch_minute_single_uses_full_et_session(monkeypatch):
    """单股分时补拉窗口覆盖 09:30-16:00 ET，不再使用 A 股 09:25-15:05。"""
    captured: dict[str, datetime] = {}

    def fake_try(symbols, *, start_time, end_time, asset_type, freq):
        captured["start"] = start_time
        captured["end"] = end_time
        return pl.DataFrame(), False

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fake_try)
    out = kline_sync.fetch_minute_single(
        "AAPL", date(2026, 9, 25), capset=MagicMock(),
    )

    assert out.is_empty()
    assert captured["start"].strftime("%H:%M") == "09:30"
    assert captured["end"].strftime("%H:%M") == "16:00"
    assert captured["start"].tzinfo is not None
    assert captured["start"].utcoffset() == timedelta(hours=-4)
