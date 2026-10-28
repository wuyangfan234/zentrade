"""TickFlow plugin contract tests (no network). Formal key permissions (2026-10-01 probe):
quotes/universe quotes/daily/ex-factors/instruments OK; minute/intraday/depth/financials/WS 403."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.plugins.tickflow import provider as tf

NOW = datetime(2026, 10, 1, 11, 0, tzinfo=us_clock.tz)
MS = int(NOW.timestamp() * 1000)


def q(sym, *, price=100.0, ts=MS - 6000, vol=1_000_000, amt=1e8, prev=98.0, open_=99.0):
    return {"symbol": f"{sym}.US", "region": "US", "last_price": price, "prev_close": prev, "open": open_,
            "high": price + 1, "low": open_ - 1, "volume": vol, "amount": amt, "timestamp": ts,
            "ext": {"type": "us_equity", "name": "名称", "change_pct": 0.02, "change_amount": 2.0,
                    "amplitude": 0.03, "turnover_rate": 0.001}}


@pytest.fixture
def prov(monkeypatch):
    monkeypatch.setattr(tf, "_api_key", lambda: "tk_test")
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(tf, "_sleep", lambda s: None)
    return tf.TickFlowProvider()


def test_symbol_mapping_round_trip():
    assert tf.to_tf("BRK.B") == "BRK.B.US" and tf.from_tf("BRK.B.US") == "BRK.B"
    assert tf.from_tf("JPM") is None  # never let non-US rows into the US store


def test_execution_quotes_are_source_stamped_and_tagged(prov, monkeypatch):
    calls = []

    def fake(path, params=None, **kw):
        calls.append((path, params))
        return {"data": [q("NVDA"), q("OLD", ts=MS - 200_000), q("FUT", ts=MS + 5000)]}

    monkeypatch.setattr(prov, "_call", fake)
    monkeypatch.setattr(prov, "_official_opens", lambda symbols: {"NVDA": (99.5, int(datetime(2026, 10, 1, 9, 30, tzinfo=us_clock.tz).timestamp() * 1000), "sip")})
    rows = prov.get_execution_quotes(["NVDA", "OLD", "FUT"])
    assert [r["symbol"] for r in rows] == ["NVDA"]
    r = rows[0]
    assert r["feed"] == "tickflow" and r["price"] == 100.0 and r["timestamp"] == MS - 6000
    assert r["open"] == 99.5 and r["open_source"] == "sip"   # TickFlow quote 'open' is NOT used as official open
    assert calls[0][0] == "/v1/quotes"


def test_execution_quotes_batches_of_50(prov, monkeypatch):
    sizes = []
    monkeypatch.setattr(prov, "_call", lambda path, params=None, **kw: sizes.append(len(params["symbols"].split(","))) or {"data": []})
    monkeypatch.setattr(prov, "_official_opens", lambda symbols: {})
    prov.get_execution_quotes([f"S{i}" for i in range(120)])
    assert sizes == [50, 50, 20]


def test_realtime_universe_rows_follow_internal_contract(prov, monkeypatch):
    monkeypatch.setattr(tf, "_known_symbols", lambda: {"AAPL", "SPY"})
    monkeypatch.setattr(prov, "_call", lambda path, params=None, **kw: {"data": [q("AAPL"), q("SPY"), q("AAPL.WS"), {**q("X"), "symbol": "JPM"}]})
    rows = prov.get_realtime()
    assert {r["symbol"] for r in rows} == {"AAPL", "SPY"}
    a = next(r for r in rows if r["symbol"] == "AAPL")
    assert a["feed"] == "tickflow" and a["volume_status"] == "valid" and a["volume_ts"] == a["timestamp"]
    assert a["volume"] == 1_000_000 and a["amount"] == 1e8  # shares, USD (US contract; no A-share lots)
    assert a["change_pct"] == pytest.approx(0.02) and a["turnover_rate"] == pytest.approx(0.001)
    assert a["data_delay"] == 0 and a["historical_volume_feed"] == "sip"


def test_realtime_soft_fails_to_empty(prov, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429")
    monkeypatch.setattr(prov, "_call", boom)
    assert prov.get_realtime() == []


def test_daily_columnar_batch_to_rows(prov, monkeypatch):
    d0 = int(datetime(2026, 9, 29, tzinfo=us_clock.tz).timestamp() * 1000)
    payload = {"data": {"NVDA.US": {"timestamp": [d0, d0 + 86_400_000], "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5],
                                    "close": [1.5, 2.5], "volume": [10, 20], "amount": [15.0, 50.0]}}}
    seen = []
    monkeypatch.setattr(prov, "_call", lambda path, params=None, **kw: seen.append(params) or payload)
    df = prov.get_daily(["NVDA"], datetime(2026, 9, 29), datetime(2026, 9, 30))
    assert df.columns[:8] == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df["symbol"].to_list() == ["NVDA", "NVDA"]
    assert df["date"].to_list() == [date(2026, 9, 29), date(2026, 9, 30)]
    assert seen[0]["adjust"] == "none" and seen[0]["period"] == "1d"


def test_adj_factors_cumulative_and_handoff_filtered(prov, monkeypatch):
    t = lambda d: int(datetime.combine(d, datetime.min.time(), tzinfo=us_clock.tz).timestamp() * 1000)
    payload = {"data": {"NVDA.US": [{"timestamp": t(date(2024, 6, 10)), "ex_factor": 10.0}],
                        "CALY.US": [{"timestamp": t(date(2023, 9, 1)), "ex_factor": 1.01}]}}
    monkeypatch.setattr(prov, "_call", lambda path, params=None, **kw: payload)
    monkeypatch.setattr(tf, "_handoff_filter", lambda rows: [r for r in rows if r["symbol"] != "CALY"])
    df = prov.get_adj_factors(["NVDA", "CALY"], datetime(2024, 1, 1), datetime(2024, 12, 31))
    assert set(df["symbol"].to_list()) == {"NVDA"}
    assert df.columns == ["symbol", "trade_date", "ex_factor"]


def test_hot_quotes_keep_last_minutes_for_stitching(prov, monkeypatch):
    base = datetime(2026, 10, 1, 10, 0, 5, tzinfo=us_clock.tz)
    seq = [(base + timedelta(seconds=5 * i)) for i in range(30)]  # 10:00:05 .. 10:02:30
    prices = [100 + (i % 7) * 0.1 for i in range(30)]
    vols = [1_000_000 + 1000 * i for i in range(30)]
    for at, p, v in zip(seq, prices, vols):
        prov._record_tick("NVDA", int(at.timestamp() * 1000), p, v)
    bars = prov.stitched_minutes("NVDA")
    assert [b["datetime"].strftime("%H:%M") for b in bars] == ["10:00", "10:01", "10:02"]
    # ticks at 10:00:05..10:00:55 → indices 0..10 (11 ticks); 10:01 → 11..22; 10:02 → 23..29
    b0 = bars[0]
    assert b0["open"] == prices[0] and b0["close"] == prices[10] and b0["high"] == max(prices[:11])
    assert b0["volume"] is None  # first minute has no prior cumulative volume → unknown, not fabricated
    assert bars[1]["volume"] == vols[22] - vols[10]
    assert all(b["stitched"] for b in bars)


def test_probe_api_key_contract(monkeypatch):
    monkeypatch.setattr(tf, "_raw_get", lambda path, key, params=None, timeout=15: {"data": [q("SPY")]})
    assert tf.probe_api_key("tk_ok") == (True, "ok")

    def denied(path, key, params=None, timeout=15):
        raise tf.TickFlowHTTPError(403, "NO_PERMISSION")
    monkeypatch.setattr(tf, "_raw_get", denied)
    ok, reason = tf.probe_api_key("tk_bad")
    assert ok is False and "403" in reason


def test_rate_limiter_spaces_calls(monkeypatch):
    clock = [0.0]
    slept = []
    monkeypatch.setattr(tf.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tf, "_sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    lim = tf.RateLimiter(per_minute=60)
    for _ in range(3):
        lim.acquire()
    assert sum(slept) >= 2 * (60 / 60) * tf.RateLimiter.SAFETY - 1e-9


def test_capability_set_accepts_tickflow_realtime(prov, monkeypatch):
    """PUT /preferences/data-providers rebuilds capabilities from provider.config (prod 500 on 2026-10-01)."""
    from app.data_providers import capabilities as caps
    from app.data_providers.models import Cap
    monkeypatch.setattr(caps.custom_sources, "provider_has_dataset", lambda n, d: n == "tickflow" and d == "realtime")
    monkeypatch.setattr(caps.custom_sources, "get_provider", lambda n: prov)
    cs = caps.current_capability_set({"realtime_data_provider": "tickflow"})
    assert cs.has(Cap.QUOTE_BATCH)
    assert prov.config.datasets["realtime"].rpm == 20
