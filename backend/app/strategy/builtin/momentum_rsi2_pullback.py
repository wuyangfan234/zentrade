"""动量股 RSI(2) 回调 — 强势股短线超卖, 大盘多头时次日开盘买, 先到先出 (2026-10-23 产品决策)。

规则 (原文, 2026-10-23 产品决策):
  买: 过去 60 日涨幅排全池前 20%、收盘在 50 日线上方、RSI(2) < 10, 且 SPY 在 200 日线上方 → 次日开盘买入
  卖 (先到先出): 收盘高于前一日最高价 / 盈利满 10% 止盈 / 持满 5 个交易日

在本系统里的落地口径:
  - 「全池」= 当天矩阵里的流动性股票池: 收盘 >= $5、20 日平均成交额 (收盘 * 成交量) >= pool_adv_min (默认 $2000 万,
    与系统流动性门槛 setups.json universe 一致), 剔除 ETF/基金/优先股/权证/单位/权利 (instruments 元数据, 与机会引擎
    _load_universe 同一排除口径; 元数据读不到就不剔除) 和 SPY 本身。生产每日扫描与选股页 = 全市场;
    回测页只选一批股票时, 全池 = 这批股票 (至少 POOL_MIN_SIZE 只够格才排名)。
    排名依赖全池, 所以本策略是截面策略 (META.cross_sectional=True): 机会板对账、回放必须在全市场矩阵上算信号,
    再取自选列 (只用自选算会改变分母和名次)。
  - SPY 取矩阵里的; 矩阵里没有 SPY (回测只选了几只股票) 时经只读的 app.strategy.market_data.scan_daily_columns
    读 SPY 日K 原始列 (只扫 parquet, 不触发指标重算)。
    两处都读不到或 200 日线样本不足 = 大盘状态不可知, 当天不出买点 (fail-closed, 不填零放行)。
  - 信号日收盘确认; 次日开盘成交由回测/交易口径 (entry_fill=open_t+1) 负责, 本文件不自己后移。
  - 出场三腿: 「收盘 > 前一日最高价」= exit 信号 (回测选“当日收盘”卖出口径即原规则);
    「盈利满 10%」= TAKE_PROFIT (回测引擎按 入场价*1.10 触价成交, 开盘跳过则按开盘价);
    「持满 5 个交易日」= MAX_HOLD_DAYS。
  - 覆盖缺口 (如实声明): 回测引擎沿用全系统口径, 买入当天不判离场, 买入当天就满足离场条件的笔会推到下一天;
    机会板 (3+3) 只用本策略的买点, 价位与持有按机会板统一卡 (tight-v1) 结算, 不是上面的三条出场线。
  - 用户贴的样本外成绩 (Top-10 夏普 2.26 / 396 笔 / 胜率 69.4% / DSR 0.77 等) 的出处在本系统里找不到,
    未复现; 以本系统回测页的复核结果为准。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_mean,
)
from app.backtest.matrix import valid_shift as shift

META = {
    "id": "momentum_rsi2_pullback",
    "name": "动量RSI2回调",
    "description": (
        "60日涨幅居流动性全池前20% + 收盘>MA50 + RSI(2)<10 + SPY>MA200 → 次日开盘买; "
        "收盘>前日最高 / 盈利满10% / 持满5日先到先出"
    ),
    "tags": ["动量", "回调", "RSI", "大盘过滤"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": False,
    # 截面策略: 排名依赖全池, 回放/对账必须在全市场矩阵上算 (swing_settlement.replay_rule_hits 读这个标记)
    "cross_sectional": True,
    "params": [
        {"id": "momentum_days", "label": "动量回看天数", "type": "int", "default": 60, "min": 20, "max": 120, "step": 5},
        {"id": "top_pct", "label": "动量排名前比例", "type": "float", "default": 0.20, "min": 0.05, "max": 0.5,
         "step": 0.05},
        {"id": "ma_days", "label": "个股趋势均线", "type": "int", "default": 50, "min": 10, "max": 200, "step": 5},
        {"id": "rsi_max", "label": "RSI(2)买入上限", "type": "float", "default": 10.0, "min": 1.0, "max": 30.0,
         "step": 1.0},
        {"id": "require_regime", "label": "要求SPY在长均线上方", "type": "bool", "default": True},
        {"id": "regime_ma", "label": "SPY均线天数", "type": "int", "default": 200, "min": 50, "max": 250, "step": 10},
        {"id": "pool_adv_min", "label": "排名池最低20日均成交额($)", "type": "float", "default": 20_000_000.0,
         "min": 0.0, "max": 1_000_000_000.0, "step": 5_000_000.0},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_momentum_rsi2_buy"]
EXIT_SIGNALS = ["signal_momentum_rsi2_exit"]
STOP_LOSS = None          # 原规则没有止损
TAKE_PROFIT = 0.10        # 盈利满 10% 止盈
MAX_HOLD_DAYS = 5         # 持满 5 个交易日
REGIME_SYMBOL = "SPY"
POOL_MIN_PRICE = 5.0      # 全池最低价, 与系统流动性门槛一致
POOL_MIN_SIZE = 10        # 当天全池少于这么多只 = 排名不可信, 不出买点
ADV_DAYS = 20


def _int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(params.get(key, default))
    except (TypeError, ValueError):
        v = default
    return min(max(v, lo), hi)


def _float(params: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(params.get(key, default))
    except (TypeError, ValueError):
        v = default
    if not np.isfinite(v):
        v = default
    return min(max(v, lo), hi)


def non_stock_symbols() -> frozenset[str]:
    """ETF/基金/优先股/权证/单位/权利 (instruments 元数据; 读不到 = 空集)。实现在只读的 app.strategy.market_data。"""
    try:
        from app.strategy.market_data import non_stock_symbols as _ns
        return _ns()
    except Exception:
        return frozenset()


def _above_ma(values: np.ndarray, ma_days: int) -> np.ndarray:
    """values 是一只票按时间排好、全为有限值的收盘; 返回同长布尔: 收盘 > 截至当根的 ma_days 日均线 (样本不足 = False)。"""
    v = np.asarray(values, dtype=np.float64)
    out = np.zeros(v.size, dtype=bool)
    if v.size < ma_days:
        return out
    cs = np.concatenate(([0.0], np.cumsum(v)))
    ma = (cs[ma_days:] - cs[:-ma_days]) / ma_days            # ma[k] = mean(v[k : k+ma_days])
    out[ma_days - 1:] = v[ma_days - 1:] > ma
    return out


def _regime_from_matrix(market: MarketDataMatrix, ma_days: int) -> np.ndarray | None:
    if REGIME_SYMBOL not in market.symbols:
        return None
    c = np.asarray(market.close[:, market.symbols.index(REGIME_SYMBOL)], dtype=np.float64)
    ok = np.flatnonzero(np.isfinite(c) & (c > 0))
    if ok.size < ma_days:
        return None
    out = np.zeros(market.shape[0], dtype=bool)
    out[ok] = _above_ma(c[ok], ma_days)
    return out


_REGIME_STORE_CACHE: dict = {}      # (起, 止, 均线天数) -> {日期: 是否在均线上方}; 同一窗口只读一次


def _regime_by_day_from_store(first: date, last: date, ma_days: int) -> dict[str, bool]:
    key = (first, last, ma_days)
    hit = _REGIME_STORE_CACHE.get(key)
    if hit is not None:
        return hit
    from app.strategy.market_data import (
        scan_daily_columns,  # 只扫 parquet, 不触发指标重算 (回测线程里安全)
    )
    df = scan_daily_columns(REGIME_SYMBOL, start=first - timedelta(days=int(ma_days * 1.6) + 40), end=last,
                            columns=["date", "close"])
    by_day: dict[str, bool] = {}
    if df is not None and not df.is_empty() and {"date", "close"} <= set(df.columns):
        df = df.drop_nulls(["date", "close"]).unique("date", keep="last").sort("date")
        days = [str(d)[:10] for d in df["date"].to_list()]
        closes = np.asarray(df["close"].to_list(), dtype=np.float64)
        good = np.isfinite(closes) & (closes > 0)
        days = [d for d, g in zip(days, good, strict=True) if g]
        by_day = dict(zip(days, (bool(x) for x in _above_ma(closes[good], ma_days)), strict=True))
    if not by_day:
        return by_day                                   # 读不到不缓存, 下次再试
    if len(_REGIME_STORE_CACHE) > 32:
        _REGIME_STORE_CACHE.clear()
    _REGIME_STORE_CACHE[key] = by_day
    return by_day


def _regime_from_store(market: MarketDataMatrix, ma_days: int) -> np.ndarray:
    """矩阵里没有 SPY (或不够长): 经只读 market_data 读 SPY 日K, 按日期对齐到矩阵时间轴。读不到 = 全 False。"""
    out = np.zeros(market.shape[0], dtype=bool)
    labels = [str(x)[:10] for x in market.timestamp_labels]
    if not labels:
        return out
    try:
        by_day = _regime_by_day_from_store(date.fromisoformat(labels[0]), date.fromisoformat(labels[-1]), ma_days)
        for t, d in enumerate(labels):
            out[t] = bool(by_day.get(d, False))
    except Exception:  # 大盘状态不可知 = 不出买点
        return np.zeros(market.shape[0], dtype=bool)
    return out


def regime_series(market: MarketDataMatrix, ma_days: int) -> np.ndarray:
    """SPY 收盘 > SPY ma_days 日均线 (按 SPY 自己的有效 K 线算)。不可知的日子 = False。"""
    got = _regime_from_matrix(market, ma_days)
    return got if got is not None else _regime_from_store(market, ma_days)


class MomentumRsi2PullbackMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        mom = _int(params, "momentum_days", 60, 20, 120)
        ma = _int(params, "ma_days", 50, 10, 200)
        reg = _int(params, "regime_ma", 200, 50, 250) if params.get("require_regime", True) else 0
        return max(mom, ma, reg, ADV_DAYS) + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        mom_days = _int(params, "momentum_days", 60, 20, 120)
        top_pct = _float(params, "top_pct", 0.20, 0.01, 1.0)
        ma_days = _int(params, "ma_days", 50, 10, 200)
        rsi_max = _float(params, "rsi_max", 10.0, 0.0, 100.0)
        adv_min = _float(params, "pool_adv_min", 20_000_000.0, 0.0, 1e12)

        close = market.close.astype(np.float32)
        high = market.high.astype(np.float32)
        mom = matrix_feature(market, f"momentum_{mom_days}d")
        ma = matrix_feature(market, f"ma{ma_days}")
        rsi2 = matrix_feature(market, "rsi_2")
        dollar = close * market.volume.astype(np.float32)
        adv = valid_rolling_mean(dollar, np.isfinite(dollar), ADV_DAYS)

        excluded = non_stock_symbols() | {REGIME_SYMBOL}
        is_stock = np.fromiter((sym not in excluded for sym in market.symbols), dtype=bool, count=market.shape[1])
        with np.errstate(invalid="ignore"):
            pool = (np.isfinite(mom) & np.isfinite(adv) & (adv >= adv_min) & (close >= POOL_MIN_PRICE)
                    & is_stock[None, :])
            setup = pool & np.isfinite(ma) & (close > ma) & np.isfinite(rsi2) & (rsi2 < rsi_max)
        if params.get("require_regime", True):
            setup &= regime_series(market, _int(params, "regime_ma", 200, 50, 250))[:, None]

        entry = np.zeros(market.shape, dtype=bool)
        score = np.zeros(market.shape, dtype=np.float32)
        for t in np.flatnonzero(setup.any(axis=1)):        # 只在有候选的日子算截面分位, 其余日子不出买点
            vals = mom[t, pool[t]]
            if vals.size < POOL_MIN_SIZE:
                continue
            thr = np.quantile(vals, 1.0 - top_pct)
            row = setup[t] & (mom[t] >= thr)
            if not row.any():
                continue
            entry[t] = row
            ranks = np.searchsorted(np.sort(vals), mom[t, row], side="right") / vals.size
            score[t, row] = (ranks * 100.0).astype(np.float32)   # 动量分位 (越强越靠前)

        with np.errstate(invalid="ignore"):
            exit_ = np.isfinite(close) & (close > shift(high, 1))   # 收盘高于前一日最高价

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=np.nan_to_num(score, nan=0.0),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_momentum_rsi2_buy",),
            exit_signal_ids=("signal_momentum_rsi2_exit",),
        )


MATRIX_STRATEGY = MomentumRsi2PullbackMatrixStrategy()
