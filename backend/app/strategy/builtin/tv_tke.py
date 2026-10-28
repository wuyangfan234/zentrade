"""TKE 上穿 20 — TradingView 公开脚本 TKE v4.2 (gokturk80, Pine v5) 的默认买入。

源脚本自己声明是指标不是策略。推荐用法写在源码头: 1 小时图, TKE 上穿 20,
下一根开盘进, ATR 3 倍跟踪止损, 最多 60 根; 近 10 根经典负背离则这次信号作废。
这里只移植「上穿 20」这一根信号, 供策略页在 1 小时自选上查看。
跟踪止损, 60 根上限和背离否决没有接进统一 7 日卡。
来源: https://tr.tradingview.com/script/bHu8yPba/
许可: 源码写明沿用 v3 的非商用约定。
"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_TKE, neutral_score, run_kernel

META = {
    "id": "tv_tke",
    "name": "TKE 上穿 20",
    "description": (
        "TradingView 公开 TKE v4.2 的默认买点: 1 小时图上 TKE 上穿 20。"
        "只供查看, 不进日线扫描, 不下单。源脚本是指标, 出场是 ATR 3 倍跟踪止损。"
    ),
    "tags": ["TradingView", "1小时", "摆动"],
    "asset_types": ["stock"],
    "timeframes": ["1h"],
    "params": [
        {"id": "period", "label": "振荡周期", "type": "int", "default": 14, "min": 2, "max": 50, "step": 1},
        {"id": "level", "label": "上穿位", "type": "float", "default": 20.0, "min": 5.0, "max": 50.0, "step": 1.0},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
    "research_only": False,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_tke_cross_20"]
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = 20
WARMUP_BARS = 80


class TvTkeMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(
            market,
            KIND_TKE,
            (int(params.get("period", 14)),),
            (float(params.get("level", 20.0)),),
        )
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_tke_cross_20",),
            exit_signal_ids=(),
        )


MATRIX_STRATEGY = TvTkeMatrixStrategy()
