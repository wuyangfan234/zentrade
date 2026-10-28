"""个股详情分时轮询的 live 直拉路径测试。

背景: 盘中分钟增量落盘后, 当日本地分区很快达到 90% 完整度,
/api/kline/minute 的本地优先启发式会拦截实时补拉, 详情分时图停在
上一增量轮 (≥60s 滞后)。live=1 让详情轮询在连续竞价时段绕过本地优先。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router
from app.market_time import CN_TZ, in_continuous_session
from app.data_providers.models import Cap, CapabilityLimits, CapabilitySet

# 2026-08-26 是周三; 10:00 处于上午连续竞价, expected(已交易分钟) = 30
_NOW = datetime(2026, 8, 26, 10, 0, tzinfo=CN_TZ)
_TODAY = date(2026, 8, 26)
_LOCAL_ROWS = 30


class _FakeRepo:
    def __init__(self) -> None:
        # /minute 读取 repo.store.data_dir 判断分钟基准标记 (无标记即旧行为)
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        self.store = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame(
            {"symbol": [], "name": [], "total_shares": [], "float_shares": []}
        )

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return pl.DataFrame({"date": [], "close": []})

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 8, 26, 9, 30) + timedelta(minutes=offset)
                for offset in range(_LOCAL_ROWS)
            ],
            "close": [10.0] * _LOCAL_ROWS,
        })


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _FakeRepo()
    app.state.capabilities = CapabilitySet({
        Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits(),
    })
    return TestClient(app)


def _patch_market(monkeypatch, *, in_session: bool) -> None:
    import app.api.kline as kline_api

    monkeypatch.setattr(kline_api.us_clock, "now", lambda: _NOW)
    monkeypatch.setattr(kline_api.us_clock, "today", lambda: _TODAY)
    monkeypatch.setattr(kline_api.us_clock, "in_session", lambda: in_session)
    # 缺省日期路径会问交易日探针; 钉成交易日 (_NOW 是周三), 不打真实行情请求
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda now=None: True)


def _patch_live_fetch(monkeypatch) -> None:
    import app.api.kline as kline_api

    def _fake_fetch(symbol, trade_date, asset_type="stock", *, capset, raw_basis=False):
        return pl.DataFrame({
            "datetime": [datetime(2026, 8, 26, 9, 59)],
            "close": [11.11],
        })

    monkeypatch.setattr(
        kline_api.kline_sync, "fetch_minute_single", _fake_fetch
    )


def test_minute_live_param_bypasses_local_first_during_session(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "JPM", "live": 1}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["rows"][-1]["close"] == 11.11


def test_minute_without_live_keeps_local_first(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "JPM"}
    )

    assert resp.status_code == 200
    body = resp.json()
    # 本地 30 根 >= expected(30)*0.9 → 完整, 走本地
    assert body["source"] == "local"
    assert body["rows"][0]["close"] == 10.0


def test_minute_live_param_falls_back_to_local_off_session(monkeypatch):
    _patch_market(monkeypatch, in_session=False)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "JPM", "live": 1}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "local"


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 29, False),
        (9, 30, True),
        (11, 30, True),   # 美股无午休
        (11, 31, True),
        (12, 30, True),
        (13, 0, True),
        (16, 0, True),
        (16, 1, False),
    ],
)
def test_in_continuous_session_boundaries(hour: int, minute: int, expected: bool, monkeypatch):
    # 隔离真实日历网络: 注入固定交易日集合 (2026-08-26 周三为交易日)
    from datetime import date as _date
    from app.market_clock import us_clock
    monkeypatch.setattr(
        us_clock, "_calendar_provider",
        lambda s, e: [_date(2026, 8, 26)],
    )
    now = datetime(2026, 8, 26, hour, minute, tzinfo=CN_TZ)  # 周三
    assert in_continuous_session(now) is expected


def test_in_continuous_session_rejects_weekend(monkeypatch):
    from datetime import date as _date
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "_calendar_provider", lambda s, e: [_date(2026, 8, 26)])
    assert in_continuous_session(datetime(2026, 8, 29, 10, 0, tzinfo=CN_TZ)) is False


# ── 实时拼接 (TickFlow 5 秒报价 → 最近 16 分钟分钟K) ──────────────

class _FakeStitcher:
    name = "tickflow"

    def __init__(self, bars):
        self.bars = bars
        self.registered = []

    def stitched_minutes(self, symbol, since=None):
        return [b for b in self.bars if since is None or b["datetime"] >= since]

    def watch(self, symbol):
        self.registered.append(symbol)


def _patch_stitcher(monkeypatch, stitcher):
    import app.api.kline as kline_api
    monkeypatch.setattr(kline_api, "_realtime_stitcher", lambda: stitcher)


def test_live_minute_appends_stitched_tail_after_official_bars(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)  # official SIP tail ends 09:59
    st = _FakeStitcher([
        {"symbol": "NVDA", "datetime": datetime(2026, 8, 26, 9, 59), "open": 1, "high": 1, "low": 1, "close": 99.0, "volume": 5, "amount": None, "stitched": True},
        {"symbol": "NVDA", "datetime": datetime(2026, 8, 26, 10, 0), "open": 1, "high": 1, "low": 1, "close": 12.5, "volume": 7, "amount": None, "stitched": True},
    ])
    _patch_stitcher(monkeypatch, st)
    body = _client().get("/api/kline/minute", params={"symbol": "NVDA", "live": 1}).json()
    rows = body["rows"]
    assert rows[-1]["close"] == 12.5 and rows[-1]["stitched"] is True
    # official bar wins where both exist (09:59 official 11.11, not stitched 99.0)
    assert [r["close"] for r in rows if r["datetime"].endswith("09:59:00")] == [11.11]
    assert body["realtime_tail"]["source"] == "tickflow" and body["realtime_tail"]["bars"] == 1
    assert st.registered == ["NVDA"]  # symbol joins the 5-second hot pool while being viewed


def test_live_minute_without_stitcher_is_unchanged(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)
    _patch_stitcher(monkeypatch, None)
    body = _client().get("/api/kline/minute", params={"symbol": "NVDA", "live": 1}).json()
    assert body["rows"][-1]["close"] == 11.11 and body.get("realtime_tail") is None


def test_live_minute_reports_session_and_realtime_source_for_notice(monkeypatch):
    """前端据此决定说明文案: 盘中有实时源=实时; 盘中无实时源=才提示延迟。"""
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)
    _patch_stitcher(monkeypatch, _FakeStitcher([]))  # 刚加入热报价池, 还没有拼接分钟
    body = _client().get("/api/kline/minute", params={"symbol": "NVDA", "live": 1}).json()
    assert body["in_session"] is True and body["realtime_source"] == "tickflow"
    assert body["realtime_tail"] is None

    _patch_stitcher(monkeypatch, None)
    body = _client().get("/api/kline/minute", params={"symbol": "NVDA", "live": 1}).json()
    assert body["in_session"] is True and body["realtime_source"] is None


def test_minute_after_close_is_not_in_session(monkeypatch):
    _patch_market(monkeypatch, in_session=False)
    _patch_live_fetch(monkeypatch)
    _patch_stitcher(monkeypatch, _FakeStitcher([]))
    body = _client().get("/api/kline/minute", params={"symbol": "NVDA", "live": 1}).json()
    assert body["in_session"] is False and body["realtime_source"] is None
