"""挤压释放 — TradingView 开源脚本 “Squeeze Momentum Indicator [LazyBear]” 作者 v2 修正版 (BB 倍数笔误已修) 移植。

指标本身不出买卖点; 信号按作者说明里 Carter 的用法: 进入挤压 (黑十字) 后第一个释放 (灰十字) 且动能 val>0 → 买;
动能柱由升转降 (颜色变化) → 卖。
来源: https://www.tradingview.com/script/nqQ1DT5a-Squeeze-Momentum-Indicator-LazyBear/ (修正版 pastebin.com/UCpcX8d7)"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_SQUEEZE, neutral_score, run_kernel

META = {'id': 'tv_squeeze_momentum',
 'name': '挤压释放做多',
 'description': 'TradingView 开源 Squeeze Momentum (LazyBear v2) 移植: 布林带收进肯特纳通道后首次释放且动能为正时买入 (用法取自作者说明)',
 'tags': ['TradingView', '波动率', '挤压'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'bb_length',
             'label': '布林长度',
             'type': 'int',
             'default': 20,
             'min': 5,
             'max': 50,
             'step': 1},
            {'id': 'bb_mult',
             'label': '布林倍数',
             'type': 'float',
             'default': 2.0,
             'min': 0.5,
             'max': 4.0,
             'step': 0.1},
            {'id': 'kc_length',
             'label': '肯特纳长度',
             'type': 'int',
             'default': 20,
             'min': 5,
             'max': 50,
             'step': 1},
            {'id': 'kc_mult',
             'label': '肯特纳倍数',
             'type': 'float',
             'default': 1.5,
             'min': 0.5,
             'max': 4.0,
             'step': 0.1},
            {'id': 'use_true_range', 'label': '肯特纳用真实波幅', 'type': 'bool', 'default': True}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_squeeze_fire_long"]
EXIT_SIGNALS = ["signal_tv_squeeze_mom_down"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvSqueezeMomentumMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_SQUEEZE, (int(params.get("bb_length", 20)), int(params.get("kc_length", 20)),
                                                         int(bool(params.get("use_true_range", True)))),
                                  (float(params.get("bb_mult", 2.0)), float(params.get("kc_mult", 1.5))))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_squeeze_fire_long",),
            exit_signal_ids=("signal_tv_squeeze_mom_down",),
        )


MATRIX_STRATEGY = TvSqueezeMomentumMatrixStrategy()
