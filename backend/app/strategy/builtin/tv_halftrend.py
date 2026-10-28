"""HalfTrend 翻多 — TradingView 开源脚本 “HalfTrend” (everget v3, Pine v6, GPL-3.0) 逐行移植。

买 = trend 由 1 翻 0 (蓝色向上箭头); 卖 = 由 0 翻 1。ATR(100) 只决定箭头位置/通道, 箭头出现条件见 tv_kernels.halftrend_1d。
来源: https://www.tradingview.com/script/U1SJ8ubc-HalfTrend/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_HALFTREND, neutral_score, run_kernel

META = {'id': 'tv_halftrend',
 'name': 'HalfTrend 翻多',
 'description': 'TradingView 开源 HalfTrend (everget) 逐行移植: 半趋势线由空转多当根买入',
 'tags': ['TradingView', '趋势'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'amplitude',
             'label': '振幅 (Amplitude)',
             'type': 'int',
             'default': 2,
             'min': 1,
             'max': 20,
             'step': 1},
            {'id': 'channel_deviation',
             'label': '通道偏离',
             'type': 'int',
             'default': 2,
             'min': 1,
             'max': 10,
             'step': 1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_halftrend_buy"]
EXIT_SIGNALS = ["signal_tv_halftrend_sell"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvHalfTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_HALFTREND, (int(params.get("amplitude", 2)),
                                                           int(params.get("channel_deviation", 2)), 0))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_halftrend_buy",),
            exit_signal_ids=("signal_tv_halftrend_sell",),
        )


MATRIX_STRATEGY = TvHalfTrendMatrixStrategy()
