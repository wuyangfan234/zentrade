"""Williams %R(2) 均值回归 — QS 公开规则页逐字转录。

来源: https://www.quantifiedstrategies.com/williams-r-strategy/ (QuantifiedStrategies.com 公开策略页)
规则 (原文): Williams %R = (HHV(high,N) - close) / (HHV(high,N) - LLV(low,N)) * -100,
           N=2; W%R < -90 收盘买; 收盘 > 昨日最高 或 W%R > -30 卖。
作者自报 (SPY, 全样本含成本): 251 笔, 胜率 78%, 最大回撤 20.5%, 实测优于同族 RSI(2)。
适配口径: 两条出场腿合成单一 exit 信号 (任一满足即出); 实际持仓由系统卡结算。
覆盖缺口: 原版无大盘过滤; 提供可选 close>ma200 (默认关, 忠实原版)。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    rolling_max,
    rolling_min,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "williams_r2_reversion",
    "name": "QS-W%R",
    "description": (
        "W%R(2)<-90收盘买入; 收盘>昨高或W%R>-30卖出 "
        "(QS公开规则, SPY 251笔胜率78%/DD20.5%; 实测优于同族RSI-2)"
    ),
    "tags": ["均值回归", "WilliamsR", "摆动", "QuantifiedStrategies"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {"id": "period", "label": "%R周期", "type": "int", "default": 2, "min": 2, "max": 10, "step": 1},
        {"id": "buy_threshold", "label": "买入阈值(W%R)", "type": "float", "default": -90.0, "min": -99.0, "max": -50.0, "step": 5.0},
        {"id": "exit_threshold", "label": "卖出阈值(W%R)", "type": "float", "default": -30.0, "min": -60.0, "max": -10.0, "step": 5.0},
        {"id": "use_prev_high_exit", "label": "启用收盘>昨高出场", "type": "bool", "default": True},
        {"id": "require_above_ma200", "label": "要求收盘在MA200上方", "type": "bool", "default": False},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_wr2_buy"]
EXIT_SIGNALS = ["signal_wr2_exit"]
STOP_LOSS = None
MAX_HOLD_DAYS = 7
WARMUP_BARS = 210


class WilliamsR2MatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return WARMUP_BARS if params.get("require_above_ma200") else 30

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        n = int(params.get("period", 2))
        hh = rolling_max(market.high.astype(np.float32), n)
        ll = rolling_min(market.low.astype(np.float32), n)
        rng = hh - ll
        with np.errstate(invalid="ignore", divide="ignore"):
            wr = np.where(rng > 0, (hh - market.close.astype(np.float32)) / np.where(rng > 0, rng, np.nan), np.nan) * -100.0

        entry = wr < float(params.get("buy_threshold", -90.0))
        exit_ = wr > float(params.get("exit_threshold", -30.0))
        if params.get("use_prev_high_exit", True):
            prev_high = shift(market.high.astype(np.float32), 1)
            exit_ = exit_ | (market.close.astype(np.float32) > prev_high)

        if params.get("require_above_ma200"):
            entry &= market.close.astype(np.float32) > matrix_feature(market, "ma200")

        entry &= np.isfinite(wr)
        exit_ &= np.isfinite(wr) | np.isfinite(market.close.astype(np.float32))

        score = np.where(np.isfinite(wr), -wr, 0.0).astype(np.float32)  # 越超卖分越高

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_wr2_buy",),
            exit_signal_ids=("signal_wr2_exit",),
        )


MATRIX_STRATEGY = WilliamsR2MatrixStrategy()
