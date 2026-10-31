"""0.43.2 回归: 全量模拟 (mode=full) 的统计必须给出年化收益。

此前全量模拟的 stats 里没有 annual_return (总收益、夏普、回撤都有), 回测页「年化」一栏显示「—」,
与结束日选哪天无关。全量模拟的收益曲线 = 按退出日聚合的样本平均收益连乘 (不是账户净值), 年化按同一条曲线、
以首笔建仓日到末笔退出日的自然日跨度折算, 与逐笔统计 _calc_stats 同一口径。"""
from __future__ import annotations

from datetime import date

import pytest

from app.backtest.engine import BacktestEngine, SimulationOptions, TradeRecord


def _trade(entry: date, exit_: date, pnl: float) -> TradeRecord:
    return TradeRecord(symbol="A", entry_date=entry, exit_date=exit_, entry_price=100.0,
                       exit_price=100.0 * (1 + pnl), pnl_pct=pnl, duration=(exit_ - entry).days,
                       exit_reason="signal")


def test_full_mode_stats_include_annual_return():
    trades = [_trade(date(2026, 1, 2), date(2026, 4, 2), 0.10),
              _trade(date(2026, 2, 2), date(2026, 7, 2), -0.05)]
    res = BacktestEngine._calc_independent_candidate_result(trades, 2, {}, options=SimulationOptions())
    assert res.stats["total_return"] == pytest.approx(0.045, abs=1e-4)
    years = (date(2026, 7, 2) - date(2026, 1, 2)).days / 365.25
    assert res.stats["annual_return"] == pytest.approx(1.045 ** (1 / years) - 1, abs=1e-4)


def test_full_mode_annual_return_for_losing_short_window_is_finite():
    """线上那种短窗口大亏 (-40% 上下) 也要给出有限的年化值, 不能是 None/NaN。"""
    trades = [_trade(date(2026, 7, 31), date(2026, 9, 15), -0.25),
              _trade(date(2026, 8, 20), date(2026, 10, 30), -0.24)]
    res = BacktestEngine._calc_independent_candidate_result(trades, 2, {}, options=SimulationOptions())
    ann = res.stats["annual_return"]
    assert ann is not None and -1.0 < ann < res.stats["total_return"]
