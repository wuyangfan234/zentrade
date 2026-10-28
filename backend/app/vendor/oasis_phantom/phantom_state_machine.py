"""Phantom Flow Core 方向-动作状态机 Python 复刻（日线批量扫描用）。

与 TradingView 最新版「Phantom Flow Core 中文优化版｜美股1周波段」（Pine v6，1179 行）逐项对齐：
- Phantom Shift: ATR(14) x 3.2, src=hl2, ta.atr (Wilder RMA) [SECTION 1，逐行一致]
- OSC: maBase = EMA(hl2, 50)；oscDiff = hl2 - maBase；osc = HMA(change(oscDiff/range, 21), 10) [SECTION 3]
  归一化采用 |oscDiff| 绝对值 1000 根 99 分位 + 下限保护（偏离 Pine 的有符号分位，防止下跌股分位≈0 失真，经用户确认）
- Trail: Chandelier 22, ATR(14) x 3，独立 trlDir 方向系统，close[1] 棘轮、击穿即重置 [SECTION 4]
- Power Levels: pivot(5,5) 聚类法，0.5*ATR 容差加权合并、触及次数计数、记忆 40 点、每侧取最强 3 区 [SECTION 5]
- 结构: LuxAlgo leg() 之字检测（internal=5 / swing=34），共振过滤（影线比）门控 bias 翻转 [SECTION 2]
  注：Pine 默认关闭内部结构显示；扫描器始终计算 internal_bias 作为执行结构（扫描器增强，刻意保留）
- 风险护盾: ATR percentrank(100)>=80 / 乖离>2.5ATR / 巨K>2ATR / 跳空>0.5ATR，四路计数 [SECTION 6]
- 仪表盘: 五灯（Shift/波段结构/OSC/EMA50位置/日线EMA20>EMA50）+ 综合动作 [SECTION 7]

扫描器增强（Pine 中不存在，刻意保留）：
- 3 根触发窗口 + 消费位；触发类型：Shift翻多/支撑反应/压力突破/内部BOS/CHoCH（做空镜像）
- ADX/DMI（Wilder 14/14）；READY/触发/准就绪分层；做空独立触发窗口与 short_trail 失效
- OSC 未就绪（<1000 根）明确标记 osc_ready=false
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import pandas as pd

BULLISH, BEARISH = 1, -1



# ---------------------------------------------------------------------------
# 基础指标
# ---------------------------------------------------------------------------

def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, min_periods=length, adjust=False).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return rma(tr, length)


def hma(series: pd.Series, length: int) -> pd.Series:
    half = max(int(length / 2), 1)
    root = max(int(round(np.sqrt(length))), 1)
    wma_half = series.rolling(half).apply(lambda w: np.dot(w, np.arange(1, len(w) + 1)) / np.arange(1, len(w) + 1).sum(), raw=True)
    wma_full = series.rolling(length).apply(lambda w: np.dot(w, np.arange(1, len(w) + 1)) / np.arange(1, len(w) + 1).sum(), raw=True)
    diff = 2 * wma_half - wma_full
    return diff.rolling(root).apply(lambda w: np.dot(w, np.arange(1, len(w) + 1)) / np.arange(1, len(w) + 1).sum(), raw=True)


def dmi(df: pd.DataFrame, length: int = 14, smoothing: int = 14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atrv = atr(df, length)
    plus_di = 100 * rma(plus_dm, length) / atrv
    minus_di = 100 * rma(minus_dm, length) / atrv
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = rma(dx, smoothing)
    return plus_di, minus_di, adx


def percentrank(series: pd.Series, length: int) -> pd.Series:
    """ta.percentrank：当前值在过去 length 根中排在百分之几（0-100）。"""
    def _rank(w):
        return 100.0 * np.sum(w[:-1] <= w[-1]) / (len(w) - 1) if len(w) > 1 else np.nan
    return series.rolling(length).apply(_rank, raw=True)


# ---------------------------------------------------------------------------
# Phantom Shift（与 Pine SECTION 1 逐行一致）
# ---------------------------------------------------------------------------

def phantom_shift(df: pd.DataFrame, length: int = 14, mult: float = 3.2):
    src = (df["high"] + df["low"]) / 2
    a = atr(df, length)
    up_base = src - mult * a
    dn_base = src + mult * a
    trend = np.ones(len(df), dtype=int)
    up = np.full(len(df), np.nan)
    dn = np.full(len(df), np.nan)
    for i in range(len(df)):
        up_prev = up[i - 1] if i > 0 and not np.isnan(up[i - 1]) else up_base.iloc[i]
        dn_prev = dn[i - 1] if i > 0 and not np.isnan(dn[i - 1]) else dn_base.iloc[i]
        prev_close = df["close"].iloc[i - 1] if i > 0 else df["close"].iloc[i]
        up[i] = max(up_base.iloc[i], up_prev) if prev_close > up_prev else up_base.iloc[i]
        dn[i] = min(dn_base.iloc[i], dn_prev) if prev_close < dn_prev else dn_base.iloc[i]
        if i > 0:
            if trend[i - 1] == -1 and df["close"].iloc[i] > dn[i - 1]:
                trend[i] = 1
            elif trend[i - 1] == 1 and df["close"].iloc[i] < up[i - 1]:
                trend[i] = -1
            else:
                trend[i] = trend[i - 1]
    return pd.Series(trend, index=df.index), pd.Series(up, index=df.index), pd.Series(dn, index=df.index)


# ---------------------------------------------------------------------------
# leg() 之字结构检测（与 Pine SECTION 2 getCurrentStructure 一致）
# ---------------------------------------------------------------------------

def leg_pivots(df: pd.DataFrame, size: int):
    """LuxAlgo leg(size) 之字检测。

    newLegHigh: high[i-size] > max(high[i-size+1..i])  → 确认一个摆动低点 low[i-size]
    newLegLow:  low[i-size]  < min(low[i-size+1..i])   → 确认一个摆动高点 high[i-size]
    返回 (pivot_highs, pivot_lows)：在确认柱 i 标注，值为摆动柱（i-size）的价格。
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    ph = pd.Series(np.nan, index=df.index)
    pl = pd.Series(np.nan, index=df.index)
    for i in range(size, n):
        seg_h = highs[i - size + 1 : i + 1]
        seg_l = lows[i - size + 1 : i + 1]
        ref_h = highs[i - size]
        ref_l = lows[i - size]
        new_leg_high = ref_h > seg_h.max() if len(seg_h) else False
        new_leg_low = ref_l < seg_l.min() if len(seg_l) else False
        if new_leg_high and not new_leg_low:
            # 新腿向下 => 确认摆动低点（pivot low = low[size]）
            pl.iloc[i] = ref_l
        elif new_leg_low and not new_leg_high:
            # 新腿向上 => 确认摆动高点（pivot high = high[size]）
            ph.iloc[i] = ref_h
        # 同根同时成立（极端宽幅）按 Pine if/else 顺序：pivotLow 分支优先（else 分支为 high）
        elif new_leg_high and new_leg_low:
            pl.iloc[i] = ref_l
    return ph, pl


def structure_bias(df: pd.DataFrame, ph: pd.Series, pl: pd.Series,
                   gate_bull: pd.Series | None = None, gate_bear: pd.Series | None = None):
    """pivot 上破/下破驱动 bias 翻转；gate 序列可门控翻转（共振过滤）。"""
    n = len(df)
    bias = np.zeros(n, dtype=int)
    bull_event = np.zeros(n, dtype=bool)
    bear_event = np.zeros(n, dtype=bool)
    cur_h, cur_l = np.nan, np.nan
    h_crossed, l_crossed = True, True
    b = 0
    closes = df["close"].values
    for i in range(n):
        if not np.isnan(ph.iloc[i]):
            cur_h = ph.iloc[i]
            h_crossed = False
        if not np.isnan(pl.iloc[i]):
            cur_l = pl.iloc[i]
            l_crossed = False
        c = closes[i]
        if i > 0 and not np.isnan(cur_h) and not h_crossed and c > cur_h and closes[i - 1] <= cur_h:
            allowed = True if gate_bull is None else bool(gate_bull.iloc[i])
            if allowed:
                bull_event[i] = True
                h_crossed = True
                b = BULLISH
        if i > 0 and not np.isnan(cur_l) and not l_crossed and c < cur_l and closes[i - 1] >= cur_l:
            allowed = True if gate_bear is None else bool(gate_bear.iloc[i])
            if allowed:
                bear_event[i] = True
                l_crossed = True
                b = BEARISH
        bias[i] = b
    return pd.Series(bias, index=df.index), pd.Series(bull_event, index=df.index), pd.Series(bear_event, index=df.index)


# ---------------------------------------------------------------------------
# Power Levels 聚类（与 Pine SECTION 5 一致）
# ---------------------------------------------------------------------------

def power_levels(df: pd.DataFrame, ph: pd.Series, pl: pd.Series, atr14: pd.Series,
                 cluster_mult: float = 0.5, memory: int = 40, max_zones: int = 3):
    """pivot 聚类：0.5*ATR 容差内加权合并并累计触及；每侧取 close 上/下方触及最多的 max_zones 区。

    返回每根的 (res_zones, sup_zones)：list[(price, touches)]，按触及降序。
    """
    n = len(df)
    res_price: list[float] = []
    res_touch: list[int] = []
    sup_price: list[float] = []
    sup_touch: list[int] = []
    res_out: list[list] = [[] for _ in range(n)]
    sup_out: list[list] = [[] for _ in range(n)]
    for i in range(n):
        tol = cluster_mult * atr14.iloc[i] if not np.isnan(atr14.iloc[i]) else np.nan
        if not np.isnan(ph.iloc[i]) and not np.isnan(tol):
            merged = False
            for k in range(len(res_price)):
                if abs(res_price[k] - ph.iloc[i]) <= tol:
                    t = res_touch[k]
                    res_price[k] = (res_price[k] * t + ph.iloc[i]) / (t + 1)
                    res_touch[k] = t + 1
                    merged = True
                    break
            if not merged:
                res_price.append(float(ph.iloc[i]))
                res_touch.append(1)
                if len(res_price) > memory:
                    res_price.pop(0)
                    res_touch.pop(0)
        if not np.isnan(pl.iloc[i]) and not np.isnan(tol):
            merged = False
            for k in range(len(sup_price)):
                if abs(sup_price[k] - pl.iloc[i]) <= tol:
                    t = sup_touch[k]
                    sup_price[k] = (sup_price[k] * t + pl.iloc[i]) / (t + 1)
                    sup_touch[k] = t + 1
                    merged = True
                    break
            if not merged:
                sup_price.append(float(pl.iloc[i]))
                sup_touch.append(1)
                if len(sup_price) > memory:
                    sup_price.pop(0)
                    sup_touch.pop(0)
        c = df["close"].iloc[i]
        above = sorted([(p, t) for p, t in zip(res_price, res_touch) if p > c], key=lambda x: -x[1])[:max_zones]
        below = sorted([(p, t) for p, t in zip(sup_price, sup_touch) if p < c], key=lambda x: -x[1])[:max_zones]
        res_out[i] = above
        sup_out[i] = below
    return res_out, sup_out


# ---------------------------------------------------------------------------
# Trail（与 Pine SECTION 4 一致：独立 trlDir、close[1] 棘轮、击穿重置）
# ---------------------------------------------------------------------------

def phantom_trail(df: pd.DataFrame, length: int = 22, mult: float = 3.0):
    a = atr(df, 14)
    long_raw = df["high"].rolling(length).max() - mult * a
    short_raw = df["low"].rolling(length).min() + mult * a
    n = len(df)
    long_stop = np.full(n, np.nan)
    short_stop = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)
    for i in range(n):
        lr, sr = long_raw.iloc[i], short_raw.iloc[i]
        if np.isnan(lr) or np.isnan(sr):
            direction[i] = direction[i - 1] if i > 0 else 1
            continue
        if i == 0 or np.isnan(long_stop[i - 1]):
            long_stop[i] = lr
            short_stop[i] = sr
            direction[i] = direction[i - 1] if i > 0 else 1
            continue
        prev_close = df["close"].iloc[i - 1]
        long_stop[i] = max(lr, long_stop[i - 1]) if prev_close > long_stop[i - 1] else lr
        short_stop[i] = min(sr, short_stop[i - 1]) if prev_close < short_stop[i - 1] else sr
        if direction[i - 1] == 1 and df["close"].iloc[i] < long_stop[i - 1]:
            direction[i] = -1
        elif direction[i - 1] == -1 and df["close"].iloc[i] > short_stop[i - 1]:
            direction[i] = 1
        else:
            direction[i] = direction[i - 1]
    return (pd.Series(direction, index=df.index),
            pd.Series(long_stop, index=df.index),
            pd.Series(short_stop, index=df.index))


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    ticker: str
    bars: int = 0
    data_ok: bool = False
    error: str = ""
    date: str = ""
    close: float = np.nan
    direction: str = ""
    action: str = ""
    trigger: str = ""
    trigger_age: int = 0
    stage: str = ""
    momentum: str = ""
    risk: str = ""
    protection: float = np.nan
    score: float = 0.0
    adx: float = np.nan
    atr_pct: float = np.nan
    gap_pct: float = np.nan
    ext_atr: float = np.nan
    osc: float = np.nan
    osc_ready: bool = False
    long_trail: float = np.nan
    power_resistance_top: float = np.nan
    power_support: float = np.nan
    buy_zone_low: float = np.nan
    buy_zone_high: float = np.nan
    stop: float = np.nan
    risk_count: int = 0
    risk_flags: str = ""
    dash_score: int = 0
    dash_action: str = ""
    notes: list = field(default_factory=list)
    short_action: str = ""
    short_trigger: str = ""
    short_trigger_age: int = 0
    short_stage: str = ""
    short_momentum: str = ""
    short_score: float = 0.0
    short_protection: float = np.nan


# ---------------------------------------------------------------------------
# 单标的扫描
# ---------------------------------------------------------------------------

def scan_one(df: pd.DataFrame, ticker: str) -> ScanResult:
    r = ScanResult(ticker=ticker, bars=len(df))
    df = df.dropna(subset=["open", "high", "low", "close"]).copy()
    if len(df) < 120:
        r.error = f"数据不足({len(df)}根)"
        return r

    # ---- 指标层 ----
    atr14 = atr(df, 14)
    shift_trend, up_shift, dn_shift = phantom_shift(df, 14, 3.2)
    plus_di, minus_di, adx = dmi(df, 14, 14)
    hl2 = (df["high"] + df["low"]) / 2

    # A1: OSC — EMA(hl2,50) 基准，|diff| 1000 根 99 分位归一化（下限保护）
    ma_base = ema(hl2, 50)
    osc_diff = hl2 - ma_base
    osc_range = osc_diff.abs().rolling(1000, min_periods=1000).quantile(0.99, interpolation="linear")
    osc_ready_s = osc_range.notna() & (osc_range.abs() > 1e-6)
    safe_ratio = pd.Series(np.where(osc_ready_s, osc_diff / osc_range, np.nan), index=df.index)
    osc = hma(safe_ratio.diff(21), 10)

    # A4: 结构 — leg() 之字检测（internal=5 / swing=34）+ 共振过滤门控 bias
    upper_wick = df["high"] - pd.concat([df["close"], df["open"]], axis=1).max(axis=1)
    lower_wick = pd.concat([df["close"], df["open"]], axis=1).min(axis=1) - df["low"]
    bullish_bar = upper_wick > lower_wick
    bearish_bar = upper_wick < lower_wick
    iph, ipl = leg_pivots(df, 5)
    sph, spl = leg_pivots(df, 34)
    internal_bias, internal_bull_evt, internal_bear_evt = structure_bias(df, iph, ipl, bullish_bar, bearish_bar)
    swing_bias, swing_bull_evt, swing_bear_evt = structure_bias(df, sph, spl)

    # A3: Power Levels 聚类（用 internal leg pivot，与 Pine ta.pivothigh(5,5) 近似同尺度）
    ph5 = pivots_symmetric(df["high"], 5, 5)
    pl5 = pivots_symmetric(df["low"], 5, 5)
    res_zones, sup_zones = power_levels(df, ph5, pl5, atr14)

    # A2: Trail — Chandelier 22 x ATR(14) x 3，独立方向
    trl_dir, long_trail, short_trail = phantom_trail(df, 22, 3.0)

    # 触发事件
    n = len(df)
    shift_buy = np.zeros(n, dtype=bool)
    shift_sell = np.zeros(n, dtype=bool)
    for i in range(1, n):
        shift_buy[i] = shift_trend.iloc[i] == 1 and shift_trend.iloc[i - 1] == -1
        shift_sell[i] = shift_trend.iloc[i] == -1 and shift_trend.iloc[i - 1] == 1

    power_support_reaction = np.zeros(n, dtype=bool)
    power_resistance_break = np.zeros(n, dtype=bool)
    power_resistance_rejection = np.zeros(n, dtype=bool)
    power_support_break = np.zeros(n, dtype=bool)
    for i in range(1, n):
        half_w = 0.25 * atr14.iloc[i] if not np.isnan(atr14.iloc[i]) else 0.0
        # 支撑反应（多）：触区后收阳回升
        for sup, _touches in sup_zones[i - 1] if i > 0 else []:
            zone_lo, zone_hi = sup - half_w, sup + half_w
            if (df["low"].iloc[i] <= zone_hi and df["close"].iloc[i] > zone_hi
                    and df["close"].iloc[i] > df["open"].iloc[i]):
                power_support_reaction[i] = True
            # 支撑跌破（空）：收破区下沿且前收在区上
            if df["close"].iloc[i] < zone_lo and df["close"].iloc[i - 1] >= zone_lo:
                power_support_break[i] = True
        for res, _touches in res_zones[i - 1] if i > 0 else []:
            zone_lo, zone_hi = res - half_w, res + half_w
            # 压力突破（多）：收破区上沿且前收在区下
            if df["close"].iloc[i] > zone_hi and df["close"].iloc[i - 1] <= zone_hi:
                power_resistance_break[i] = True
            # 压力区反应（空）：触区后收阴回落（上影冲高回落）
            if (df["high"].iloc[i] >= zone_lo and df["close"].iloc[i] < zone_lo
                    and df["close"].iloc[i] < df["open"].iloc[i]):
                power_resistance_rejection[i] = True

    # A5: 风险护盾（四路计数）
    atr_rank = percentrank(atr14, 100)
    rsk_dev = df["close"] - ma_base
    gap_abs = (df["open"] - df["close"].shift(1)).abs()
    rsk_high_vol = atr_rank >= 80
    rsk_extended = rsk_dev.abs() > 2.5 * atr14
    rsk_huge_bar = (df["high"] - df["low"]) > 2 * atr14
    rsk_gap = gap_abs > 0.5 * atr14
    risk_count_s = (rsk_high_vol.astype(int) + rsk_extended.astype(int)
                    + rsk_huge_bar.astype(int) + rsk_gap.astype(int))

    # A6: 仪表盘五灯（日线即本周期，D-EMA20/50 用已确认上一根）
    d_ema20 = ema(df["close"], 20).shift(1)
    d_ema50 = ema(df["close"], 50).shift(1)
    dash_l1 = shift_trend == 1
    dash_l2 = swing_bias == BULLISH
    dash_l3 = osc > 0
    dash_l4 = df["close"] > ma_base
    dash_l5 = d_ema20 > d_ema50
    dash_score_s = (dash_l1.astype(int) + dash_l2.astype(int) + dash_l3.astype(int)
                    + dash_l4.astype(int) + dash_l5.fillna(False).astype(int))

    # 扫描器增强：跳空>4% 冷静 2 根（与风险护盾并存）
    gap_pct_s = (df["open"] / df["close"].shift(1) - 1).abs()
    atr_pct_s = atr14 / df["close"] * 100
    long_ext = (df["close"] - ma_base) / atr14

    # ---- 状态机回放（多/空镜像，3 根窗口 + 消费位）----
    last_trigger_bar = -1
    last_trigger_type = ""
    consumed_bar = -1
    last_short_trigger_bar = -1
    last_short_trigger_type = ""
    consumed_short_bar = -1
    last_gap_bar = -1

    for j in range(n):
        if shift_buy[j]:
            last_trigger_bar, last_trigger_type = j, "Shift翻多"
        if power_support_reaction[j]:
            last_trigger_bar, last_trigger_type = j, "支撑区反应"
        if power_resistance_break[j]:
            last_trigger_bar, last_trigger_type = j, "压力区突破"
        if internal_bull_evt.iloc[j]:
            last_trigger_bar, last_trigger_type = j, "内部结构转多"
        if shift_sell[j]:
            last_short_trigger_bar, last_short_trigger_type = j, "Shift翻空"
        if power_resistance_rejection[j]:
            last_short_trigger_bar, last_short_trigger_type = j, "压力区反应"
        if power_support_break[j]:
            last_short_trigger_bar, last_short_trigger_type = j, "支撑区跌破"
        if internal_bear_evt.iloc[j]:
            last_short_trigger_bar, last_short_trigger_type = j, "内部结构转空"

        if not np.isnan(gap_pct_s.iloc[j]) and gap_pct_s.iloc[j] > 0.04:
            last_gap_bar = j

        # Trail 失效（多：跌破 long_trail 前一根；空：上破 short_trail 前一根）— 各自清除本向触发
        trail_invalid = False
        short_trail_invalid = False
        if j > 0:
            if not np.isnan(long_trail.iloc[j - 1]) and df["close"].iloc[j] < long_trail.iloc[j - 1]:
                trail_invalid = True
            if not np.isnan(short_trail.iloc[j - 1]) and df["close"].iloc[j] > short_trail.iloc[j - 1]:
                short_trail_invalid = True
        if trail_invalid and shift_trend.iloc[j] == 1:
            last_trigger_bar, last_trigger_type = -1, ""
        if short_trail_invalid and shift_trend.iloc[j] == -1:
            last_short_trigger_bar, last_short_trigger_type = -1, ""

        trigger_fresh = last_trigger_bar >= 0 and (j - last_trigger_bar) < 3
        trigger_avail = trigger_fresh and last_trigger_bar != consumed_bar
        short_trigger_fresh = last_short_trigger_bar >= 0 and (j - last_short_trigger_bar) < 3
        short_trigger_avail = short_trigger_fresh and last_short_trigger_bar != consumed_short_bar
        gap_cooldown = last_gap_bar >= 0 and (j - last_gap_bar) < 2

        momentum_known = bool(osc_ready_s.iloc[j]) and not np.isnan(osc.iloc[j]) and j > 0 and not np.isnan(osc.iloc[j - 1])
        momentum_improving = momentum_known and osc.iloc[j] > osc.iloc[j - 1]
        internal_aligned = internal_bias.iloc[j] == BULLISH
        direction = BULLISH if shift_trend.iloc[j] == 1 else BEARISH

        ready = direction == BULLISH and internal_aligned and trigger_avail and momentum_improving
        if ready:
            consumed_bar = last_trigger_bar

        short_internal_aligned = internal_bias.iloc[j] == BEARISH
        short_momentum_improving = momentum_known and osc.iloc[j] < osc.iloc[j - 1]
        short_ready = shift_trend.iloc[j] == -1 and short_internal_aligned and short_trigger_avail and short_momentum_improving
        if short_ready:
            consumed_short_bar = last_short_trigger_bar

        if j == n - 1:
            # ---- 记录最后状态 ----
            r.date = str(df.index[j].date())
            r.close = float(df["close"].iloc[j])
            r.adx = float(adx.iloc[j]) if not np.isnan(adx.iloc[j]) else np.nan
            r.atr_pct = float(atr_pct_s.iloc[j]) if not np.isnan(atr_pct_s.iloc[j]) else np.nan
            r.gap_pct = float(gap_pct_s.iloc[j]) if not np.isnan(gap_pct_s.iloc[j]) else np.nan
            r.ext_atr = float(long_ext.iloc[j]) if not np.isnan(long_ext.iloc[j]) else np.nan
            r.osc = float(osc.iloc[j]) if not np.isnan(osc.iloc[j]) else np.nan
            r.osc_ready = bool(osc_ready_s.iloc[j])
            r.long_trail = float(long_trail.iloc[j]) if not np.isnan(long_trail.iloc[j]) else np.nan
            res_now = res_zones[j]
            sup_now = sup_zones[j]
            r.power_resistance_top = float(res_now[0][0] + 0.25 * atr14.iloc[j]) if res_now and not np.isnan(atr14.iloc[j]) else np.nan
            r.power_support = float(sup_now[0][0]) if sup_now else np.nan
            r.trigger = last_trigger_type if trigger_fresh else ""
            r.trigger_age = (j - last_trigger_bar + 1) if trigger_fresh else 0
            r.risk_count = int(risk_count_s.iloc[j])
            flags = []
            if bool(rsk_high_vol.iloc[j]):
                flags.append("高波动")
            if bool(rsk_extended.iloc[j]):
                flags.append("乖离过大↑" if rsk_dev.iloc[j] > 0 else "乖离过大↓")
            if bool(rsk_huge_bar.iloc[j]):
                flags.append("异常振幅")
            if bool(rsk_gap.iloc[j]):
                flags.append("跳空缺口")
            r.risk_flags = " ".join(flags)
            r.risk = f"风险护盾{r.risk_count}项" if r.risk_count > 0 else ""
            r.dash_score = int(dash_score_s.iloc[j])
            r.dash_action = ("积极做多" if r.dash_score >= 4 else "轻仓试多" if r.dash_score == 3
                             else "等待观望" if r.dash_score == 2 else "做空/规避")

            if direction != BULLISH:
                r.direction = "↓ 主看空"
                r.action = "空头趋势，等待做多转折"
            elif trail_invalid:
                r.direction = "↑ 主看多"
                r.action = "多头保护失效：有仓退出，无仓暂停"
            elif gap_cooldown:
                r.direction = "↑ 主看多"
                r.action = "跳空消化中，等待稳定不追多"
            elif (not np.isnan(long_ext.iloc[j])) and long_ext.iloc[j] > 2.5:
                r.direction = "↑ 主看多"
                r.action = "位置过远，等待回踩不追多"
            elif not internal_aligned:
                r.direction = "↑ 主看多"
                r.action = "等待内部结构转多"
            elif trigger_avail and momentum_improving:
                r.direction = "↑ 主看多"
                r.action = "多头条件齐备｜按预案评估入场"
            elif trigger_avail:
                r.direction = "↑ 主看多"
                r.action = "价格已触发，等待动能转强"
            elif not momentum_improving:
                r.direction = "↑ 主看多"
                r.action = "等待多头动能修复"
            else:
                r.direction = "↑ 主看多"
                r.action = "等待支撑反应或压力突破"

            short_action = ""
            short_trigger_out = ""
            short_trigger_age = 0
            short_stage = ""
            short_momentum = ""
            short_score = 0.0
            short_protection = np.nan
            if shift_trend.iloc[j] == -1:
                if short_trail_invalid:
                    short_action = "空头保护失效：有仓退出，无仓暂停"
                elif gap_cooldown:
                    short_action = "跳空消化中，等待稳定不追空"
                elif not short_internal_aligned:
                    short_action = "等待内部结构转空"
                elif short_trigger_avail and short_momentum_improving:
                    short_action = "空头条件齐备｜按预案评估做空"
                elif short_trigger_avail:
                    short_action = "价格已触发，等待空头动能转强"
                elif not short_momentum_improving:
                    short_action = "等待空头动能修复"
                else:
                    short_action = "等待压力反应或支撑跌破"
                short_trigger_out = last_short_trigger_type if short_trigger_fresh else ""
                short_trigger_age = (j - last_short_trigger_bar + 1) if short_trigger_fresh else 0
                short_stage = stage_text(shift_trend.iloc[j], internal_bias.iloc[j], adx.iloc[j], short=True)
                short_momentum = momentum_text(momentum_known, short_momentum_improving, bool(osc_ready_s.iloc[j]))
                short_protection = float(short_trail.iloc[j]) if not np.isnan(short_trail.iloc[j]) else np.nan

            r.stage = stage_text(shift_trend.iloc[j], internal_bias.iloc[j], adx.iloc[j])
            r.momentum = momentum_text(momentum_known, momentum_improving, bool(osc_ready_s.iloc[j]))
            r.protection = float(long_trail.iloc[j]) if not np.isnan(long_trail.iloc[j]) else np.nan
            r.data_ok = True

            # 打分（扫描器增强）
            if r.action.startswith("多头条件齐备"):
                score = 70
                score += {"Shift翻多": 15, "支撑区反应": 12, "压力区突破": 12, "内部结构转多": 10}.get(r.trigger, 8)
                score += min(max(r.adx - 20, 0), 15) if not np.isnan(r.adx) else 0
                if not np.isnan(r.atr_pct) and r.atr_pct > 6:
                    score -= 10
                if not np.isnan(r.ext_atr) and r.ext_atr > 2.0:
                    score -= (r.ext_atr - 2.0) * 10
                if "同向" in r.stage:
                    score += 8
                score -= r.risk_count * 3
                r.score = round(max(min(score, 100), 0), 1)
            elif r.action.startswith("价格已触发"):
                r.score = 60 + (3 - r.trigger_age) * 5
            elif r.action.startswith("等待内部结构"):
                r.score = 35
            elif r.action.startswith("等待多头动能"):
                r.score = 30
            elif r.action.startswith("跳空") or r.action.startswith("位置过远"):
                r.score = 20
            elif r.action.startswith("等待支撑"):
                r.score = 25
            else:
                r.score = 0

            if short_action.startswith("空头条件齐备"):
                s = 70
                s += {"Shift翻空": 15, "压力区反应": 12, "支撑区跌破": 12, "内部结构转空": 10}.get(short_trigger_out, 8)
                s += min(max(r.adx - 20, 0), 15) if not np.isnan(r.adx) else 0
                if not np.isnan(r.atr_pct) and r.atr_pct > 6:
                    s -= 10
                s -= r.risk_count * 3
                short_score = round(max(min(s, 100), 0), 1)
            elif short_action.startswith("价格已触发"):
                short_score = 60 + (3 - short_trigger_age) * 5
            elif short_action.startswith("等待内部结构"):
                short_score = 35
            elif short_action.startswith("等待空头动能"):
                short_score = 30
            elif short_action.startswith("跳空"):
                short_score = 20
            elif short_action.startswith("等待压力"):
                short_score = 25
            else:
                short_score = 0

            r.short_action = short_action
            r.short_trigger = short_trigger_out
            r.short_trigger_age = short_trigger_age
            r.short_stage = short_stage
            r.short_momentum = short_momentum
            r.short_score = short_score
            r.short_protection = short_protection


    return r


def pivots_symmetric(series: pd.Series, left: int, right: int):
    """ta.pivothigh(len,len) 等价：对称 pivot，确认柱标注摆动柱值。"""
    out = pd.Series(np.nan, index=series.index)
    vals = series.values
    for i in range(left, len(series) - right):
        window = vals[i - left : i + right + 1]
        if vals[i] == window.max() and (window == vals[i]).sum() == 1:
            out.iloc[i + right] = vals[i]
    return out


def stage_text(shift_dir: int, internal_b: int, adx_v: float, short: bool = False) -> str:
    trend_txt = "趋势强" if not np.isnan(adx_v) and adx_v >= 25 else "趋势建立中" if not np.isnan(adx_v) and adx_v >= 18 else "方向过渡"
    adx_txt = f"ADX {int(adx_v)}" if not np.isnan(adx_v) else "ADX na"
    want = BEARISH if short else BULLISH
    aligned = "同向" if internal_b == want else "逆向"
    return f"{trend_txt} {adx_txt}｜波段/内部结构{aligned}"


def momentum_text(known: bool, improving: bool, osc_rdy: bool) -> str:
    if not osc_rdy:
        return "OSC初始化中"
    if not known:
        return "动能未知"
    return "动能增强" if improving else "动能减弱"


# ---------------------------------------------------------------------------
# 批量扫描入口
# ---------------------------------------------------------------------------

def run_phantom_scan(price_data: dict) -> dict:
    """price_data: {ticker: df(OHLCV)} → READY/触发/准就绪 + 做空四层 + 多空 Top10。"""
    results = []
    for t, df in price_data.items():
        try:
            df2 = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
            if len(df2) < 120:
                continue
            avg_dollar_vol = float((df2["close"] * df2["volume"]).tail(20).mean())
            if avg_dollar_vol < 10_000_000:
                continue
            r = scan_one(df2, t)
            if r.data_ok:
                rd = r.__dict__
                rd["avg_dollar_volume"] = avg_dollar_vol
                results.append(rd)
        except Exception:
            continue
    if not results:
        return {"available": False, "status": "无有效扫描结果", "ready": [], "triggered": [], "almost": [],
                "short_ready": [], "short_triggered": [], "short_almost": [], "short_candidates": []}
    df_out = pd.DataFrame(results)
    ready = df_out[df_out["action"].str.startswith("多头条件齐备", na=False)].sort_values("score", ascending=False)
    triggered = df_out[df_out["action"].str.startswith("价格已触发", na=False)].sort_values("score", ascending=False)
    almost = df_out[
        (df_out["stage"].str.contains("同向|已转向", na=False))
        & (df_out["momentum"].str.contains("增强", na=False))
        & (~df_out["action"].str.startswith(("多头条件齐备", "价格已触发"), na=False))
    ].sort_values("adx", ascending=False)
    short_ready = df_out[df_out["short_action"].str.startswith("空头条件齐备", na=False)].sort_values("short_score", ascending=False)
    short_triggered = df_out[df_out["short_action"].str.startswith("价格已触发", na=False)].sort_values("short_score", ascending=False)
    short_almost = df_out[
        (df_out["short_stage"].str.contains("同向|已转向", na=False))
        & (df_out["short_momentum"].str.contains("增强", na=False))
        & (~df_out["short_action"].str.startswith(("空头条件齐备", "价格已触发"), na=False))
    ].sort_values("adx", ascending=False)
    short_candidates = pd.concat([short_ready.assign(_rank=0), short_triggered.assign(_rank=1), short_almost.assign(_rank=2)])
    if len(short_candidates) < 5:
        rest = df_out[
            (df_out["short_action"] != "")
            & (~df_out["ticker"].isin(short_candidates["ticker"]))
        ].sort_values(["short_score", "adx"], ascending=[False, False])
        short_candidates = pd.concat([short_candidates, rest.assign(_rank=3)])
    short_candidates = short_candidates.sort_values(["_rank", "short_score", "adx"], ascending=[True, False, False]).head(10)
    long_candidates = pd.concat([ready.assign(_rank=0), triggered.assign(_rank=1), almost.assign(_rank=2)])
    if len(long_candidates) < 5:
        rest = df_out[
            (df_out["action"] != "")
            & (df_out["action"] != "空头趋势，等待做多转折")
            & (~df_out["ticker"].isin(long_candidates["ticker"]))
        ].sort_values(["score", "adx"], ascending=[False, False])
        long_candidates = pd.concat([long_candidates, rest.assign(_rank=3)])
    long_candidates = long_candidates.sort_values(["_rank", "score", "adx"], ascending=[True, False, False]).head(10)

    def pack(sub, limit, short=False):
        out = []
        for _, row in sub.head(limit).iterrows():
            base = {
                "ticker": row["ticker"], "action": row["action"], "trigger": row.get("trigger", ""),
                "trigger_age": int(row["trigger_age"]) if pd.notna(row.get("trigger_age")) else 0,
                "close": round(float(row["close"]), 2), "score": round(float(row["score"]), 1),
                "adx": round(float(row["adx"]), 1) if pd.notna(row.get("adx")) else None,
                "osc": round(float(row["osc"]), 3) if pd.notna(row.get("osc")) else None,
                "osc_ready": bool(row.get("osc_ready", False)),
                "atr_pct": round(float(row["atr_pct"]), 2) if pd.notna(row.get("atr_pct")) else None,
                "ext_atr": round(float(row["ext_atr"]), 2) if pd.notna(row.get("ext_atr")) else None,
                "stop": round(float(row["stop"]), 2) if pd.notna(row.get("stop")) else None,
                "buy_zone_low": round(float(row["buy_zone_low"]), 2) if pd.notna(row.get("buy_zone_low")) else None,
                "buy_zone_high": round(float(row["buy_zone_high"]), 2) if pd.notna(row.get("buy_zone_high")) else None,
                "stage": row.get("stage", ""), "momentum": row.get("momentum", ""),
                "protection": round(float(row["protection"]), 2) if pd.notna(row.get("protection")) else None,
                "power_resistance_top": round(float(row["power_resistance_top"]), 2) if pd.notna(row.get("power_resistance_top")) else None,
                "avg_dollar_volume": round(float(row["avg_dollar_volume"]), 0) if pd.notna(row.get("avg_dollar_volume")) else None,
                "risk_count": int(row.get("risk_count", 0)),
                "risk_flags": row.get("risk_flags", ""),
                "dash_score": int(row.get("dash_score", 0)),
                "dash_action": row.get("dash_action", ""),
            }
            if short:
                base.update({
                    "short_action": row.get("short_action", ""), "short_trigger": row.get("short_trigger", ""),
                    "short_trigger_age": int(row["short_trigger_age"]) if pd.notna(row.get("short_trigger_age")) else 0,
                    "short_score": round(float(row["short_score"]), 1) if pd.notna(row.get("short_score")) else None,
                    "short_stage": row.get("short_stage", ""), "short_momentum": row.get("short_momentum", ""),
                    "short_protection": round(float(row["short_protection"]), 2) if pd.notna(row.get("short_protection")) else None,
                })
            out.append(base)
        return out

    return {
        "available": True, "status": "ok",
        "universe_size": len(results),
        "ready": pack(ready, 10),
        "triggered": pack(triggered, 10),
        "almost": pack(almost, 15),
        "short_ready": pack(short_ready, 10, short=True),
        "short_triggered": pack(short_triggered, 10, short=True),
        "short_almost": pack(short_almost, 15, short=True),
        "short_candidates": pack(short_candidates, 10, short=True),
        "long_candidates": pack(long_candidates, 10),
    }
