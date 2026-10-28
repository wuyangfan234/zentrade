"""UT Bot 翻多 — TradingView 开源脚本 “UT Bot Alerts” (QuantNomad v1.0, Pine v4) 逐行移植。

ATR 追踪止损 xATRTrailingStop; 买 = src>stop 且 src 上穿 stop; 卖 = src<stop 且 stop 上穿 src。src=close (Heikin Ashi 默认关)。
来源: https://www.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_UTBOT, neutral_score, run_kernel

META = {'id': 'tv_ut_bot',
 'name': 'UT Bot 翻多',
 'description': 'TradingView 开源 UT Bot Alerts (QuantNomad) 逐行移植: 收盘上穿 ATR 追踪止损线当根买入',
 'tags': ['TradingView', '趋势', '追踪止损'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'key_value',
             'label': '敏感度 (Key Value)',
             'type': 'float',
             'default': 1.0,
             'min': 0.1,
             'max': 10.0,
             'step': 0.1},
            {'id': 'atr_period',
             'label': 'ATR 周期',
             'type': 'int',
             'default': 10,
             'min': 2,
             'max': 50,
             'step': 1}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_utbot_buy"]
EXIT_SIGNALS = ["signal_tv_utbot_sell"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvUtBotMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_UTBOT, (int(params.get("atr_period", 10)),),
                                  (float(params.get("key_value", 1.0)),))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_utbot_buy",),
            exit_signal_ids=("signal_tv_utbot_sell",),
        )


MATRIX_STRATEGY = TvUtBotMatrixStrategy()
