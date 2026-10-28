"""3 Down Days 隔夜 — QS 公开规则页逐字转录。

来源: https://www.quantifiedstrategies.com/3-consecutive-down-days-strategy/
规则 (原文): 连3日收跌 → 第3日收盘买; 次日开盘卖 (T+1, 纯隔夜)。
作者自报 (SPY, 含成本): 643 笔, 胜率 65%, 最大回撤仅 8%。
适配口径:
  - "次日开盘卖" 无法由矩阵 exit 直接表达成次日开盘价成交 — 这里 exit 在入场
    当日同根即触发 (holding=1的语义标签), 实际成交/持有天数由系统统一卡结算;
    系统若按次日开盘撮合并结算 1 天, 则最接近原规则; 若按统一 5 日卡结算,
    则偏离原版, 预登记必须声明这是"信号级T+1语义, 执行依赖系统撮合"。
  - 原规则无大盘过滤; 提供可选 close>ma200 (默认关)。
覆盖缺口: 实际持有期由系统卡决定, 与本策略 exit 标签可能不一致 — 这是
          隔夜家族统一的适配缺口, 不是单条策略的问题。
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
    "id": "three_down_days_overnight",
    "name": "QS-3连跌",
    "description": (
        "连3日收跌→第3日收盘买, 次日开盘卖(T+1隔夜; QS公开规则, SPY 643笔胜率65%/DD8%; "
        "exit=T+0标签, 实际持有由系统卡结算)"
    ),
    "tags": ["均值回归", "隔夜", "连跌", "QuantifiedStrategies"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {"id": "down_days", "label": "连跌天数", "type": "int", "default": 3, "min": 2, "max": 6, "step": 1},
        {"id": "require_above_ma200", "label": "要求收盘在MA200上方", "type": "bool", "default": False},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_3dd_overnight_buy"]
EXIT_SIGNALS = ["signal_3dd_overnight_exit"]
STOP_LOSS = None
MAX_HOLD_DAYS = 1  # 隔夜语义: 持有1天
WARMUP_BARS = 210


class ThreeDownDaysMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return WARMUP_BARS if params.get("require_above_ma200") else int(params.get("down_days", 3)) + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        close = market.close.astype(np.float32)
        n = int(params.get("down_days", 3))

        down = close < shift(close, 1)
        streak = down.astype(np.float32)
        for i in range(1, n):
            streak = streak + np.nan_to_num(shift(down.astype(np.float32), i), nan=0.0)
        entry = streak >= n

        if params.get("require_above_ma200"):
            entry &= close > matrix_feature(market, "ma200")

        # T+1 语义: exit 在入场当日即触发, 表示"下一交易日卖出"
        exit_ = entry.copy()

        score = np.nan_to_num(streak * 10.0, nan=0.0).astype(np.float32)  # 连跌越多分越高

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_3dd_overnight_buy",),
            exit_signal_ids=("signal_3dd_overnight_exit",),
        )


MATRIX_STRATEGY = ThreeDownDaysMatrixStrategy()
