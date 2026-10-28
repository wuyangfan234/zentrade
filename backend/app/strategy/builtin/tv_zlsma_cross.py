"""收盘上穿 ZLSMA — TradingView 开源脚本 “ZLSMA - Zero Lag LSMA” (veryfid, Pine v4, MPL-2.0)。

源码只画线; 信号按作者说明 “价格上穿 = 多头趋势, 下穿 = 空头趋势”: 买 = crossover(close, zlsma), 卖 = crossunder。
来源: https://www.tradingview.com/script/3LGnSrQN-ZLSMA-Zero-Lag-LSMA/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_ZLSMA, neutral_score, run_kernel

META = {'id': 'tv_zlsma_cross',
 'name': '收盘上穿 ZLSMA',
 'description': 'TradingView 开源 ZLSMA (veryfid) 移植: 收盘价上穿零滞后最小二乘均线当根买入 (信号取自作者说明)',
 'tags': ['TradingView', '均线', '趋势'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'length', 'label': '长度', 'type': 'int', 'default': 32, 'min': 5, 'max': 36, 'step': 1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_zlsma_cross_up"]
EXIT_SIGNALS = ["signal_tv_zlsma_cross_down"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvZlsmaCrossMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_ZLSMA, (int(params.get("length", 32)), 0))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_zlsma_cross_up",),
            exit_signal_ids=("signal_tv_zlsma_cross_down",),
        )


MATRIX_STRATEGY = TvZlsmaCrossMatrixStrategy()
