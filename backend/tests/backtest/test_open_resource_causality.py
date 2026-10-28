"""Opening cash and position slots must not include later-day exits."""
from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions
from app.backtest.matrix import build_market_matrix

START = date(2024, 1, 1)
OPTIONS = SimulationOptions(include_monte_carlo=False, include_return_distribution=False)


def _mask(frame, marks):
    return pl.Series([
        (row["symbol"], (row["date"] - START).days) in marks
        for row in frame.select(["symbol", "date"]).iter_rows(named=True)
    ], dtype=pl.Boolean)


def _run(*, exit_fill, exit_marks, a_close=12.0, b_price=20.0,
         max_positions=1, max_exposure=1.0):
    rows = []
    for symbol, price in (("A", 10.0), ("B", b_price)):
        for day in range(4):
            close = a_close if symbol == "A" and day >= 2 else price
            rows.append({
                "symbol": symbol, "name": symbol, "date": START + timedelta(days=day),
                "open": price, "high": max(price, close), "low": price,
                "close": close, "volume": 100_000,
                "score": 4 if symbol == "A" else 3,
            })
    frame = pl.DataFrame(rows).sort(["symbol", "date"])
    entries = _mask(frame, {("A", 0), ("B", 1)})
    exits = _mask(frame, exit_marks)
    config = MatcherConfig(
        entry_fill="open_t+1", exit_fill=exit_fill,
        fees_pct=0, slippage_bps=0,
        max_positions=max_positions, max_exposure_pct=max_exposure,
        initial_capital=100.0,
    )
    matrix = build_market_matrix(
        frame, entries, exits,
        entry_delay_bars=1,
        exit_delay_bars=1 if exit_fill == "open_t+1" else 0,
    )
    return BacktestEngine(repo=None)._simulate_portfolio_matrix(
        matrix, config, None, None, OPTIONS,
    )


def test_close_exit_cannot_release_slot_and_cash_for_same_day_open():
    result = _run(exit_fill="close_t", exit_marks={("A", 2)})
    assert [trade.symbol for trade in result.trades] == ["A"]


def test_close_exit_cannot_release_slot_when_open_cash_is_sufficient():
    result = _run(exit_fill="close_t", exit_marks={("A", 2)}, max_exposure=0.5)
    assert [trade.symbol for trade in result.trades] == ["A"]


def test_close_exit_cannot_release_cash_when_open_slot_is_available():
    result = _run(
        exit_fill="close_t", exit_marks={("A", 2)},
        a_close=30.0, b_price=60.0, max_positions=2,
    )
    assert [trade.symbol for trade in result.trades] == ["A"]


def test_no_exit_on_buy_day_keeps_existing_capacity_guard():
    result = _run(exit_fill="close_t", exit_marks={("A", 3)})
    assert [trade.symbol for trade in result.trades] == ["A"]
    assert result.stats["execution"]["buy_no_slot"] == 1


def test_open_exit_can_fund_an_entry_at_the_same_open():
    result = _run(exit_fill="open_t+1", exit_marks={("A", 1)})
    assert [trade.symbol for trade in result.trades] == ["A", "B"]
    assert result.trades[1].entry_date == "2024-01-03"
    assert result.trades[1].entry_value == 100.0
    assert result.trades[1].shares == 5.0
