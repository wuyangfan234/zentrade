"""吊灯止损翻多 — TradingView 开源脚本 “Chandelier Exit” (everget v8, Pine v6, GPL-3.0) 逐行移植,
并带 "Avize Cikisi - Ultimate Pro (Turkce)" (liter.md, 土耳其语版) v2.2 (Göktürk80, GPL-3.0) 的三个可选过滤 (默认全关 = 与 everget 原版逐根相同)。

买 = dir 由 -1 翻 1; 卖 = 由 1 翻 -1。Ultimate 的止盈阶梯/专家模式/40 只分组扫描只是显示与提醒层, 不改信号, 未移植;
“1 根确认”在上传源码里缩进已损坏 (默认也关闭), 未移植。
来源: https://www.tradingview.com/script/AqXxNS7j-Chandelier-Exit/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_CE, neutral_score, run_kernel

META = {'id': 'tv_chandelier_exit',
 'name': '吊灯止损翻多',
 'description': 'TradingView 开源 Chandelier Exit (everget) 逐行移植, 可选 CE Ultimate 的量能/EMA 趋势/ATR 扩张过滤 (默认关闭)',
 'tags': ['TradingView', '趋势', '吊灯止损'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'atr_period',
             'label': 'ATR 周期',
             'type': 'int',
             'default': 22,
             'min': 2,
             'max': 100,
             'step': 1},
            {'id': 'multiplier',
             'label': 'ATR 倍数',
             'type': 'float',
             'default': 3.0,
             'min': 0.5,
             'max': 10.0,
             'step': 0.1},
            {'id': 'use_close', 'label': '用收盘价取极值', 'type': 'bool', 'default': True},
            {'id': 'use_volume_filter', 'label': '量能过滤 (Ultimate)', 'type': 'bool', 'default': False},
            {'id': 'volume_multiplier',
             'label': '量能倍数',
             'type': 'float',
             'default': 1.2,
             'min': 1.0,
             'max': 5.0,
             'step': 0.1},
            {'id': 'use_trend_filter', 'label': 'EMA 趋势过滤 (Ultimate)', 'type': 'bool', 'default': False},
            {'id': 'ema_length',
             'label': '趋势 EMA 周期',
             'type': 'int',
             'default': 200,
             'min': 20,
             'max': 400,
             'step': 1},
            {'id': 'use_atr_filter', 'label': 'ATR 扩张过滤 (Ultimate)', 'type': 'bool', 'default': False},
            {'id': 'atr_sma_length',
             'label': 'ATR 均线周期',
             'type': 'int',
             'default': 20,
             'min': 5,
             'max': 100,
             'step': 1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_ce_buy"]
EXIT_SIGNALS = ["signal_tv_ce_sell"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvChandelierExitMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ints = (int(params.get("atr_period", 22)), int(bool(params.get("use_close", True))),
                int(bool(params.get("use_volume_filter", False))), int(bool(params.get("use_trend_filter", False))),
                int(params.get("ema_length", 200)), int(bool(params.get("use_atr_filter", False))),
                int(params.get("atr_sma_length", 20)))
        entry, exit_ = run_kernel(market, KIND_CE, ints,
                                  (float(params.get("multiplier", 3.0)), float(params.get("volume_multiplier", 1.2))))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_ce_buy",),
            exit_signal_ids=("signal_tv_ce_sell",),
        )


MATRIX_STRATEGY = TvChandelierExitMatrixStrategy()
