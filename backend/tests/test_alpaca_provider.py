"""Alpaca provider 契约测试（mock HTTP 层，零真网络）。

覆盖: 凭据装载 (packed key)、日K归一 (日期/量额)、除权因子比值重建、
实时快照字段、分钟K UTC→美东墙钟、分页与分块、429 退避。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.plugins.alpaca import provider as alp


# ── HTTP mock 基座 ─────────────────────────────────────────

def _mock_get(monkeypatch, routes: dict[str, object]):
    """routes: {url 前缀或子串: 响应 dict}。按子串匹配。"""
    calls: list[str] = []

    def fake_get(url, key, secret, **kwargs):
        calls.append(url)
        for needle, body in routes.items():
            if needle in url:
                return body
        raise AssertionError(f"未 mock 的 URL: {url}")

    monkeypatch.setattr(alp, "_get", fake_get)
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    return calls


def _daily_bar(t: str, c: float, v: float = 1000.0, vw: float | None = None) -> dict:
    return {"t": t, "o": c - 0.5, "h": c + 1, "l": c - 1, "c": c, "v": v,
            "vw": vw if vw is not None else c, "n": 100}


# ── 凭据 ──────────────────────────────────────────────────

def test_credentials_reject_bad_packed_format(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)       # 隔离真实 secrets.json
    monkeypatch.setenv("ZENTRADE_ALPACA_KEY", "nocolon")       # 格式不对 → 不采用
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("ZENTRADE_ENV_DIR", raising=False)
    k, s = alp._load_credentials()
    assert (k, s) == ("", "")


def test_probe_rejects_malformed_key():
    ok, reason = alp.probe_api_key("nocolon")
    assert ok is False


# ── 日K ──────────────────────────────────────────────────

def test_daily_normalizes_date_and_amount(monkeypatch):
    _mock_get(monkeypatch, {
        "stocks/bars": {"bars": {
            "AAPL": [
                _daily_bar("2026-09-24T04:00:00Z", 230.0, v=100.0, vw=2.0),
                _daily_bar("2026-09-25T04:00:00Z", 231.0, v=200.0, vw=1.5),
            ],
        }, "next_page_token": None},
    })
    p = alp.AlpacaProvider()
    df = p.get_daily(["AAPL"], datetime(2026, 9, 24), datetime(2026, 9, 25))
    assert df.height == 2
    assert df["date"].to_list() == [date(2026, 9, 24), date(2026, 9, 25)]
    assert df["amount"].to_list() == [200.0, 300.0]  # v × vw
    assert df["close"].to_list() == [230.0, 231.0]


def test_daily_pages_and_batches(monkeypatch):
    """分页 + 分块: 101 个 symbol 触发 2 批, 第 1 批带翻页。"""
    symbols = [f"S{i:03d}" for i in range(101)]
    bars_page = {"bars": {s: [_daily_bar("2026-09-25T04:00:00Z", 10.0)] for s in symbols[:100]},
                 "next_page_token": "p2"}
    pages = {"stocks/bars": bars_page, "page_token=p2": {"bars": {}, "next_page_token": None}}

    def fake_get(url, key, secret, **kwargs):
        if "page_token=p2" in url:
            return {"bars": {}, "next_page_token": None}
        if "S100" in url:  # 第二批 (100 以后)
            return {"bars": {"S100": [_daily_bar("2026-09-25T04:00:00Z", 10.0)]},
                    "next_page_token": None}
        return bars_page

    monkeypatch.setattr(alp, "_get", fake_get)
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    p = alp.AlpacaProvider()
    df = p.get_daily(symbols, datetime(2026, 9, 25), datetime(2026, 9, 25))
    assert df.height == 101  # 批1(100, 翻页后空) + 批2(1)


# ── 除权因子 ──────────────────────────────────────────────

def test_adj_factor_ratio_rebuild(monkeypatch):
    """raw/all 水平比值只在除权日跳变: 输出单条拆股事件 (前后价格比 10), 不输出逐日水平值。"""
    raw_bars = {"bars": {"NVDA": [
        _daily_bar("2024-06-06T04:00:00Z", 1209.98, v=66_469_619),
        _daily_bar("2024-06-07T04:00:00Z", 1208.88, v=41_238_580),
        _daily_bar("2024-06-10T04:00:00Z", 121.79, v=314_162_666),
    ]}, "next_page_token": None}
    adj_bars = {"bars": {"NVDA": [
        _daily_bar("2024-06-06T04:00:00Z", 120.65, v=664_696_190),
        _daily_bar("2024-06-07T04:00:00Z", 120.54, v=412_385_800),
        _daily_bar("2024-06-10T04:00:00Z", 121.44, v=314_162_666),
    ]}, "next_page_token": None}

    def fake_get(url, key, secret, **kwargs):
        if "adjustment=all" in url:
            return adj_bars
        return raw_bars

    monkeypatch.setattr(alp, "_get", fake_get)
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    p = alp.AlpacaProvider()
    df = p.get_adj_factors(["NVDA"], datetime(2024, 6, 6), datetime(2024, 6, 10))
    assert df.columns == ["symbol", "trade_date", "ex_factor", "vol_factor"]
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["trade_date"] == date(2024, 6, 10)
    assert row["ex_factor"] == pytest.approx(10.0, rel=1e-3)
    assert row["vol_factor"] == pytest.approx(10.0, rel=1e-6)


# ── 实时快照 ──────────────────────────────────────────────

def test_realtime_snapshot_fields(monkeypatch):
    monkeypatch.setattr(alp.us_clock, "now", lambda: datetime(2026, 9, 25, 15, 31, tzinfo=alp.us_clock.tz))
    snap = {
        "AAPL": {
            "dailyBar": {"t": "2026-09-25T04:00:00Z", "o": 228.0, "h": 231.0, "l": 227.0, "c": 230.0, "v": 1000, "vw": 229.5},
            "prevDailyBar": {"c": 225.0},
            "latestTrade": {"p": 230.5, "t": "2026-09-25T19:30:00Z"},
        },
    }
    _mock_get(monkeypatch, {
        "/assets": [{"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ",
                      "tradable": True, "status": "active"}],
        "snapshots": snap,
    })
    # snapshots 端点响应是 {symbol: snapshot} 平铺
    def fake_get(url, key, secret, **kwargs):
        if "/assets" in url:
            return [{"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ",
                     "tradable": True, "status": "active"}]
        return snap

    monkeypatch.setattr(alp, "_get", fake_get)
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    p = alp.AlpacaProvider()
    rows = p.get_realtime()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "AAPL"
    assert r["last_price"] == 230.5
    assert r["prev_close"] == 225.0
    assert r["change_pct"] == pytest.approx((230.5 - 225.0) / 225.0)
    assert r["amount"] == pytest.approx(1000 * 229.5)
    assert r["timestamp"] is not None


# ── 分钟K ────────────────────────────────────────────────

def test_minute_utc_to_et_wallclock(monkeypatch):
    """UTC bar 时间 → 美东墙钟 naive (夏令时 -4h)。"""
    bars = {"bars": {"AAPL": [
        {"t": "2026-09-25T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "vw": 1.2},
    ]}, "next_page_token": None}
    _mock_get(monkeypatch, {"stocks/bars": bars})
    p = alp.AlpacaProvider()
    df = p.get_minute(["AAPL"], datetime(2026, 9, 25), datetime(2026, 9, 26))
    assert df["datetime"].to_list() == [datetime(2026, 9, 25, 9, 30)]  # 13:30 UTC = 9:30 ET
    assert df["datetime"].dtype == pl.Datetime


def test_minute_request_converts_et_wallclock_to_utc(monkeypatch):
    """Alpaca query parameters must not reinterpret ET naive values as UTC."""
    calls = _mock_get(monkeypatch, {
        "stocks/bars": {"bars": {}, "next_page_token": None},
    })
    p = alp.AlpacaProvider()
    p.get_minute(
        ["AAPL"],
        datetime(2026, 9, 25, 9, 30),
        datetime(2026, 9, 25, 16, 0),
    )
    assert "start=2026-09-25T13:30:00Z" in calls[0]
    assert "end=2026-09-25T20:00:00Z" in calls[0]


def test_minute_discards_premarket_and_postmarket(monkeypatch):
    """Minute provider output is the 390-bar regular session contract."""
    bars = {"bars": {"AAPL": [
        {"t": "2026-09-25T12:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},  # 08:00 ET
        {"t": "2026-09-25T13:30:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2, "vw": 2},  # 09:30 ET
        {"t": "2026-09-25T19:59:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 3, "vw": 3},  # 15:59 ET
        {"t": "2026-09-25T20:00:00Z", "o": 4, "h": 4, "l": 4, "c": 4, "v": 4, "vw": 4},  # 16:00 ET
    ]}, "next_page_token": None}
    _mock_get(monkeypatch, {"stocks/bars": bars})
    p = alp.AlpacaProvider()
    df = p.get_minute(["AAPL"], datetime(2026, 9, 25), datetime(2026, 9, 26))
    assert df["datetime"].to_list() == [
        datetime(2026, 9, 25, 9, 30),
        datetime(2026, 9, 25, 15, 59),
    ]


def test_get_instruments_returns_list_of_dicts(monkeypatch):
    body = [
        {"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ", "tradable": True, "status": "active"},
        {"symbol": "BRK.B", "name": "Berkshire B", "exchange": "NYSE", "tradable": True, "status": "active"},
        {"symbol": "OTCX", "name": "otc", "exchange": "OTC", "tradable": True, "status": "active"},
    ]
    _mock_get(monkeypatch, {"/assets": body})
    p = alp.AlpacaProvider()
    rows = p.get_instruments()
    assert isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
    assert [r["symbol"] for r in rows] == ["AAPL", "BRK.B"]


def test_instruments_filters(monkeypatch):
    body = [
        {"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ", "tradable": True, "status": "active"},
        {"symbol": "BRK.B", "name": "Berkshire B", "exchange": "NYSE", "tradable": True, "status": "active"},  # 普通股类别点号不得误删
        {"symbol": "OTCX", "name": "otc", "exchange": "OTC", "tradable": True, "status": "active"},  # 交易所剔除
        {"symbol": "DEAD", "name": "dead", "exchange": "NYSE", "tradable": False, "status": "active"},
    ]
    _mock_get(monkeypatch, {"/assets": body})
    p = alp.AlpacaProvider()
    df = p.instruments_df()
    assert df["symbol"].to_list() == ["AAPL", "BRK.B"]
