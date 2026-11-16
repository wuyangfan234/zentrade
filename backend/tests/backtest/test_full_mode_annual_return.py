"""0.43.2 回归: 全量模拟 (mode=full) 的统计必须给出年化收益。

此前全量模拟的 stats 里没有 annual_return (总收益、夏普、回撤都有), 回测页「年化」一栏显示「—」,
与结束日选哪天无关。

净值口径现为逐日盯市的等权组合 (见 test_full_mode_nav.py), 不再把各退出日的平均收益连乘;
年化按净值曲线覆盖的交易日数折算, 与仓位模拟同一公式 (1 + 总收益) ** (252 / 天数) - 1。"""
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
    total = res.stats["total_return"]
    # 两笔有一段重叠: 等权盯市后总收益落在两笔之间, 不是逐笔连乘的 1.10 * 0.95 - 1 = +4.5%。
    assert -0.05 < total < 0.10
    assert total != pytest.approx(0.045, abs=1e-3)
    n_days = len(res.equity_curve)
    assert res.stats["n_days"] == n_days
    assert res.stats["annual_return"] == pytest.approx((1 + total) ** (252 / n_days) - 1, abs=1e-3)


def test_full_mode_annual_return_for_losing_short_window_is_finite():
    """线上那种短窗口大亏 (-40% 上下) 也要给出有限的年化值, 不能是 None/NaN。"""
    trades = [_trade(date(2026, 7, 31), date(2026, 9, 15), -0.25),
              _trade(date(2026, 8, 20), date(2026, 10, 30), -0.24)]
    res = BacktestEngine._calc_independent_candidate_result(trades, 2, {}, options=SimulationOptions())
    ann = res.stats["annual_return"]
    assert ann is not None and -1.0 < ann < res.stats["total_return"]
