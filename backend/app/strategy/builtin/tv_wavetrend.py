"""WaveTrend 超卖强金叉 — TradingView 开源脚本 “WaveTrend [LazyBear] vX by DGT” (dgtrd, Pine v6) 移植, 默认 crosses = Strong。

买 = wtOsc 上穿 wtSig 且 wtOsc < avg(-60, -53) (强多); 卖 = 下穿且 > avg(60, 53) (强空)。对比他股 (Compare Symbol) 默认关, 未移植。
来源: https://www.tradingview.com/script/cYG1lqRI-WaveTrend-LazyBear-vX-by-DGT/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_WAVETREND, neutral_score, run_kernel

META = {'id': 'tv_wavetrend',
 'name': 'WaveTrend 超卖强金叉',
 'description': 'TradingView 开源 WaveTrend vX (DGT) 移植: 波浪趋势在超卖区 (低于 -56.5) 金叉信号线时买入',
 'tags': ['TradingView', '振荡器', '超卖'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'channel_length',
             'label': '通道长度',
             'type': 'int',
             'default': 10,
             'min': 2,
             'max': 30,
             'step': 1},
            {'id': 'average_length',
             'label': '平均长度',
             'type': 'int',
             'default': 21,
             'min': 2,
             'max': 40,
             'step': 1},
            {'id': 'signal_length',
             'label': '信号长度',
             'type': 'int',
             'default': 4,
             'min': 1,
             'max': 10,
             'step': 1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_wavetrend_strong_bull"]
EXIT_SIGNALS = ["signal_tv_wavetrend_strong_bear"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvWaveTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_WAVETREND, (int(params.get("channel_length", 10)),
                                                           int(params.get("average_length", 21)), int(params.get("signal_length", 4))),
                                  (-56.5, 56.5))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_wavetrend_strong_bull",),
            exit_signal_ids=("signal_tv_wavetrend_strong_bear",),
        )


MATRIX_STRATEGY = TvWaveTrendMatrixStrategy()
