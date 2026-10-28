"""Feed/time isolation for consolidated-volume research signals."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services.quote_service import QuoteService

NOW = datetime(2026, 9, 29, 11, 0, tzinfo=us_clock.tz)


def ms(at):
    return int(at.timestamp() * 1000)


def record(symbol="AAPL", *, feed="delayed_sip", age=900, volume=100_000, **overrides):
    ts = ms(NOW - timedelta(seconds=age))
    return {"symbol": symbol, "last_price": 100.0, "open": 99.0, "high": 102.0, "low": 98.0,
            "volume": volume, "amount": volume * 100, "timestamp": ts, "quote_ts": ts,
            "volume_ts": ts, "feed": feed, "quote_feed": feed, "volume_feed": feed,
            "historical_volume_feed": "sip", "data_delay": 900 if feed == "delayed_sip" else 0,
            **overrides}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)


def test_daily_frame_preserves_source_contract_and_blanks_iex_volume():
    df = QuoteService._build_daily([record(), record("IEX", feed="iex", age=10)])
    rows = {r["symbol"]: r for r in df.to_dicts()}
    assert set(rows) == {"AAPL", "IEX"}
    assert rows["AAPL"]["feed"] == "delayed_sip"
    assert rows["AAPL"]["volume_ts"] == ms(NOW - timedelta(minutes=15))
    assert rows["IEX"]["volume"] is None and rows["IEX"]["amount"] is None
    assert rows["IEX"]["volume_status"] == "iex_volume_disabled"


@pytest.mark.parametrize("changes", [{"quote_ts": None, "timestamp": None},
    {"quote_ts": ms(NOW + timedelta(seconds=1)), "timestamp": ms(NOW + timedelta(seconds=1))},
    {"quote_ts": ms(NOW - timedelta(days=1)), "timestamp": ms(NOW - timedelta(days=1))}])
def test_explicit_feed_missing_future_or_old_price_cannot_be_written_today(changes):
    assert QuoteService._build_daily([record(**changes)]).is_empty()


@pytest.mark.parametrize("overrides, status", [
    ({"volume_feed": "iex"}, "feed_mismatch"),
    ({"quote_feed": "iex"}, "feed_mismatch"),
    ({"historical_volume_feed": "iex"}, "history_feed_mismatch"),
    ({"volume_ts": None}, "missing_volume_time"),
    ({"volume_ts": ms(NOW - timedelta(days=1))}, "stale_volume"),
])
def test_volume_contract_fails_closed_for_mixed_feed_or_missing_time(overrides, status):
    row = QuoteService._build_daily([record(**overrides)]).to_dicts()[0]
    assert row["volume"] is None
    assert row["volume_status"] == status


def test_volume_ratio_uses_each_symbols_volume_time_not_quote_or_wall_clock():
    daily = QuoteService._build_daily([record("A", age=900), record("B", age=900, volume_ts=ms(NOW - timedelta(seconds=960)))])
    base = daily.with_columns(pl.lit(0.5).alias("vol_ratio_5d"), pl.lit(False).alias("signal_volume_surge"))
    result = QuoteService._apply_volume_contract(base, daily)
    rows = {r["symbol"]: r for r in result.to_dicts()}
    assert rows["A"]["vol_ratio_5d"] == pytest.approx(0.5 * us_clock.total_minutes / 75)
    assert rows["B"]["vol_ratio_5d"] == pytest.approx(0.5 * us_clock.total_minutes / 74)
    assert rows["A"]["signal_volume_surge"] is True


def test_iex_cannot_leave_prior_volume_surge_or_ratio_in_enriched():
    daily = QuoteService._build_daily([record(feed="iex", age=10)])
    base = daily.with_columns(pl.lit(8.0).alias("vol_ratio_5d"), pl.lit(True).alias("signal_volume_surge"),
                             pl.lit(999.0).alias("vol_ma5"), pl.lit(999.0).alias("vol_ma10"))
    row = QuoteService._apply_volume_contract(base, daily).to_dicts()[0]
    assert row["vol_ratio_5d"] is None and row["signal_volume_surge"] is False
    assert row["vol_ma5"] is None and row["vol_ma10"] is None


def service():
    svc = QuoteService.__new__(QuoteService)
    svc._prev_stock_volume = None
    svc._prev_volume_fetched_at = None
    svc._prev_volume_date = None
    svc._volume_delta = {}
    svc._volume_delta_span_s = 0
    return svc


def test_volume_delta_source_span_does_not_use_fast_polling_time():
    svc = service()
    first = [record(volume=1000, age=960)]
    second = [record(volume=2000)]
    svc._update_volume_delta(first, ms(NOW))
    svc._update_volume_delta(second, ms(NOW) + 6000)
    result = svc._inject_volume_delta(pl.DataFrame({"symbol": ["AAPL"]})).to_dicts()[0]
    assert result["_volume_delta"] == 1000
    assert result["_volume_delta_span"] == 60  # source snapshots advanced one minute, polling advanced 6s


@pytest.mark.parametrize("feed", ["iex", "sip", "delayed_sip"])
def test_volume_delta_never_bridges_feed_changes_or_duplicate_source_times(feed):
    svc = service()
    svc._update_volume_delta([record(volume=1000, age=910)], ms(NOW))
    svc._update_volume_delta([record(volume=2000, feed=feed, age=910 if feed == "delayed_sip" else 10)], ms(NOW) + 6000)
    assert svc._volume_delta == {}


def test_iex_delta_disabled_even_if_volume_is_supplied():
    svc = service()
    svc._update_volume_delta([record(feed="iex", age=60, volume=1000)], ms(NOW))
    svc._update_volume_delta([record(feed="iex", age=10, volume=100_000)], ms(NOW) + 6000)
    assert svc._volume_delta == {}


def test_full_fallback_enriched_keeps_iex_price_but_not_volume_signal(tmp_path):
    from app.storage.repository import DataStore, KlineRepository
    repo = KlineRepository(DataStore(tmp_path))
    day = NOW.date() - timedelta(days=20)
    while day < NOW.date():
        if day.weekday() < 5:
            df = pl.DataFrame({"symbol": ["AAPL", "IEX"], "date": [day, day], "open": [99., 99.],
                "high": [102., 102.], "low": [98., 98.], "close": [100., 100.],
                "volume": [100_000., 100_000.], "amount": [10_000_000., 10_000_000.]})
            repo.flush_live_daily(df)
        day += timedelta(days=1)
    # Force actual full pipeline rather than incremental path.
    repo.get_live_agg = lambda: pl.DataFrame()
    repo.get_enriched_latest = lambda: (pl.DataFrame(), None)
    svc = QuoteService()
    svc._repo = repo
    rows = [record(), record("IEX", feed="iex", age=10)]
    svc._flush_live_enriched(svc._build_daily(rows))
    assert repo._enriched_cache is not None
    result = {r["symbol"]: r for r in repo._enriched_cache.to_dicts()}
    assert set(result) == {"AAPL", "IEX"}
    assert result["AAPL"]["vol_ratio_5d"] == pytest.approx(us_clock.total_minutes / 75)
    assert result["IEX"]["raw_close"] == 100
    assert result["IEX"]["vol_ratio_5d"] is None
    assert result["IEX"]["signal_volume_surge"] is False
    assert result["IEX"]["feed"] == "iex"


# 0.12.1: 10-12 七只 SPAC 类实时行 open=上一日开盘 (10.0) 而 H=L=C=10.02 —— 开盘必须是当日值
@pytest.mark.parametrize("open_, expected", [(10.0, 10.02), (None, 10.02), (10.5, 10.02), (10.01, 10.01)])
def test_daily_open_outside_today_range_falls_back_to_close(open_, expected):
    row = QuoteService._build_daily([record(last_price=10.02, open=open_, high=10.02 if open_ != 10.01 else 10.03,
                                            low=10.02 if open_ != 10.01 else 10.0)]).to_dicts()[0]
    assert row["open"] == expected
    assert row["low"] <= min(row["open"], row["close"]) and row["high"] >= max(row["open"], row["close"])


def test_daily_valid_open_untouched():
    row = QuoteService._build_daily([record()]).to_dicts()[0]
    assert (row["open"], row["high"], row["low"], row["close"]) == (99.0, 102.0, 98.0, 100.0)
