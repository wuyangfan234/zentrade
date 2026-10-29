"""Phantom 趋势换挡买入 — 用户提供的 Pine 源码 “Phantom Flow Core 中文优化版|美股1周波段” (Pine v6, 1179 行) SECTION 1 逐行移植 (日线)。

图上唯一标“买入”的信号: shiftTrend 由 -1 翻 1 (ATR 14 x 3.2, 价格源 hl2, 内置 ATR)。算法与 Supertrend 相同, 参数不同。
与现有 phantom_4h (vendor 4h 简化扫描: Shift + EMA50 + 内部结构 + MACD/RSI/ADX 打分) 不是同一套规则, 互不替代。"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_SUPERTREND, neutral_score, run_kernel

META = {'id': 'tv_phantom_shift',
 'name': 'Phantom 趋势换挡买入',
 'description': 'Phantom Flow Core Pine 源码 SECTION 1 逐行移植 (日线): 趋势换挡线由空翻多当根买入',
 'tags': ['Phantom', '趋势', 'TradingView'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'atr_period',
             'label': 'ATR 周期',
             'type': 'int',
             'default': 14,
             'min': 2,
             'max': 50,
             'step': 1},
            {'id': 'multiplier',
             'label': 'ATR 倍数',
             'type': 'float',
             'default': 3.2,
             'min': 0.5,
             'max': 10.0,
             'step': 0.1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_phantom_shift_buy"]
EXIT_SIGNALS = ["signal_tv_phantom_shift_sell"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvPhantomShiftMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_SUPERTREND, (int(params.get("atr_period", 14)),),
                                  (float(params.get("multiplier", 3.2)),))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_phantom_shift_buy",),
            exit_signal_ids=("signal_tv_phantom_shift_sell",),
        )


MATRIX_STRATEGY = TvPhantomShiftMatrixStrategy()
