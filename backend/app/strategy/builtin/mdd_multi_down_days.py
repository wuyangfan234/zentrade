"""MDD 多日连跌 — Connors 原书 + QS 复测。

来源: Larry Connors & Cesar Alvarez, "Short Term Trading Strategies That Work" (2008);
      规则页 https://www.quantifiedstrategies.com/multiple-days-down-mdd-strategy/
规则 (原文):
  买: 收盘 > 200日SMA 且 收盘 < 5日SMA 且 近5日 >= 4日收跌 → 收盘买
  卖: 收盘 > 5日SMA
证据: Connors 原书; QS 样本外复测有效。
适配口径: 出场=收盘上穿MA5; 实际持有由系统卡结算。
覆盖缺口: 无 (本实现与原文规则一一对应)。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "mdd_multi_down_days",
    "name": "QS-MDD",
    "description": (
        "收盘>MA200 且 收盘<MA5 且 近5日>=4日跌 → 买; 收盘>MA5卖 "
        "(Connors原书, QS样本外复测有效)"
    ),
    "tags": ["均值回归", "连跌", "Connors", "趋势过滤"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {"id": "lookback", "label": "回看天数", "type": "int", "default": 5, "min": 3, "max": 10, "step": 1},
        {"id": "min_down", "label": "最少下跌天数", "type": "int", "default": 4, "min": 2, "max": 8, "step": 1},
        {"id": "require_below_ma5", "label": "要求收盘在MA5下方", "type": "bool", "default": True},
        {"id": "require_above_ma200", "label": "要求收盘在MA200上方", "type": "bool", "default": True},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_mdd_buy"]
EXIT_SIGNALS = ["signal_mdd_exit"]
STOP_LOSS = None
MAX_HOLD_DAYS = 7
WARMUP_BARS = 210


class MddMultiDownDaysMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return WARMUP_BARS if params.get("require_above_ma200", True) else int(params.get("lookback", 5)) + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close.astype(np.float32)
        lookback = int(params.get("lookback", 5))
        min_down = int(params.get("min_down", 4))

        down = (close < shift(close, 1)).astype(np.float32)
        # 近 lookback 日(含当日)下跌天数; shift 前 N 根为 NaN, 用 0 填充累加
        cnt = np.zeros_like(down)
        for i in range(lookback):
            cnt = cnt + np.nan_to_num(shift(down, i), nan=0.0)

        ma5 = matrix_feature(market, "ma5")
        entry = cnt >= min_down
        if params.get("require_below_ma5", True):
            entry &= close < ma5
        if params.get("require_above_ma200", True):
            entry &= close > matrix_feature(market, "ma200")

        # 出场: 收盘重新站上 MA5 (上穿沿)
        exit_ = (close > ma5) & (shift(close, 1) <= shift(ma5, 1))

        score = np.nan_to_num(cnt * 10.0, nan=0.0).astype(np.float32)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_mdd_buy",),
            exit_signal_ids=("signal_mdd_exit",),
        )


MATRIX_STRATEGY = MddMultiDownDaysMatrixStrategy()
