"""Snapshot contracts: delayed consolidated research / timely IEX paper prices."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import polars as pl
import pytest

from app.market_clock import us_clock
from app.plugins.alpaca import provider as alp


NOW = datetime(2026, 9, 29, 11, 0, tzinfo=us_clock.tz)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def snapshot(at=None):
    at = at or NOW - timedelta(minutes=15)
    return {
        "latestTrade": {"p": 101.0, "t": iso(at)},
        "minuteBar": {"c": 100.5, "t": iso(at - timedelta(seconds=30))},
        "dailyBar": {"t": iso(at.replace(hour=0, minute=0, second=0)),
                     "o": 99.0, "h": 102.0, "l": 98.0, "c": 101.0,
                     "v": 1_000_000, "vw": 100.0},
        "prevDailyBar": {"c": 98.0},
    }


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("test", "test"))
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    return alp.AlpacaProvider()


def test_realtime_uses_delayed_sip_and_1000_symbol_batches(provider, monkeypatch):
    symbols = [f"T{i}" for i in range(2101)]
    monkeypatch.setattr(provider, "instruments_df", lambda: pl.DataFrame({"symbol": symbols}))
    calls = []

    def get(url, *args, **kw):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        return {sym: snapshot() for sym in query["symbols"][0].split(",")}

    monkeypatch.setattr(alp, "_get", get)
    rows = provider.get_realtime()
    assert len(rows) == len(symbols)
    assert [len(q["symbols"][0].split(",")) for q in calls] == [1000, 1000, 101]
    assert all(q["feed"] == ["delayed_sip"] for q in calls)
    assert provider.config.datasets["realtime"].batch == 1000
    row = rows[0]
    assert row["feed"] == row["volume_feed"] == "delayed_sip"
    assert row["quote_ts"] == row["timestamp"] == int((NOW - timedelta(minutes=15)).timestamp() * 1000)
    assert row["volume_ts"] == row["quote_ts"]
    assert row["data_delay"] == 900  # seconds, not wall-clock age
    assert row["volume"] == 1_000_000


def test_delayed_sip_failure_falls_back_to_price_only_iex(provider, monkeypatch):
    calls = []

    def get(url, *args, **kw):
        calls.append(url)
        if "feed=delayed_sip" in url:
            raise RuntimeError("upstream unavailable")
        return {"AAPL": snapshot(NOW - timedelta(seconds=10))}

    monkeypatch.setattr(alp, "_get", get)
    rows = provider.get_realtime_indices(["AAPL"])
    assert len(calls) == 2
    assert "feed=delayed_sip" in calls[0] and "feed=iex" in calls[1]
    row = rows[0]
    assert row["last_price"] == 101.0
    assert row["feed"] == "iex"
    assert row["volume"] is None and row["amount"] is None
    assert row["volume_status"] == "iex_volume_disabled"
    assert row["data_delay"] == 0


def test_bad_snapshots_never_get_request_time_as_source_time(provider, monkeypatch):
    missing = snapshot()
    missing["latestTrade"].pop("t")
    missing.pop("minuteBar")
    stale = snapshot(NOW - timedelta(days=1))
    monkeypatch.setattr(alp, "_get", lambda *a, **k: {"MISSING": missing, "OLD": stale, "VOID": None})
    rows = {r["symbol"]: r for r in provider._snapshot_records(["MISSING", "OLD", "VOID"])}
    assert rows["MISSING"]["timestamp"] is None
    assert rows["MISSING"]["last_price"] is None
    assert rows["MISSING"]["quote_status"] == "missing_source_time"
    assert rows["OLD"]["timestamp"] == int((NOW - timedelta(days=1)).timestamp() * 1000)
    assert rows["OLD"]["quote_status"] == "stale"
    assert rows["OLD"]["volume"] is None
    assert "VOID" not in rows


def test_daily_bar_day_cannot_borrow_new_trade_time(provider, monkeypatch):
    snap = snapshot()
    snap["dailyBar"]["t"] = iso(NOW - timedelta(days=1))
    monkeypatch.setattr(alp, "_get", lambda *a, **k: {"AAPL": snap})
    row = provider._snapshot_records(["AAPL"])[0]
    assert row["volume"] is None and row["volume_ts"] is None
    assert row["open"] is None
    assert row["volume_status"] == "daily_date_mismatch"


def test_execution_quotes_only_targeted_iex_fresh_trades_and_verified_open(provider, monkeypatch):
    samples = {"OK": snapshot(NOW - timedelta(seconds=10)),
               "OLD": snapshot(NOW - timedelta(minutes=3)),
               "FUTURE": snapshot(NOW + timedelta(seconds=1)),
               "MISSING": snapshot(), "NAN": snapshot(NOW)}
    samples["MISSING"]["latestTrade"].pop("t")
    samples["NAN"]["latestTrade"]["p"] = float("nan")
    calls = []

    def get(url, *args, **kw):
        calls.append(url)
        assert "feed=iex" in url
        if "/snapshots" in url:
            return deepcopy(samples)
        return {"bars": {"OK": [{"t": "2026-09-29T13:30:00Z", "o": 99.0}]}}

    monkeypatch.setattr(alp, "_get", get)
    monkeypatch.setattr(provider, "get_instruments", lambda *a: pytest.fail("must not fetch universe"))
    rows = provider.get_execution_quotes(list(samples))
    assert [r["symbol"] for r in rows] == ["OK"]
    row = rows[0]
    assert row["price"] == 101.0 and row["feed"] == "iex"
    assert row["timestamp"] == int((NOW - timedelta(seconds=10)).timestamp() * 1000)
    assert row["open"] == 99.0
    assert row["open_timestamp"] == int(datetime(2026, 9, 29, 9, 30, tzinfo=us_clock.tz).timestamp() * 1000)
    assert "volume" not in row
    assert len(calls) == 2
    provider.get_execution_quotes(["OK"])
    assert len(calls) == 3  # verified first-minute open cached by session, never the latest price
    assert provider.get_execution_quotes([]) == []


def test_execution_open_not_fabricated_from_daily_date(provider, monkeypatch):
    def get(url, *args, **kw):
        if "/snapshots" in url:
            return {"AAPL": snapshot(NOW)}
        return {"bars": {"AAPL": [{"t": "2026-09-29T13:31:00Z", "o": 99.0}]}}
    monkeypatch.setattr(alp, "_get", get)
    row = provider.get_execution_quotes(["AAPL"])[0]
    assert row["open"] is None and row["open_timestamp"] is None


def test_source_timestamp_must_have_explicit_timezone():
    assert alp._rfc3339_ms("2026-09-29T11:00:00") is None


def test_thin_stock_official_open_falls_back_to_sip_first_minute_after_delay(provider, monkeypatch):
    """冷门股 IEX 09:30 无成交 → 09:46 后取 SIP 09:30 分钟根 (全市场官方口径) 作开盘价。"""
    calls = []

    def get(url, *args, **kw):
        calls.append(url)
        if "/snapshots" in url:
            return {"TH": snapshot(NOW - timedelta(seconds=5))}
        if "feed=iex" in url:
            return {"bars": {"TH": [{"t": "2026-09-29T13:34:00Z", "o": 19.85}]}}
        assert "feed=sip" in url
        return {"bars": {"TH": [{"t": "2026-09-29T13:30:00Z", "o": 19.64}]}}

    monkeypatch.setattr(alp, "_get", get)
    row = provider.get_execution_quotes(["TH"])[0]
    assert row["open"] == 19.64
    assert row["open_timestamp"] == int(datetime(2026, 9, 29, 9, 30, tzinfo=us_clock.tz).timestamp() * 1000)
    assert row["open_source"] == "sip"
    n = len(calls)
    provider.get_execution_quotes(["TH"])
    assert len(calls) == n + 1  # SIP open cached for the session; only the snapshot is re-fetched


def test_sip_open_fallback_waits_for_sip_delay_window(provider, monkeypatch):
    early = datetime(2026, 9, 29, 9, 40, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: early)

    def get(url, *args, **kw):
        if "/snapshots" in url:
            return {"TH": snapshot(early - timedelta(seconds=5))}
        assert "feed=sip" not in url, "SIP recent window is not queryable before 09:46"
        return {"bars": {}}

    monkeypatch.setattr(alp, "_get", get)
    row = provider.get_execution_quotes(["TH"])[0]
    assert row["open"] is None


def test_official_opens_do_not_require_a_fresh_trade(provider, monkeypatch):
    """TickFlow 主源下只向 Alpaca 取官方开盘价: 冷门股即使一小时没有 IEX 成交也要能拿到。"""
    def get(url, *args, **kw):
        assert "/snapshots" not in url
        if "feed=iex" in url:
            return {"bars": {}}
        return {"bars": {"TH": [{"t": "2026-09-29T13:30:00Z", "o": 19.64}]}}
    monkeypatch.setattr(alp, "_get", get)
    got = provider.official_opens(["TH"])
    assert got["TH"][0] == 19.64 and got["TH"][2] == "sip"
