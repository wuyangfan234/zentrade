"""回测面板加载不得依赖已退役的 A 股涨跌停信号链 (10-02 清理轮)。

复现: BacktestEngine.load_panel_for_backtest 导入已删除的 compute_limit_signals →
非 matrix_native 日线策略 (AI 生成默认 polars_expr) 与全部分钟策略回测直接 ImportError;
分钟回放计划还要求 signal_limit_* 列, enriched 早已不再产出。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.minute_replay import MINUTE_DAILY_PANEL_COLUMNS, minute_replay_feature_plan


def _panel() -> pl.DataFrame:
    days = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    return pl.DataFrame({
        "symbol": ["NVDA"] * 3, "date": days,
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
        "volume": [10.0] * 3, "amount": [10.0] * 3,
        "raw_close": [1.0, 2.0, 3.0], "raw_high": [1.0, 2.0, 3.0], "raw_low": [1.0, 2.0, 3.0],
        "turnover_rate": [0.1] * 3,
    })


class _Repo:
    store = SimpleNamespace(data_dir=None)

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({
            "symbol": ["NVDA"], "name": ["NVIDIA"], "total_shares": [1e9], "float_shares": [1e9],
        })

    def get_historical_shares(self):
        return pl.DataFrame()


def _engine() -> BacktestEngine:
    engine = BacktestEngine(repo=_Repo())  # type: ignore[arg-type]
    engine.load_panel = lambda *args, **kwargs: _panel()  # type: ignore[method-assign]
    return engine


def test_polars_expr_daily_backtest_panel_loads_without_limit_signal_chain():
    plan = SimpleNamespace(
        base_columns=frozenset({"close"}), indicator_columns=frozenset(), signal_columns=frozenset(),
        instrument_columns=frozenset({"name"}), execution_backend="polars_expr",
    )
    out = _engine().load_panel_for_backtest(["NVDA"], date(2026, 9, 1), date(2026, 9, 3), plan)
    assert out.height == 3
    assert out["name"].to_list() == ["NVIDIA"] * 3


def test_minute_replay_plan_no_longer_requires_retired_limit_columns():
    plan = minute_replay_feature_plan(5)
    retired = {c for c in (*plan.base_columns, *plan.signal_columns, *MINUTE_DAILY_PANEL_COLUMNS) if "limit" in c}
    assert retired == set()
    out = _engine().load_panel_for_backtest(["NVDA"], date(2026, 9, 1), date(2026, 9, 3), plan)
    assert out.height == 3
    assert {"close", "raw_close", "total_shares", "float_shares"} <= set(out.columns)
