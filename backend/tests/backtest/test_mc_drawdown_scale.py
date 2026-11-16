"""蒙卡回撤必须和账户净值同一量纲。

仓位模拟里每笔只占约 1/最大持仓数 的资金, 旧版却把逐笔收益当满仓连乘重抽样: 一笔 -8% 被当成整个
账户 -8%。线上「因子组合 · atr_14」仓位模拟实测: 真实最大回撤 -38%, 蒙卡中位 -72% / 95% -88%。
现行口径 = 对账户每日净值收益有放回重抽样 (全量模拟同样对其等权组合的每日收益重抽样)。
"""
from __future__ import annotations

from datetime import date

import numpy as np

from app.backtest.engine import BacktestEngine, TradeRecord


def _trade(pnl: float) -> TradeRecord:
    return TradeRecord(symbol="A", entry_date=date(2026, 1, 2), exit_date=date(2026, 1, 9), entry_price=10.0,
                       exit_price=10.0 * (1 + pnl), pnl_pct=pnl, duration=5, exit_reason="signal",
                       position_pct=0.1)


def test_position_mode_monte_carlo_resamples_daily_equity_not_full_size_trades():
    rng = np.random.default_rng(7)
    daily = rng.normal(0.001, 0.01, 250)  # 账户每天 ±1% 左右
    values = [float(v) for v in 1_000_000 * np.cumprod(1 + daily)]
    # 逐笔看很猛 (各占 10% 仓位): 满仓连乘重抽样会给出接近 -100% 的回撤。
    trades = [_trade(p) for p in [-0.08] * 60 + [0.30] * 20]
    stats = BacktestEngine._calc_portfolio_stats_from_values(values, [0.9] * len(values), trades, 1_000_000)
    expected = BacktestEngine._mc_drawdown_percentiles(np.array(values[1:]) / np.array(values[:-1]) - 1)
    assert stats["mc_maxdd_p50"] == expected["mc_maxdd_p50"]
    assert stats["mc_maxdd_p95"] == expected["mc_maxdd_p95"]
    assert stats["mc_maxdd_p95"] > -0.5
    full_size = BacktestEngine._mc_drawdown_percentiles(np.array([t.pnl_pct for t in trades]))
    assert full_size["mc_maxdd_p50"] < -0.6  # 旧口径的量级, 留作对照
