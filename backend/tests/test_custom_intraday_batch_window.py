"""自定义数据源 (YAML) 全量分钟修复轮的当日请求窗口 — 按美东墙钟 (P1-2)。

MinuteRefreshService 的修复轮经 kline_sync.fetch_intraday_custom_batch 调
GenericHTTPProvider.get_intraday_batch, 窗口为「当日 00:00 → 现在」。
ZenTrade 约定分钟时间是美东墙钟 (naive): 在 UTC 主机上, 美东 09:45 时
datetime.now() 是 13:45, 窗口若按服务器本地构造会变成「00:00 → 13:45」,
开盘前半小时的分钟K全部漏拉。窗口必须经 market_clock (us_clock.now()) 构造。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.market_clock import clock as clock_module
from app.data_providers.custom import provider as provider_module
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import datetime_payload

ET = ZoneInfo("America/New_York")
_MINUTE_FIELDS = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")


def _host_clock(instant: datetime, host_utc_offset_hours: int) -> type[datetime]:
    """模拟某时区主机的时钟: now() 返回主机本地墙钟, now(tz) 返回该时区的同一时刻。"""
    host_tz = timezone(timedelta(hours=host_utc_offset_hours))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(host_tz).replace(tzinfo=None)
            return instant.astimezone(tz)

    return _Clock


def _capture_window(monkeypatch: pytest.MonkeyPatch, instant: datetime, host_offset: int) -> tuple[str, str]:
    clock = _host_clock(instant, host_offset)
    # 补丁点: market_clock 的 datetime (us_clock.now 的真实来源, 经 market_time 门面
    # 传导到 provider 的 cn_now 调用) 与 provider 自身的 datetime
    monkeypatch.setattr(clock_module, "datetime", clock)
    monkeypatch.setattr(provider_module, "datetime", clock)
    provider = provider_module.GenericHTTPProvider(CustomSourceConfig(
        name="test_source",
        display_name="Test Source",
        datasets={"full_minute": DatasetConfig(
            url="https://example.test/full_minute",
            field_map={name: name for name in _MINUTE_FIELDS},
        )},
    ))
    captured: dict = {}

    def request_rows(cfg, **kwargs):
        captured.update(kwargs)
        return []

    provider._request_rows = request_rows
    try:
        provider.get_intraday_batch(["AAPL"])
    finally:
        provider.close()
    return datetime_payload(captured["start_time"]), datetime_payload(captured["end_time"])


@pytest.mark.parametrize(
    ("et_now", "expected"),
    [
        (datetime(2026, 9, 16, 9, 45, tzinfo=ET), ("2026-09-16T00:00:00", "2026-09-16T09:45:00")),
        (datetime(2026, 9, 16, 14, 30, tzinfo=ET), ("2026-09-16T00:00:00", "2026-09-16T14:30:00")),
    ],
)
def test_intraday_batch_window_is_et_wallclock_on_utc_host(monkeypatch, et_now, expected) -> None:
    assert _capture_window(monkeypatch, et_now, host_offset=0) == expected


def test_intraday_batch_window_unchanged_on_et_host(monkeypatch) -> None:
    """美东时区主机 (如美国 VPS) 上窗口语义一致。"""
    et_now = datetime(2026, 9, 16, 9, 45, tzinfo=ET)
    assert _capture_window(monkeypatch, et_now, host_offset=-4) == (
        "2026-09-16T00:00:00", "2026-09-16T09:45:00",
    )
