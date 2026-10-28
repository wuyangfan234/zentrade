"""Regression oracles for entry-resource fixes across fill modes and sizing."""
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions
from app.backtest.matrix import build_market_matrix

START = date(2024, 1, 1)
OPTIONS = SimulationOptions(include_monte_carlo=False, include_return_distribution=False)


def _mask(frame, marks):
    return pl.Series([
        (row["symbol"], (row["date"] - START).days) in marks
        for row in frame.select(["symbol", "date"]).iter_rows(named=True)
    ], dtype=pl.Boolean)


def _run(frame, entries, exits, config):
    matrix = build_market_matrix(
        frame.sort(["symbol", "date"]), entries, exits,
        entry_delay_bars=1 if config.entry_fill == "open_t+1" else 0,
        exit_delay_bars=1 if config.exit_fill == "open_t+1" else 0,
    )
    return BacktestEngine(repo=None)._simulate_portfolio_matrix(
        matrix, config, None, None, OPTIONS,
    )


def _config(**overrides):
    values = dict(
        entry_fill="open_t+1", exit_fill="close_t", fees_pct=0, slippage_bps=0,
        max_positions=2, max_exposure_pct=1.0, initial_capital=1000.0,
    )
    values.update(overrides)
    return MatcherConfig(**values)


def test_same_day_multiple_buys_keep_last_close_capacity_accounting():
    rows = []
    for symbol, score in (("A", 4), ("B", 3)):
        for day in range(3):
            # A gaps down at the open but recovers to the prior close by the end.
            open_price = 5.0 if symbol == "A" and day == 1 else 10.0
            rows.append({
                "symbol": symbol, "name": symbol, "date": START + timedelta(days=day),
                "open": open_price, "high": 10.0, "low": open_price,
                "close": 10.0, "volume": 100_000, "score": score,
            })
    frame = pl.DataFrame(rows)
    result = _run(
        frame, _mask(frame, {("A", 0), ("B", 0)}), _mask(frame, set()),
        _config(max_exposure_pct=0.4),
    )
    trades = {trade.symbol: trade for trade in result.trades}
    assert trades["A"].shares == 40.0
    assert trades["B"].shares == 8.0
    assert result.stats["max_exposure"] == pytest.approx(0.4)


def test_intraday_stop_releases_resources_for_close_entry():
    rows = []
    for symbol, price in (("A", 10.0), ("B", 20.0)):
        for day in range(3):
            low = 8.9 if symbol == "A" and day == 1 else price
            rows.append({
                "symbol": symbol, "name": symbol, "date": START + timedelta(days=day),
                "open": price, "high": price, "low": low, "close": price,
                "volume": 100_000, "score": 4 if symbol == "A" else 3,
            })
    frame = pl.DataFrame(rows)
    result = _run(
        frame, _mask(frame, {("A", 0), ("B", 1)}), _mask(frame, set()),
        _config(entry_fill="close_t", max_positions=1, initial_capital=100.0,
                stop_loss_pct=0.1),
    )
    trades = {trade.symbol: trade for trade in result.trades}
    assert trades["A"].exit_reason == "stop_loss"
    assert trades["B"].shares == 4.0
    assert trades["B"].entry_value == 80.0


def test_close_signal_exit_releases_resources_for_close_entry():
    rows = []
    for symbol, price in (("A", 10.0), ("B", 20.0)):
        for day in range(3):
            rows.append({
                "symbol": symbol, "name": symbol, "date": START + timedelta(days=day),
                "open": price, "high": price, "low": price, "close": price,
                "volume": 100_000, "score": 4 if symbol == "A" else 3,
            })
    frame = pl.DataFrame(rows)
    result = _run(
        frame, _mask(frame, {("A", 0), ("B", 1)}), _mask(frame, {("A", 1)}),
        _config(entry_fill="close_t", max_positions=1, initial_capital=100.0),
    )
    trades = {trade.symbol: trade for trade in result.trades}
    assert trades["B"].shares == 5.0
    assert trades["B"].entry_value == 100.0
