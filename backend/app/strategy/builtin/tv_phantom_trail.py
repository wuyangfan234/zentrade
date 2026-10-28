"""Phantom 追踪轨道转多 — 用户提供的 Pine 源码 “Phantom Flow Core 中文优化版|美股1周波段” SECTION 4 (Phantom Trail) 逐行移植 (日线)。

吊灯轨道: highest(high,22) - 3xATR(14), close[1] 棘轮; trlDir 与当根轨道比较翻转; 买 = 转多 (trlFlipLong), 卖 = 转空。"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_PHANTOM_TRAIL, neutral_score, run_kernel

META = {'id': 'tv_phantom_trail',
 'name': 'Phantom 追踪轨道转多',
 'description': 'Phantom Flow Core Pine 源码 SECTION 4 逐行移植 (日线): 吊灯追踪轨道由空转多当根买入',
 'tags': ['Phantom', '趋势', '吊灯止损'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'trail_length',
             'label': '吊灯周期',
             'type': 'int',
             'default': 22,
             'min': 5,
             'max': 60,
             'step': 1},
            {'id': 'multiplier',
             'label': 'ATR 倍数',
             'type': 'float',
             'default': 3.0,
             'min': 0.5,
             'max': 10.0,
             'step': 0.1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_phantom_trail_long"]
EXIT_SIGNALS = ["signal_tv_phantom_trail_short"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvPhantomTrailMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_PHANTOM_TRAIL, (int(params.get("trail_length", 22)),),
                                  (float(params.get("multiplier", 3.0)),))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_phantom_trail_long",),
            exit_signal_ids=("signal_tv_phantom_trail_short",),
        )


MATRIX_STRATEGY = TvPhantomTrailMatrixStrategy()
