"""Real-rule replay must be causal and must not let missing data change outcomes silently."""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest

import scripts.replay_setups_realistic as replay
from app.indicators.pipeline import _apply_adj_factor
from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity import engine
from app.opportunity.card import CardState, OpportunityCard

D0, D1, D2, D3 = map(date.fromisoformat, ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"])


@pytest.fixture(autouse=True)
def weekday_calendar():
    with patch.object(us_clock, "_calendar_provider", None), patch.object(us_clock, "_cal_cache", {}):
        yield


def card(symbol="TEST"):
    return OpportunityCard(id=f"{symbol}-card", symbol=symbol, setup_type="breakout", created_date=D0,
                           state=CardState.OPEN, entry_zone=(100.0, 102.0), invalidation=95.0,
                           target_zone=(115.0, 120.0), risk_per_share=6.0, reward_risk=3.17, max_holding_days=7)


def bars(day, seq, symbol="TEST"):
    return pl.DataFrame([{"symbol": symbol, "datetime": datetime.fromisoformat(f"{day}T{t}"),
                          "open": float(o), "close": float(c)} for t, o, c in seq])


class FixedMinutes:
    def __init__(self, frames):
        self.frames = frames

    def day(self, symbol, day):
        return self.frames.get(day, pl.DataFrame())


def run(rows, frames, days, c=None):
    return replay.simulate_card(c or card(), pl.DataFrame(rows), FixedMinutes(frames), days, Account())


def test_a_minute_close_is_only_usable_after_the_bar_ends():
    """15:30 bar closes at 15:31, which is after the 15:30 entry cutoff: no fill is possible."""
    f = bars(D1, [("09:30:00", 101, 101), ("15:29:00", 101, 101), ("15:30:00", 101, 101)])
    result = run([{"date": D1, "close": 101.0}], {D1: f}, [D1])
    assert result.status == "no_fill"


def test_missing_daily_close_while_holding_marks_the_trade_incomplete():
    f = {
        D1: bars(D1, [("09:30:00", 101, 101), ("09:45:00", 101, 101), ("09:46:00", 101, 101), ("15:59:00", 101, 101)]),
        D2: bars(D2, [("09:30:00", 101, 101), ("09:45:00", 101, 90), ("15:59:00", 90, 90)]),
        D3: bars(D3, [("09:30:00", 90, 90), ("09:45:00", 90, 120)]),
    }
    rows = [{"date": D1, "close": 101.0}, {"date": D3, "close": 120.0}]   # D2 official close missing
    result = run(rows, f, [D1, D2, D3])
    assert result.status == "incomplete_data"
    assert result.r_multiple is None


def test_missing_minutes_in_the_entry_window_is_not_a_no_fill():
    days = [D1, D2, D3, date(2026, 9, 25), date(2026, 9, 28)]
    result = run([{"date": d, "close": 101.0} for d in days], {}, days)
    assert result.status == "no_minute_data"


def test_non_finite_opening_price_blocks_entry():
    f = bars(D1, [("09:30:00", float("nan"), 101), ("09:45:00", 101, 101), ("09:46:00", 101, 101),
                  ("09:47:00", 101, 120)])
    result = run([{"date": D1, "close": 120.0}], {D1: f}, [D1])
    assert result.status == "no_fill"


def test_an_empty_minute_response_is_retried_not_cached_as_final(tmp_path):
    class Provider:
        calls = 0

        def get_minute(self, *_):
            self.calls += 1
            return pl.DataFrame() if self.calls == 1 else bars(D1, [("09:30:00", 101, 101)])
    provider = Provider()
    source = replay.MinuteSource(provider, Path(tmp_path))
    source.fetch_many({"TEST": {D1}})
    source.fetch_many({"TEST": {D1}})
    assert provider.calls == 2
    assert source.day("TEST", D1).height == 1


def test_point_in_time_adjustment_ignores_future_splits():
    dates = [D0 - timedelta(days=129 - i) for i in range(130)]
    raw = pl.DataFrame({"symbol": ["TEST"] * 130, "date": dates, "open": [10.0] * 130, "high": [10.2] * 130,
                        "low": [9.8] * 130, "close": [10.0] * 130, "volume": [3_000_000.0] * 130})
    factors = pl.DataFrame({"symbol": ["TEST"], "trade_date": [date(2027, 1, 4)], "ex_factor": [10.0],
                            "vol_factor": [10.0]})
    as_of = replay.point_in_time_adjust(raw, factors, D0)
    assert as_of["close"][-1] == pytest.approx(10.0)
    assert engine.universe_filter(as_of, engine.load_params())


def test_minute_prices_stay_as_traded_so_size_and_fees_match_real_trading(tmp_path):
    """The old replay multiplied minutes by a full-sample (future-aware) adjustment ratio, so a later
    10:1 split shrank prices 10x and inflated share counts and per-share fees. Minutes and point-in-time
    card prices must both be as-traded, independent of whether a future split exists."""
    bars(D1, [("09:30:00", 101, 101)]).write_parquet(tmp_path / f"TEST_{D1}.parquet")
    source = replay.MinuteSource(provider=None, cache_dir=tmp_path)
    assert source.day("TEST", D1)["close"][0] == pytest.approx(101.0)
    dates = [D1 - timedelta(days=129 - i) for i in range(130)]
    raw = pl.DataFrame({"symbol": ["TEST"] * 130, "date": dates, "open": [101.0] * 130, "high": [102.0] * 130,
                        "low": [100.0] * 130, "close": [101.0] * 130, "volume": [3_000_000.0] * 130})
    future = pl.DataFrame({"symbol": ["TEST"], "trade_date": [D1 + timedelta(days=30)], "ex_factor": [10.0],
                           "vol_factor": [10.0]})
    with_future = replay.point_in_time_adjust(raw, future, D1)
    without = replay.point_in_time_adjust(raw, future.head(0), D1)
    assert with_future.select("close", "volume").equals(without.select("close", "volume"))


def test_relative_strength_population_matches_production_liquidity_gate():
    dates = [D0 - timedelta(days=199 - i) for i in range(200)]

    def frame(final, volume):
        c = np.linspace(100.0, final, 200)
        return pl.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * .99, "close": c,
                             "volume": [float(volume)] * 200})
    good, illiquid = frame(110, 1_000_000), frame(150, 100_000)
    params = engine.load_params()
    pool = {"GOOD": good, **{f"LOW{i}": illiquid for i in range(9)}}
    liquid = replay.production_liquid_pool(pool, params, D0)
    assert set(liquid) == {"GOOD"}


def test_confidence_interval_clusters_same_day_trades():
    trades = [replay.TradeResult(f"c{i}", "X", "breakout", "2026-09-01", "closed", entry_date="2026-09-02",
                                 r_multiple=r) for i, r in enumerate([2.0, 2.0, -1.0, 1.0] * 25)]
    for i, t in enumerate(trades):
        t.entry_date = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"][i % 4]
    summary = replay.summarize(trades)["ALL"]
    lo, hi = summary["ci95_day_clustered"]
    assert hi - lo > 1.0, "100 trades on 4 distinct days must not look like 100 independent samples"
    assert summary["independent_days"] == 4
