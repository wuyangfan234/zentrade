"""Phantom 4h · Pine 原版仪表盘 (Phantom Flow Core 中文优化版, Pine v6) —— 逐根、无前视。

与 `phantom_4h` (vendor 简化扫描, “多头条件齐备”) 的区别 (2026-10-09 逐根对照 10 只真实 4h 序列):
  - Pine 本身没有“多头条件齐备”: 它的交易判断是仪表盘“现在该交易吗”五盏灯 (第 1134-1144 行),
    >=4 盏 = “积极做多”。MACD/RSI/ADX 打分是简化扫描后加的, 不在 Pine 里。
  - Pine 的结构 (SMC leg/getCurrentStructure/displayStructure) 与 vendored `leg_pivots/structure_bias` 只有 64% 一致:
    vendored 把摆动高/低点标反、每根都记 pivot 而非只在 leg 翻转时记、且没有 Pine 默认开启的共振过滤。
  - Shift 与 Trail 与 Pine 分别 100% / 99.2% 一致。

本模块 = Pine 原文转写, 用 PyneCore (Pine 的 Python 运行时) 逐根对照 (tests/tv_reference/phantom_4h_pine.py)。
  L1 shiftTrend == 1                      (SECTION 1, 第 16-43 行)
  L2 swingTrend.bias == BULLISH           (SMC 波段结构 swingsLength=34, 第 358-605 行)
  L3 osc > 0                              (SECTION 3, osc = hma(change(oscDiff/percentile(oscDiff,1000,99),21),10))
  L4 close > maBase                       (maBase = EMA(hl2, 50))
  L5 日线 EMA20[1] > EMA50[1]             (request.security('D', …[1], lookahead_off): 上一根已收盘日线)
  进场事件 = 仪表盘分数由 <4 变为 >=4 (“积极做多”出现的那一根); 保护位 = Phantom Trail 多头轨道 (SECTION 4)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_BARS = 1060          # osc 需要 1000 根分位窗 + 21 根变化 + HMA(10) 预热; 不足则 L3 恒为假 (Pine 同样是 na)
NAN = np.nan


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma: 以前 n 个有效值的 SMA 起步, 之后 alpha=1/n。"""
    out = np.full(x.shape[0], NAN)
    acc, cnt, prev = 0.0, 0, NAN
    for i, v in enumerate(x):
        if not np.isfinite(v):
            continue
        if not np.isfinite(prev):
            acc += v
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = (v + (n - 1) * prev) / n
        out[i] = prev
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.ema: 以前 n 个有效值的 SMA 起步, alpha=2/(n+1)。"""
    out = np.full(x.shape[0], NAN)
    a = 2.0 / (n + 1)
    acc, cnt, prev = 0.0, 0, NAN
    for i, v in enumerate(x):
        if not np.isfinite(v):
            continue
        if not np.isfinite(prev):
            acc += v
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = a * v + (1 - a) * prev
        out[i] = prev
    return out


def _wma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape[0], NAN)
    w = np.arange(1, n + 1, dtype=float)
    s = w.sum()
    for i in range(n - 1, x.shape[0]):
        seg = x[i - n + 1:i + 1]
        if np.all(np.isfinite(seg)):
            out[i] = float(np.dot(seg, w) / s)
    return out


def _atr(h, lo, c, n=14) -> np.ndarray:
    pc = np.concatenate([[NAN], c[:-1]])
    tr = np.where(np.isfinite(pc), np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc))), h - lo)
    return _rma(tr, n)


def _percentile_linear(x: np.ndarray, length: int, pct: float) -> np.ndarray:
    """Pine ta.percentile_linear_interpolation: 1 起的插值位置 pos = n·p/100 + 0.5; 窗内有 na 时给 na。
    向量化: 滑动窗 + np.partition 取第 k-1/k 个顺序统计量 (与整窗排序取值完全相同)。"""
    n = x.shape[0]
    out = np.full(n, NAN)
    if n < length:
        return out
    pos = length * pct / 100.0 + 0.5
    if abs(pos - round(pos)) < 1e-9:
        pos = float(round(pos))
    win = np.lib.stride_tricks.sliding_window_view(x, length)          # (n-length+1, length), 只读视图
    bad = ~np.isfinite(win).all(axis=1)
    if pos <= 1:
        val = np.nanmin(np.where(np.isfinite(win), win, np.inf), axis=1)
    elif pos >= length:
        val = np.nanmax(np.where(np.isfinite(win), win, -np.inf), axis=1)
    else:
        k = int(np.floor(pos))
        f = pos - k
        part = np.partition(np.where(np.isfinite(win), win, np.inf), (k - 1, k), axis=1)
        with np.errstate(invalid="ignore"):                         # 含 na 的窗下面统一置 na
            val = part[:, k - 1] + f * (part[:, k] - part[:, k - 1])
    val[bad] = NAN
    out[length - 1:] = val
    return out


def shift_trend(h, lo, c, period=14, mult=3.2) -> np.ndarray:
    """SECTION 1 Phantom Shift (hl2 ± 3.2·ATR14 棘轮, shiftTrend 与前一根轨道比较)。"""
    n = c.shape[0]
    src = (h + lo) / 2
    a = _atr(h, lo, c, period)
    up_raw, dn_raw = src - mult * a, src + mult * a
    up = np.full(n, NAN)
    dn = np.full(n, NAN)
    tr = np.ones(n, dtype=int)
    t = 1
    for i in range(n):
        upp = up[i - 1] if i > 0 and np.isfinite(up[i - 1]) else up_raw[i]
        dnp = dn[i - 1] if i > 0 and np.isfinite(dn[i - 1]) else dn_raw[i]
        pc = c[i - 1] if i > 0 else NAN
        up[i] = max(up_raw[i], upp) if (np.isfinite(pc) and np.isfinite(upp) and pc > upp and np.isfinite(up_raw[i])) else up_raw[i]
        dn[i] = min(dn_raw[i], dnp) if (np.isfinite(pc) and np.isfinite(dnp) and pc < dnp and np.isfinite(dn_raw[i])) else dn_raw[i]
        if t == -1 and np.isfinite(dnp) and c[i] > dnp:
            t = 1
        elif t == 1 and np.isfinite(upp) and c[i] < upp:
            t = -1
        tr[i] = t
    return tr


def trail(h, lo, c, length=22, mult=3.0):
    """SECTION 4 Phantom Trail: highest(22)-3·ATR14 / lowest(22)+3·ATR14, close[1] 棘轮, 与当根轨道比较翻转。"""
    n = c.shape[0]
    a = _atr(h, lo, c, 14)
    hh = pd.Series(h).rolling(length).max().to_numpy()
    ll = pd.Series(lo).rolling(length).min().to_numpy()
    lr, sr = hh - mult * a, ll + mult * a
    ls = np.full(n, NAN)
    ss = np.full(n, NAN)
    d = np.ones(n, dtype=int)
    t = 1
    for i in range(n):
        lp = ls[i - 1] if i > 0 and np.isfinite(ls[i - 1]) else lr[i]
        sp = ss[i - 1] if i > 0 and np.isfinite(ss[i - 1]) else sr[i]
        pc = c[i - 1] if i > 0 else NAN
        ls[i] = max(lr[i], lp) if (np.isfinite(pc) and np.isfinite(lp) and pc > lp and np.isfinite(lr[i])) else lr[i]
        ss[i] = min(sr[i], sp) if (np.isfinite(pc) and np.isfinite(sp) and pc < sp and np.isfinite(sr[i])) else sr[i]
        if t == 1 and np.isfinite(ls[i]) and c[i] < ls[i]:
            t = -1
        elif t == -1 and np.isfinite(ss[i]) and c[i] > ss[i]:
            t = 1
        d[i] = t
    return d, ls, ss


def swing_bias(h, lo, c, size=34) -> np.ndarray:
    """SMC 波段结构: leg(size) 翻转时记摆动点 (0→1 记低点 low[size], 1→0 记高点 high[size]);
    收盘上穿未被穿越的摆动高点 → bias=多; 下穿未被穿越的摆动低点 → bias=空。"""
    n = c.shape[0]
    leg, prev = 0, None
    sw_hi = sw_lo = NAN
    hx = lx = False
    bias = 0
    out = np.zeros(n, dtype=int)
    for j in range(n):
        if j >= size:
            if h[j - size] > h[j - size + 1:j + 1].max():
                leg = 0
            elif lo[j - size] < lo[j - size + 1:j + 1].min():
                leg = 1
        if prev is not None and leg - prev == 1:
            sw_lo, lx = lo[j - size], False
        elif prev is not None and leg - prev == -1:
            sw_hi, hx = h[j - size], False
        prev = leg
        if j > 0 and np.isfinite(sw_hi) and c[j] > sw_hi and c[j - 1] <= sw_hi and not hx:
            hx, bias = True, 1
        if j > 0 and np.isfinite(sw_lo) and c[j] < sw_lo and c[j - 1] >= sw_lo and not lx:
            lx, bias = True, -1
        out[j] = bias
    return out


def oscillator(h, lo, c):
    src = (h + lo) / 2
    ma = _ema(src, 50)
    diff = src - ma
    rng = _percentile_linear(diff, 1000, 99)
    ratio = diff / rng
    ch = np.full(c.shape[0], NAN)
    ch[21:] = ratio[21:] - ratio[:-21]
    osc = _wma(2 * _wma(ch, 5) - _wma(ch, 10), 3)
    return osc, ma


def daily_trend_ok(daily: pd.DataFrame) -> pd.Series:
    """L5: 日线 EMA20 > EMA50 (日K close), 按日期索引; 调用方按“上一根已收盘日线”对齐到 4h。"""
    c = daily["close"].to_numpy(dtype=float)
    return pd.Series(_ema(c, 20) > _ema(c, 50), index=pd.Index(daily.index))


def dashboard_series(df: pd.DataFrame, daily_ok: pd.Series | None = None) -> pd.DataFrame:
    """df: 4h OHLCV (index = 美东墙钟 naive, 升序)。daily_ok: 日期 → 当日收盘后日线 EMA20>EMA50 (bool)。
    返回逐根: l1..l5, score, enter (分数由 <4 变 >=4), long_trail, short_trail, trail_dir, close。"""
    df = df.dropna(subset=["open", "high", "low", "close"])
    h, lo, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    st = shift_trend(h, lo, c)
    sb = swing_bias(h, lo, c, 34)
    osc, ma = oscillator(h, lo, c)
    d, ls, ss = trail(h, lo, c)
    l1, l2 = st == 1, sb == 1
    l3 = np.nan_to_num(osc, nan=0.0) > 0                 # na → 假 (Pine: na > 0 为 false)
    l4 = np.where(np.isfinite(ma), c > ma, False)
    if daily_ok is not None and len(daily_ok):
        # request.security('D', ema[1], lookahead_off): 4h 根所在日 D 的“上一根已收盘日线” = 严格早于 D 的最近一个交易日
        ok = daily_ok.sort_index()
        d_days = pd.to_datetime(pd.Index(ok.index)).values.astype("datetime64[D]")
        bar_days = np.array(df.index.date, dtype="datetime64[D]")
        idx = np.searchsorted(d_days, bar_days, side="left") - 1
        vals = ok.to_numpy(dtype=bool)
        l5 = np.where(idx >= 0, vals[np.clip(idx, 0, None)], False)
    else:
        l5 = np.zeros(len(df), dtype=bool)
    score = l1.astype(int) + l2 + l3 + l4 + l5
    prev_score = np.concatenate([[0], score[:-1]])
    out = pd.DataFrame({"close": c, "l1": l1, "l2": l2, "l3": l3, "l4": l4, "l5": l5, "score": score,
                        "enter": (score >= 4) & (prev_score < 4), "long_trail": ls, "short_trail": ss,
                        "trail_dir": d, "shift_trend": st, "swing_bias": sb, "osc": osc}, index=df.index)
    out.loc[out.index[:min(MIN_BARS, len(out))], "enter"] = False   # osc 未预热完的根不出进场事件
    return out
