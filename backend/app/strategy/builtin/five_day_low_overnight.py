"""5-Day Low 隔夜 — QS 公开规则页逐字转录。

来源: https://www.quantifiedstrategies.com/5-day-low-strategy/
规则 (原文): 开盘创5日新低 且 收盘高于开盘 → 收盘买; 次日开盘卖 (T+1隔夜)。
作者自报 (SPY 长样本): 胜率约65-68%, 回撤浅。
适配口径:
  - "开盘创5日新低" = open < LLV(low, 5)[shifted 1] (不含当日的近5日最低);
    原页用 rolling 5 日 lowest low (含当日), 这里取前一日的5日最低点作为参照,
    与"开盘那一刻即知5日新低"的可成交语义一致 — 预登记需声明该口径选择。
  - "收盘高于开盘" = close > open (当日收阳)。
  - "次日开盘卖"同 3dd_overnight: exit=entry同根标签, 实际持有由系统卡结算。
覆盖缺口: 同上 — 隔夜语义依赖系统撮合。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    rolling_min,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "five_day_low_overnight",
    "name": "QS-5日新低",
    "description": (
        "开盘创5日新低且收盘>开盘→买; 次日开盘卖(T+1; QS公开规则; "
        "开盘新低=open<前一日5日最低价; exit=T+0标签, 持有由系统卡结算)"
    ),
    "tags": ["均值回归", "隔夜", "新低", "QuantifiedStrategies"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {"id": "low_lookback", "label": "新低回看天数", "type": "int", "default": 5, "min": 3, "max": 20, "step": 1},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_5dl_overnight_buy"]
EXIT_SIGNALS = ["signal_5dl_overnight_exit"]
STOP_LOSS = None
MAX_HOLD_DAYS = 1
WARMUP_BARS = 30


class FiveDayLowOvernightMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "low", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        return int(params.get("low_lookback", 5)) + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        n = int(params.get("low_lookback", 5))
        low = market.low.astype(np.float32)
        # 前一日的近5日最低价 (含前一日, 不含当日) — 开盘那一刻可知的参照位
        ref_low = shift(rolling_min(low, n), 1)

        open_ = market.open.astype(np.float32)
        close = market.close.astype(np.float32)

        entry = (open_ < ref_low) & (close > open_)
        exit_ = entry.copy()

        score = ((open_ - close) / np.where(np.isfinite(open_) & (open_ > 0), open_, np.nan) * -100.0)
        score = np.nan_to_num(score, nan=0.0).astype(np.float32)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_5dl_overnight_buy",),
            exit_signal_ids=("signal_5dl_overnight_exit",),
        )


MATRIX_STRATEGY = FiveDayLowOvernightMatrixStrategy()
