"""SMC 波段结构转多 — TradingView 开源脚本 “Smart Money Concepts [LuxAlgo]” (CC BY-NC-SA 4.0, Pine v5) 波段结构部分移植。

leg(size) 识别摆动高/低点; 收盘上穿未被穿越的摆动高点 = 多头结构突破 (原趋势为空 → CHoCH, 否则 BOS)。
默认只取 CHoCH (由空转多); 关闭 “只取转折” 时 BOS 也买。卖 = 空头结构突破 (BOS 或 CHoCH)。订单块/FVG/等高低只是画图层, 未移植。
来源: https://www.tradingview.com/script/CnB3fSph-Smart-Money-Concepts-SMC-LuxAlgo/"""

import numpy as np

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix
from app.backtest.tv_kernels import KIND_SMC, neutral_score, run_kernel

META = {'id': 'tv_smc_structure',
 'name': 'SMC 波段结构转多',
 'description': 'TradingView 开源 Smart Money Concepts (LuxAlgo) 波段结构移植: 收盘突破摆动高点且结构由空转多 (CHoCH) 时买入',
 'tags': ['TradingView', '结构', 'SMC'],
 'asset_types': ['stock'],
 'timeframes': ['1d'],
 'params': [{'id': 'swing_length',
             'label': '波段长度',
             'type': 'int',
             'default': 50,
             'min': 10,
             'max': 70,
             'step': 1},
            {'id': 'choch_only', 'label': '只取转折 (CHoCH)', 'type': 'bool', 'default': True}],
 'scoring': {},
 'order_by': 'score',
 'descending': True,
 'limit': 100,
 'research_only': False}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_tv_smc_bull_structure"]
EXIT_SIGNALS = ["signal_tv_smc_bear_structure"]
STOP_LOSS = None          # 源脚本的止损是指标线本身; 系统统一 7 日卡用 2ATR 失效位
MAX_HOLD_DAYS = 20
WARMUP_BARS = 74          # 与现有最长预热持平, 不放大生产共享历史窗口 (见 tests/test_tv_strategies.py 截断对照)


class TvSmcStructureMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return WARMUP_BARS

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry, exit_ = run_kernel(market, KIND_SMC, (int(params.get("swing_length", 50)),
                                                     int(bool(params.get("choch_only", True)))))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=neutral_score(market),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_tv_smc_bull_structure",),
            exit_signal_ids=("signal_tv_smc_bear_structure",),
        )


MATRIX_STRATEGY = TvSmcStructureMatrixStrategy()
