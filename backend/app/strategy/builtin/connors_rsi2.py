"""Connors RSI(2) 均值回归 — Larry Connors《Short Term Trading Strategies That Work》原书规则。

来源: Larry Connors & Cesar Alvarez, "Short Term Trading Strategies That Work" (2008);
      规则页 https://www.quantifiedstrategies.com/rsi2-strategy/
规则 (原文):
  买: 收盘 > 200日SMA 且 RSI(2) < 10  → 收盘买
  卖 (两条腿任一): RSI(2) > 70  或  收盘 > 5日SMA
证据: Connors 原书 SPY 长样本; QS 样本外复测有效 (多版本复现)。
适配口径: 出场两腿合成单一 exit 信号; 实际持有由系统卡结算。
覆盖缺口: 原书另有一个加仓版本 (第二日 RSI(2)<5 加第二笔), 本实现只做首笔单腿,
          不加仓 — 与原书"分批建仓"变体不同, 预登记需声明。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "connors_rsi2",
    "name": "QS-RSI2",
    "description": (
        "收盘>MA200 且 RSI(2)<10 买入; RSI(2)>70或收盘>MA5 卖出 "
        "(Connors原书规则, SPY长样本; 首笔单腿不含加仓变体)"
    ),
    "tags": ["均值回归", "RSI", "Connors", "趋势过滤"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {"id": "rsi_buy", "label": "RSI买入阈值", "type": "float", "default": 10.0, "min": 1.0, "max": 30.0, "step": 1.0},
        {"id": "rsi_exit", "label": "RSI卖出阈值", "type": "float", "default": 70.0, "min": 50.0, "max": 90.0, "step": 5.0},
        {"id": "use_ma5_exit", "label": "启用收盘>MA5出场", "type": "bool", "default": True},
        {"id": "require_above_ma200", "label": "要求收盘在MA200上方", "type": "bool", "default": True},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_connors_rsi2_buy"]
EXIT_SIGNALS = ["signal_connors_rsi2_exit"]
STOP_LOSS = None
MAX_HOLD_DAYS = 7
WARMUP_BARS = 210  # ma200 是核心过滤, 不可降


class ConnorsRsi2MatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return WARMUP_BARS if params.get("require_above_ma200", True) else 30

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close.astype(np.float32)
        rsi2 = matrix_feature(market, "rsi_2")

        entry = rsi2 < float(params.get("rsi_buy", 10.0))
        if params.get("require_above_ma200", True):
            entry &= close > matrix_feature(market, "ma200")

        exit_ = rsi2 > float(params.get("rsi_exit", 70.0))
        if params.get("use_ma5_exit", True):
            exit_ = exit_ | (close > matrix_feature(market, "ma5"))

        entry &= np.isfinite(rsi2)
        exit_ &= np.isfinite(rsi2) | np.isfinite(close)

        score = np.where(np.isfinite(rsi2), 100.0 - rsi2, 0.0).astype(np.float32)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_connors_rsi2_buy",),
            exit_signal_ids=("signal_connors_rsi2_exit",),
        )


MATRIX_STRATEGY = ConnorsRsi2MatrixStrategy()
