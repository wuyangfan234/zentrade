"""Batch D: US strategy execution, point-in-time ranking and provenance."""
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService, _basic_filter_for_asset

START = date(2026, 1, 5)


def panel(symbols=("AAPL",), price=1000.0, days=5, liquidity=None):
    liquidity = liquidity or {}
    return pl.DataFrame([
        {"symbol": symbol, "date": START + timedelta(days=i), "name": symbol,
         "open": price, "high": price, "low": price, "close": price,
         "volume": float(liquidity.get((symbol, i), 1000)), "score": 0.0}
        for symbol in symbols for i in range(days)
    ])


def run(frame, method="simulate_portfolio", **kwargs):
    cfg = MatcherConfig(matching="open_t+1", initial_capital=2505, max_positions=1,
                        max_hold_days=1, **kwargs)
    entries = frame["date"] == START
    return getattr(BacktestEngine(None), method)(frame, entries, None, cfg)


@pytest.mark.parametrize("method", ["simulate_portfolio", "simulate_portfolio_legacy"])
def test_high_price_stock_is_sized_in_single_shares_with_minimum_commission(method):
    result = run(panel(), method)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.shares == 2
    assert t.entry_value == 2002.0  # $2000 + $1 commission + $1 slippage
    assert t.exit_value == 1998.0
    assert t.pnl_amount == -4.0
    assert result.equity_curve[-1]["value"] == 2501.0
    assert t.entry_commission == 1.0
    assert t.exit_commission == 1.0
    assert t.entry_slippage == 1.0
    assert t.exit_slippage == 1.0


@pytest.mark.parametrize("method", ["simulate_independent_candidates", "simulate_independent_candidates_legacy"])
def test_independent_samples_use_explicit_notional_and_us_costs(method):
    t = run(panel(), method).trades[0]
    assert t.shares == 2  # same $2505 sample budget, not a hard-coded 100-share lot
    assert t.entry_value == 2002.0
    assert t.exit_value == 1998.0


def test_us_commission_uses_per_share_rate_above_minimum_and_reserves_cash():
    cfg = MatcherConfig()
    assert cfg.commission(400, 10) == 2.0
    assert cfg.commission(1, 1000) == 1.0
    assert cfg.affordable_shares(1001.49, 1000) == 0
    assert cfg.affordable_shares(1001.50, 1000) == 1
    assert cfg.buy_value(400, 10) == 4004.0
    assert cfg.sell_value(400, 10) == 3996.0


@pytest.mark.parametrize("method", ["simulate_portfolio", "simulate_portfolio_legacy"])
@pytest.mark.parametrize("order", [("AAPL", "ZZZ"), ("ZZZ", "AAPL")])
def test_score_tie_uses_prior_liquidity_not_symbol_order_or_future_volume(method, order):
    # Huge volume from fill day onward must NOT rank AAPL ahead of liquid ZZZ.
    liquidity = {("ZZZ", 0): 2000, **{("AAPL", i): 1e12 for i in range(1, 5)}}
    result = run(panel(order, price=10.0, liquidity=liquidity), method)
    assert [t.symbol for t in result.trades] == ["ZZZ"]
    assert result.stats["selection_policy"]["id"] == "score_adv20_prior_v1"
    trade = result.trades[0]
    assert trade.entry_adv20_usd == 20_000.0
    assert trade.selection_as_of == str(START)


@pytest.mark.parametrize("method", ["simulate_portfolio", "simulate_portfolio_legacy"])
def test_economically_identical_cutoff_ties_are_skipped_not_chosen_alphabetically(method):
    result = run(panel(("AAPL", "ZZZ"), price=10.0), method)
    assert result.trades == []
    assert result.stats["execution"]["buy_unresolved_tie"] == 2


def test_minute_vwap_uses_dollar_amount_divided_by_shares():
    minute = np.array([[100, 100, 100, 100, 20, 2000], [110, 110, 110, 110, 10, 1100]])
    assert BacktestEngine._resolve_minute_fill(minute, None, "buy") == pytest.approx(3100 / 30)


def test_benchmark_reads_spy_from_stock_daily_and_filters_nonfinite_values():
    calls = []
    def get_daily(symbol, start, end, columns=None):
        calls.append((symbol, start, end, columns))
        return pl.DataFrame({"date": [START + timedelta(days=i) for i in range(4)],
                             "close": [100.0, float("inf"), 105.0, 110.0]})
    repo = SimpleNamespace(get_daily=get_daily)
    svc = StrategyBacktestService(BacktestEngine(repo), None)
    curve = svc._build_benchmark_curve(START, START + timedelta(days=3))
    assert [p["close"] for p in curve] == [100, 105, 110]
    assert {p["symbol"] for p in curve} == {"SPY"}
    assert calls[0][0] == "SPY"


def test_missing_benchmark_stays_empty():
    repo = SimpleNamespace(get_daily=lambda *a, **kw: pl.DataFrame())
    svc = StrategyBacktestService(BacktestEngine(repo), None)
    assert svc._build_benchmark_curve(START, START) == []


@pytest.mark.parametrize("asset_type", ["stock", "etf"])
def test_backtest_neutralizes_cn_board_and_st_filters_without_mutating_saved_config(asset_type):
    original = {"enabled": True, "exclude_st": True, "boards": ["沪主板"], "amount_min": 10.0}
    clean = _basic_filter_for_asset(original, asset_type)
    assert not clean.get("exclude_st")
    assert not clean.get("boards")
    assert original["exclude_st"] is True
    assert clean["amount_min"] == 10.0


def test_new_config_snapshot_declares_us_costs_legacy_request_keeps_legacy_evidence():
    new = StrategyBacktestConfig("test", None, START, START)
    payload = StrategyBacktestService._config_to_dict(new)
    assert payload["cost_model"] == "us_per_share_v1"
    assert payload["commission_per_share"] == 0.005
    assert payload["commission_min"] == 1.0
    assert payload["currency"] == "USD"
    assert payload["share_unit"] == 1
    old = StrategyBacktestConfig("test", None, START, START, commission_pct=0.0002, stamp_tax_pct=0.001)
    legacy = StrategyBacktestService._config_to_dict(old)
    assert legacy["cost_model"] == "legacy_pct"
    assert legacy["commission_pct"] == 0.0002
    assert legacy["stamp_tax_pct"] == 0.001


def test_candidate_store_preserves_new_cost_provenance(tmp_path):
    from app.backtest.candidates import CandidateStore
    cfg = {"strategy_id": "test", "cost_model": "us_per_share_v1", "commission_per_share": 0.005,
           "commission_min": 1.0, "slippage_bps": 5, "currency": "USD", "share_unit": 1}
    saved = CandidateStore(tmp_path).create(kind="strategy", name="US", source_id="test", config=cfg, metrics={}, data_as_of=None)
    assert saved["config"] == cfg
