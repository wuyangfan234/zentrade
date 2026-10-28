"""IBS 均值回归 — QuantifiedStrategies 公开规则页逐字转录。

来源: https://www.quantifiedstrategies.com/ibs-strategy/ (QuantifiedStrategies.com 公开策略页)
规则 (原文): IBS = (close - low) / (high - low); 收盘 IBS < 0.2 买入, IBS > 0.8 卖出。
作者自报 (SPY 1993-至今, 全样本含成本): 919 笔, 胜率 68%, Profit Factor 1.9。
适配口径: 出场信号仅作 exit 标签 (ibs_exit_high), 实际持仓天数由系统统一卡
(MAX_HOLD_DAYS / watchlist_cards._hold) 结算; IBS>0.8 当日即触发 exit。
覆盖缺口 (与原规则的差异, 预登记必须如实声明):
  - 原规则无大盘过滤; 这里提供可选的 close>ma200 过滤 (默认关, 忠实原版)。
  - high==low 的 bar (一字板) IBS 记为 NaN, 不产生信号。
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
    "id": "ibs_mean_reversion",
    "name": "QS-IBS",
    "description": (
        "Internal Bar Strength=(C-L)/(H-L); 收盘 IBS<0.2 买入, IBS>0.8 卖出 "
        "(QS公开规则, SPY长样本919笔胜率68%/PF1.9; exit=T+0信号, 持有期由系统卡结算)"
    ),
    "tags": ["均值回归", "IBS", "隔夜", "QuantifiedStrategies"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "research_only": False,
    "params": [
        {
            "id": "ibs_buy",
            "label": "IBS买入阈值",
            "type": "float",
            "default": 0.2,
            "min": 0.05,
            "max": 0.5,
            "step": 0.05,
        },
        {
            "id": "ibs_exit",
            "label": "IBS卖出阈值",
            "type": "float",
            "default": 0.8,
            "min": 0.5,
            "max": 0.95,
            "step": 0.05,
        },
        {
            "id": "require_above_ma200",
            "label": "要求收盘在MA200上方",
            "type": "bool",
            "default": False,
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ibs_buy"]
EXIT_SIGNALS = ["signal_ibs_exit_high"]
STOP_LOSS = None
MAX_HOLD_DAYS = 7
WARMUP_BARS = 210  # ma200 可选过滤需要 200+ 预热


class IbsMeanReversionMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return WARMUP_BARS if params.get("require_above_ma200") else 30

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        high = market.high.astype(np.float32)
        low = market.low.astype(np.float32)
        close = market.close.astype(np.float32)

        rng = high - low
        # high==low → IBS 无定义, 记 NaN (该 bar 不产生信号)
        with np.errstate(invalid="ignore", divide="ignore"):
            ibs = np.where(rng > 0, (close - low) / np.where(rng > 0, rng, np.nan), np.nan)

        entry = ibs < float(params.get("ibs_buy", 0.2))
        exit_ = ibs > float(params.get("ibs_exit", 0.8))

        if params.get("require_above_ma200"):
            entry &= close > matrix_feature(market, "ma200")

        entry = entry & np.isfinite(ibs)
        exit_ = exit_ & np.isfinite(ibs)

        # score: IBS 越低越靠前 (做多均值回归, 越深的日内弱势越优先)
        score = np.where(np.isfinite(ibs), (1.0 - ibs) * 100.0, 0.0).astype(np.float32)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ibs_buy",),
            exit_signal_ids=("signal_ibs_exit_high",),
        )


MATRIX_STRATEGY = IbsMeanReversionMatrixStrategy()
