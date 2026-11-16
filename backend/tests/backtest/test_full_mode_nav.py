"""全量模拟 (mode=full) 净值口径回归。

旧口径把「每个退出日平仓那几笔的平均收益」直接连乘成净值: 同一只票每天都入场、
每笔持有十几天时, 10230 笔重叠交易被串成 1 万多次复利, 线上算出总收益 +6.5e22%、
年化 +7e11%、夏普 10.97, 而同一批交易的平均单笔收益只有 +7%。

现行口径 = 逐日盯市的等权组合: 每天把资金平均分给当天所有在场候选 (每日再平衡),
没有候选的日子持现金; 每笔在持有期内的逐日收益连乘后恰好等于该笔扣费后的 pnl_pct。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions, TradeRecord


def _row(symbol: str, day: int, price: float, *, entry: bool = False) -> dict:
    return {
        "symbol": symbol, "name": symbol, "date": date(2024, 1, 1) + timedelta(days=day),
        "open": price, "high": price, "low": price, "close": price, "volume": 100_000,
        "score": 0.0, "signal_limit_up": False, "signal_limit_down": False,
        "signal_entry": entry, "signal_exit": False,
    }


def _zero_cost(**kw) -> MatcherConfig:
    return MatcherConfig(matching="close_t", cost_model="legacy_pct", fees_pct=0.0, slippage_bps=0.0, **kw)


def _run(panel: pl.DataFrame, config: MatcherConfig):
    panel = panel.sort(["symbol", "date"])
    return BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, panel["signal_entry"], panel["signal_exit"], config,
    )


def _rising_panel() -> pl.DataFrame:
    # A 每天 +1 美元 (100 → 111), 前 7 天天天出买点, 每笔持有 5 天: 任何一天都有多笔重叠在场。
    return pl.DataFrame([_row("A", d, 100.0 + d, entry=d <= 6) for d in range(12)])


def test_overlapping_trades_are_not_compounded_into_the_nav():
    res = _run(_rising_panel(), _zero_cost(max_hold_days=5))
    pnls = [t.pnl_pct for t in res.trades]
    assert len(pnls) == 7 and all(p > 0.04 for p in pnls)
    # 旧口径: 7 个退出日各 ~+4.7% 连乘 ≈ +38%。真实可实现的收益 = 一直持有 A 的涨幅 111/100。
    assert res.stats["total_return"] == pytest.approx(0.11, abs=1e-4)
    assert res.stats["nav_method"] == "daily_mtm_equal_weight"


def test_curve_covers_every_bar_and_reports_open_positions():
    res = _run(_rising_panel(), _zero_cost(max_hold_days=5))
    dates = [row["date"] for row in res.equity_curve]
    assert dates == [str(date(2024, 1, 1) + timedelta(days=d)) for d in range(12)]
    # close_t 入场在收盘成交, 入场当天不占用资金; 第 d 天持有的是 d-5..d-1 天入场的那几笔。
    positions = [row["positions"] for row in res.equity_curve]
    assert positions == [0, 1, 2, 3, 4, 5, 5, 5, 4, 3, 2, 1]
    values = np.array([row["value"] for row in res.equity_curve])
    assert values[0] == pytest.approx(1.0)
    assert values[-1] == pytest.approx(1.11, abs=1e-4)


def test_stats_are_derived_from_the_daily_nav():
    panel = pl.DataFrame(
        [_row("A", d, p, entry=d in (0, 2)) for d, p in enumerate([100, 110, 99, 104, 95, 100, 102, 101])]
    )
    res = _run(panel, _zero_cost(max_hold_days=4))
    values = np.array([row["value"] for row in res.equity_curve])
    daily = values[1:] / values[:-1] - 1
    n = len(values)
    total = res.stats["total_return"]
    assert total == pytest.approx(values[-1] - 1, abs=1e-4)
    assert res.stats["annual_return"] == pytest.approx((1 + total) ** (252 / n) - 1, rel=1e-3)
    assert res.stats["max_drawdown"] == pytest.approx(min(p["value"] for p in res.drawdown_curve), abs=1e-4)
    assert res.stats["max_drawdown"] == pytest.approx(float((values / np.maximum.accumulate(values)).min() - 1), abs=1e-4)
    full_daily = np.concatenate([[0.0], daily])
    expected_sharpe = full_daily.mean() / full_daily.std() * np.sqrt(252)
    assert res.stats["sharpe"] == pytest.approx(expected_sharpe, abs=0.05)
    assert res.stats["n_days"] == n


def test_two_symbols_are_equal_weighted_each_day():
    rows = [_row("A", d, p, entry=d == 0) for d, p in enumerate([100, 110, 121, 121])]
    rows += [_row("B", d, p, entry=d == 0) for d, p in enumerate([50, 45, 45, 45])]
    res = _run(pl.DataFrame(rows), _zero_cost(max_hold_days=2))
    values = [row["value"] for row in res.equity_curve]
    # 第 1 天: A +10%, B -10% → 0; 第 2 天: A +10%, B 0% → +5%。
    assert values[:3] == pytest.approx([1.0, 1.0, 1.05], abs=1e-4)
    per_symbol = {row["symbol"]: row["total_return"] for row in res.per_symbol_stats}
    assert per_symbol == pytest.approx({"A": 0.21, "B": -0.10}, abs=1e-4)


def test_per_symbol_return_is_that_symbols_nav_not_compounded_trades():
    res = _run(_rising_panel(), _zero_cost(max_hold_days=5))
    (row,) = res.per_symbol_stats
    assert row["n_trades"] == 7
    assert row["total_return"] == pytest.approx(0.11, abs=1e-4)


def test_costs_are_kept_so_each_trade_path_reproduces_its_pnl():
    panel = pl.DataFrame([_row("A", d, 100.0 + d, entry=d == 0) for d in range(6)])
    res = _run(panel, MatcherConfig(matching="close_t", max_hold_days=3))  # 默认美股按股佣金 + 5bp 滑点
    (trade,) = res.trades
    assert trade.pnl_pct < 0.03  # 扣费后低于毛收益 3%
    assert res.stats["total_return"] == pytest.approx(trade.pnl_pct, abs=1e-4)


def _trade(entry: date, exit_: date, pnl: float, symbol: str = "A") -> TradeRecord:
    return TradeRecord(symbol=symbol, entry_date=entry, exit_date=exit_, entry_price=100.0,
                       exit_price=100.0 * (1 + pnl), pnl_pct=pnl, duration=(exit_ - entry).days,
                       exit_reason="signal")


def test_trade_only_fallback_does_not_compound_identical_overlapping_trades():
    # 没有行情矩阵时按持有工作日几何均摊: 三笔完全重合的 +10% 等权后仍是 +10%。
    trades = [_trade(date(2026, 1, 5), date(2026, 1, 9), 0.10) for _ in range(3)]
    res = BacktestEngine._calc_independent_candidate_result(trades, 3, {}, options=SimulationOptions())
    assert res.stats["total_return"] == pytest.approx(0.10, abs=1e-4)
    staggered = [_trade(date(2026, 1, 5) + timedelta(days=i), date(2026, 1, 12) + timedelta(days=i), 0.10)
                 for i in range(5)]
    res = BacktestEngine._calc_independent_candidate_result(staggered, 5, {}, options=SimulationOptions())
    # 五笔错开一天、各 +10%: 旧口径连乘 1.1**5-1 = +61%; 等权盯市远小于此。
    assert res.stats["total_return"] < 0.25


def test_benchmark_window_extends_to_the_last_nav_day():
    from app.backtest.strategy import StrategyBacktestService

    curve = [{"date": "2026-10-14", "value": 1.0}, {"date": "2026-11-13", "value": 1.2}]
    end = StrategyBacktestService._benchmark_end(date(2026, 10, 15), curve)
    assert end == date(2026, 11, 13)
    assert StrategyBacktestService._benchmark_end(date(2026, 10, 15), []) == date(2026, 10, 15)
    assert StrategyBacktestService._benchmark_end(date(2026, 10, 15), curve[:1]) == date(2026, 10, 15)
