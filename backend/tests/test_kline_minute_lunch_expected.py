"""个股分时 /minute 与自选分时 /minute-batch 的期望根数边界 (美股单段语义, P1-2b)。

两个端点按美东时刻估算当日「应有」的分钟K根数 (9:30 起连续, 无午休),
本地根数够了就直接用本地, 不够才去数据源实时拉取。
边界: 11:30 时刻应有 120 根; 11:31 起应有 121+ 根 — 上午只有 120 根的
本地数据在 11:31 后即判「尾部落后」走增量补拉 (与 A股午休语义的本质区别)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline as kline_api
from app.market_time import CN_TZ
from app.services import preferences, trading_day
from app.data_providers.models import Cap, CapabilityLimits, CapabilitySet

TODAY = date(2026, 9, 15)  # 周二, 交易日
SYMBOL = "MCD"


def _morning(symbol: str = SYMBOL, bars: int = 120) -> pl.DataFrame:
    """当日上午从 09:31 起连续 bars 根分钟K (120 根 = 完整上午, 到 11:30)。"""
    start = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 30)
    return pl.DataFrame({
        "symbol": [symbol] * bars,
        "datetime": [start + timedelta(minutes=i) for i in range(bars)],
        "close": [100.0] * bars,
    })


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """钉住交易日与探针, 数据源补拉一律返回空; 返回 (设置北京时刻的函数, 单股拉取 mock, 批量拉取 mock)。"""
    monkeypatch.setattr(kline_api.us_clock, "today", lambda: TODAY)
    monkeypatch.setattr(kline_api.us_clock, "in_session", lambda: False)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    fetch = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", fetch)
    sync = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync)

    def set_time(hour: int, minute: int) -> None:
        now = datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute, tzinfo=CN_TZ)
        monkeypatch.setattr(kline_api.us_clock, "now", lambda: now)

    return set_time, fetch, sync


# 完整上午边界 (美股无午休): 11:30 应有 120 根。
# /minute 有 10% 容差: 11:35 (应有 125) 仍完整; batch 用 expected-2: 11:35 即落后
COMPLETE_BOUNDARY_TIMES = [(11, 29), (11, 30)]
STALE_AFTER_BOUNDARY_TIMES = [(11, 31), (11, 35), (11, 50), (12, 0)]
BATCH_COMPLETE_TIMES = [(11, 30)]
BATCH_STALE_TIMES = [(11, 35), (11, 50), (12, 0)]


# ---------- /minute (个股详情分时) ----------


class _FakeRepo:
    def __init__(self, bars: int = 120) -> None:
        self._bars = bars
        # /minute 与 /minute-batch 读取 repo.store.data_dir 判断分钟基准标记
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        self.store = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": [], "name": [], "total_shares": [], "float_shares": []})

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        return pl.DataFrame({"date": [], "close": []})

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        if trade_date == TODAY:
            return _morning(bars=self._bars).drop("symbol")
        return pl.DataFrame()


def _client(bars: int = 120) -> TestClient:
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = _FakeRepo(bars)
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    return TestClient(app)


@pytest.mark.parametrize(("hour", "minute"), COMPLETE_BOUNDARY_TIMES)
@pytest.mark.parametrize("live", [0, 1])
def test_minute_at_1130_serves_complete_morning_from_local(clock, hour, minute, live) -> None:
    """11:30 整: 应有 120 根, 本地完整上午直接用。"""
    set_time, fetch, _ = clock
    set_time(hour, minute)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL, "live": live}).json()

    assert (body["source"], len(body["rows"])) == ("local", 120)
    fetch.assert_not_called()


@pytest.mark.parametrize(("hour", "minute"), STALE_AFTER_BOUNDARY_TIMES)
def test_minute_after_1130_morning_is_stale_and_pulls(clock, hour, minute) -> None:
    """11:31 起 (美股无午休): 120 根已落后, 走实时拉取 (拉空 → rows 与本地一致但 source 非 local)。"""
    set_time, fetch, _ = clock
    set_time(hour, minute)

    body = _client().get("/api/kline/minute", params={"symbol": SYMBOL}).json()

    fetch.assert_called()


@pytest.mark.parametrize(("hour", "minute", "bars"), [(10, 30, 60), (13, 30, 240), (15, 30, 360)])
def test_minute_trading_session_expected_unchanged(clock, hour, minute, bars) -> None:
    """连续交易时段的估算 (美股单段): 本地根数跟上时刻即用本地。"""
    set_time, fetch, _ = clock
    set_time(hour, minute)

    body = _client(bars).get("/api/kline/minute", params={"symbol": SYMBOL}).json()

    assert (body["source"], len(body["rows"])) == ("local", bars)
    fetch.assert_not_called()


def test_minute_midday_still_pulls_when_morning_incomplete(clock) -> None:
    """中午时本地上午明显不全 (只有 60 根) 仍走实时拉取。"""
    set_time, fetch, _ = clock
    set_time(11, 45)

    _client(60).get("/api/kline/minute", params={"symbol": SYMBOL})

    fetch.assert_called_once()


# ---------- /minute-batch (自选列表分时小图) ----------


def _batch_request(bars: int = 120) -> MagicMock:
    repo = MagicMock()
    repo.get_etf_symbol_set.return_value = set()
    repo.get_minute_batch.side_effect = (
        lambda syms, day, asset_type="stock": _morning(bars=bars) if day == TODAY else pl.DataFrame()
    )
    capset = MagicMock()
    capset.has.return_value = True
    capset.limits.return_value = None
    request = MagicMock()
    request.app.state.repo = repo
    request.app.state.capabilities = capset
    request.headers = {}
    return request


@pytest.mark.parametrize(("hour", "minute"), BATCH_COMPLETE_TIMES)
def test_minute_batch_at_1130_serves_complete_morning_from_local(clock, hour, minute) -> None:
    set_time, _, sync = clock
    set_time(hour, minute)

    payload = kline_api.get_minute_batch(_batch_request(), {"symbols": [SYMBOL]})

    assert len(payload["data"].get(SYMBOL, [])) == 120
    sync.assert_not_called()


@pytest.mark.parametrize(("hour", "minute"), BATCH_STALE_TIMES)
def test_minute_batch_after_1130_morning_is_stale_and_pulls(clock, hour, minute) -> None:
    set_time, _, sync = clock
    set_time(hour, minute)

    kline_api.get_minute_batch(_batch_request(), {"symbols": [SYMBOL]})

    sync.assert_called()


@pytest.mark.parametrize(("hour", "minute", "bars"), [(10, 30, 60), (13, 30, 240), (15, 30, 360)])
def test_minute_batch_trading_session_expected_unchanged(clock, hour, minute, bars) -> None:
    set_time, _, sync = clock
    set_time(hour, minute)

    payload = kline_api.get_minute_batch(_batch_request(bars), {"symbols": [SYMBOL]})

    assert len(payload["data"].get(SYMBOL, [])) == bars
    sync.assert_not_called()


def test_minute_batch_midday_still_pulls_when_morning_incomplete(clock) -> None:
    set_time, _, sync = clock
    set_time(11, 45)

    kline_api.get_minute_batch(_batch_request(60), {"symbols": [SYMBOL]})

    sync.assert_called_once()
