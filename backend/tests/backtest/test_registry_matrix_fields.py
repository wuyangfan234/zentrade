"""0.47.0: matrix-native backtests accept custom / composite registry factors.

Before: factor_rank_research scored with a `uf_*` DSL factor failed with
"unsupported matrix feature". The fix attaches registry factors as matrix fields after loading,
computed by the factor inspector path, so the matrix score equals the panel value.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import MarketDataMatrix, matrix_feature
from app.backtest.registry_fields import attach_matrix_registry_fields, registry_factor_names
from app.factors import registry
from app.factors.store import register_definition


@pytest.fixture
def uf_factor():
    fid = "uf_test_mom3_matrix"
    registry.unregister_factor(fid)
    register_definition({
        "id": fid, "kind": "custom", "label": "mom3", "formula": "close / ts_delay(close, 3) - 1",
        "status": "draft", "direction": "high",
    })
    yield fid
    registry.unregister_factor(fid)


def _panel(symbols, days):
    rows = []
    d0 = date(2024, 1, 1)
    rng = np.random.default_rng(7)
    for s_i, sym in enumerate(symbols):
        price = 100.0 + s_i
        for k in range(days):
            price *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": sym, "date": d0 + timedelta(days=k), "open": price, "high": price * 1.01,
                         "low": price * 0.99, "close": price, "volume": 1e6, "amount": price * 1e6, "turnover_rate": 0.0})
    return pl.DataFrame(rows)


class _Engine:
    def __init__(self, panel):
        self.panel = panel
        self.calls = []

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock", **kw):
        self.calls.append((tuple(symbols), start, end))
        p = self.panel.filter(pl.col("symbol").is_in(symbols) & (pl.col("date") >= start) & (pl.col("date") <= end))
        return p.select([c for c in (columns or p.columns) if c in p.columns])


def _matrix(panel, symbols, labels):
    from types import MappingProxyType
    n_t, n_s = len(labels), len(symbols)
    close = np.full((n_t, n_s), np.nan, dtype=np.float32)
    idx = {d: i for i, d in enumerate(labels)}
    for r in panel.iter_rows(named=True):
        if str(r["date"]) in idx and r["symbol"] in symbols:
            close[idx[str(r["date"])], symbols.index(r["symbol"])] = r["close"]
    z = np.zeros((n_t, n_s), dtype=np.float32)
    return MarketDataMatrix(
        timestamps=np.arange(n_t), timestamp_labels=tuple(labels), session_ids=np.arange(n_t), symbols=tuple(symbols),
        names=tuple(symbols), open=close, high=close, low=close, close=close, volume=z + 1, tradable=np.ones((n_t, n_s), bool),
        fields=MappingProxyType({}),
    )


def test_registry_names_only_custom_and_composite(uf_factor):
    assert registry_factor_names([uf_factor, "momentum_5d", "close", "nope"]) == frozenset({uf_factor})


def test_matrix_feature_rejects_custom_without_attach(uf_factor):
    panel = _panel(["AAA", "BBB"], 30)
    labels = [str(date(2024, 1, 1) + timedelta(days=k)) for k in range(10, 30)]
    m = _matrix(panel, ["AAA", "BBB"], labels)
    with pytest.raises(ValueError, match="unsupported matrix feature"):
        matrix_feature(m, uf_factor)


def test_attach_matches_inspector_values(uf_factor):
    syms = ["AAA", "BBB", "CCC"]
    panel = _panel(syms, 40)
    labels = [str(date(2024, 1, 1) + timedelta(days=k)) for k in range(10, 40)]
    m = _matrix(panel, syms, labels)
    eng = _Engine(panel)
    m2 = attach_matrix_registry_fields(m, eng, [uf_factor])
    got = matrix_feature(m2, uf_factor)
    assert got.shape == m.shape and not got.flags.writeable
    # warm-up came from before the first label: no NaN in the visible window
    assert np.isfinite(got).all()
    p = panel.sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("e"))
    for j, s in enumerate(syms):
        exp = p.filter(pl.col("symbol") == s).filter(pl.col("date") >= date.fromisoformat(labels[0]))["e"].to_numpy()
        np.testing.assert_allclose(got[:, j], exp.astype(np.float32), rtol=1e-5)
    assert eng.calls[0][1] < date.fromisoformat(labels[0])


def test_attach_noop_for_builtin_names():
    panel = _panel(["AAA"], 20)
    labels = [str(date(2024, 1, 1) + timedelta(days=k)) for k in range(5, 20)]
    m = _matrix(panel, ["AAA"], labels)
    eng = _Engine(panel)
    assert attach_matrix_registry_fields(m, eng, ["momentum_5d"]) is m
    assert eng.calls == []


def test_feature_plan_carries_registry_columns(uf_factor):
    from pathlib import Path

    from app.backtest.strategy import StrategyDependencyResolver
    from app.strategy.engine import StrategyEngine
    se = StrategyEngine(strategy_dirs=[Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"])
    se.reload()
    strat = se.get("factor_rank_research")
    plan = StrategyDependencyResolver().resolve(
        strat, params={"scoring": {uf_factor: 1.0, "momentum_5d": 1.0}, "directions": {uf_factor: "high", "momentum_5d": "low"}},
        basic_filter={}, entry_signals=strat.entry_signals, exit_signals=strat.exit_signals, overrides={},
    )
    assert plan.registry_columns == frozenset({uf_factor})
    assert "close" in plan.base_columns
