"""TradingView 开源指标的逐根内核 (2026-10-09)。

每个函数逐行对应一份 Pine 源码 (出处见各函数 docstring), 在“每只股票自己的有效日K序列”上逐根推进 ——
与 TradingView 图表一致: 停牌/缺失日没有 K 线, 不参与任何窗口或递推。Pine 语义按 PyneCore
(对拍 TradingView 的开源 Pine 运行时) 实测规则复刻:
  - ta.rma / ta.ema: 先用前 length 个非 na 值的 SMA 作种子, na 输入 → na 输出且状态不前进;
  - ta.sma / ta.stdev / ta.linreg / ta.highest / ta.lowest: 按 K 线计数的窗口, 窗口内有 na → na;
  - ta.tr(true) 首根 = high - low; ta.tr (变量) 首根 = na;
  - 与 na 比较一律 false; math.max/min 遇 na → na;
  - ta.crossover(a, b): a > b 且“上一根 a、b 都有值时” a <= b (na 根不更新状态)。
校验: tests/test_tv_strategies.py 用 PyneCore 逐行转写的参照脚本逐根比对 (见 tests/tv_reference/)。
"""
from __future__ import annotations

import math

import numpy as np

from app.backtest.matrix import _build_valid_bar_index, njit, prange
from app.backtest.numba_runtime import run_numba_parallel

NAN = np.nan


# ── Pine 基础函数 (一维, float64) ─────────────────────────────

@njit(cache=True, nogil=True)
def _rma(x, n):
    out = np.full(x.shape[0], NAN)
    if n == 1:
        return x.copy()
    s, cnt, last, seeded = 0.0, 0, NAN, False
    for j in range(x.shape[0]):
        v = x[j]
        if not np.isfinite(v):
            continue
        if not seeded:
            s += v
            cnt += 1
            if cnt == n:
                last = s / n
                seeded = True
                out[j] = last
        else:
            last = (last * (n - 1) + v) / n
            out[j] = last
    return out


@njit(cache=True, nogil=True)
def _ema(x, n):
    out = np.full(x.shape[0], NAN)
    if n == 1:
        return x.copy()
    alpha = 2.0 / (n + 1)
    s, cnt, last, seeded = 0.0, 0, NAN, False
    for j in range(x.shape[0]):
        v = x[j]
        if not np.isfinite(v):
            continue
        if not seeded:
            s += v
            cnt += 1
            if cnt == n:
                last = s / n
                seeded = True
                out[j] = last
        else:
            last = last + alpha * (v - last)
            out[j] = last
    return out


@njit(cache=True, nogil=True)
def _sma(x, n):
    out = np.full(x.shape[0], NAN)
    for j in range(n - 1, x.shape[0]):
        s = 0.0
        ok = True
        for k in range(j - n + 1, j + 1):
            v = x[k]
            if not np.isfinite(v):
                ok = False
                break
            s += v
        if ok:
            out[j] = s / n
    return out


@njit(cache=True, nogil=True)
def _stdev(x, n):
    out = np.full(x.shape[0], NAN)
    for j in range(n - 1, x.shape[0]):
        p = 0.0
        q = 0.0
        ok = True
        for k in range(j - n + 1, j + 1):
            v = x[k]
            if not np.isfinite(v):
                ok = False
                break
            p += v
            q += v * v
        if ok:
            m = p / n
            out[j] = math.sqrt(max(0.0, q / n - m * m))
    return out


@njit(cache=True, nogil=True)
def _linreg(x, n, offset):
    out = np.full(x.shape[0], NAN)
    sx = 0.0
    sx2 = 0.0
    for i in range(1, n + 1):
        sx += float(i)
        sx2 += float(i) * float(i)
    denom = n * sx2 - sx * sx
    for j in range(n - 1, x.shape[0]):
        sy = 0.0
        sxy = 0.0
        per = 0.0
        ok = True
        for k in range(j - n + 1, j + 1):
            y = x[k]
            if not np.isfinite(y):
                ok = False
                break
            per += 1.0
            sy += y
            sxy += y * per
        if not ok:
            continue
        slope = (n * sxy - sx * sy) / denom
        average = sy / n
        intercept = average - slope * sx / n + slope
        out[j] = intercept + slope * (n - 1 - offset)
    return out


@njit(cache=True, nogil=True)
def _highest(x, n):
    out = np.full(x.shape[0], NAN)
    for j in range(n - 1, x.shape[0]):
        m = x[j]
        for k in range(j - n + 1, j):
            if x[k] > m:
                m = x[k]
        out[j] = m
    return out


@njit(cache=True, nogil=True)
def _lowest(x, n):
    out = np.full(x.shape[0], NAN)
    for j in range(n - 1, x.shape[0]):
        m = x[j]
        for k in range(j - n + 1, j):
            if x[k] < m:
                m = x[k]
        out[j] = m
    return out


@njit(cache=True, nogil=True)
def _tr(h, lo, c, handle_na):
    n = c.shape[0]
    out = np.full(n, NAN)
    for j in range(n):
        if j == 0:
            if handle_na:
                out[j] = h[j] - lo[j]
        else:
            pc = c[j - 1]
            out[j] = max(h[j] - lo[j], max(abs(h[j] - pc), abs(lo[j] - pc)))
    return out


@njit(cache=True, nogil=True)
def _crossover(a, b):
    """ta.crossover(a, b): 逐根状态机 (na 根返回 false 且不更新)。"""
    n = a.shape[0]
    out = np.zeros(n, dtype=np.bool_)
    was_le = False
    for j in range(n):
        if not (np.isfinite(a[j]) and np.isfinite(b[j])):
            continue
        out[j] = a[j] > b[j] and was_le
        was_le = a[j] <= b[j]
    return out


@njit(cache=True, nogil=True)
def _crossunder(a, b):
    n = a.shape[0]
    out = np.zeros(n, dtype=np.bool_)
    was_ge = False
    for j in range(n):
        if not (np.isfinite(a[j]) and np.isfinite(b[j])):
            continue
        out[j] = a[j] < b[j] and was_ge
        was_ge = a[j] >= b[j]
    return out


# ── 各脚本 (输入 = 单只股票的有效 K 线, 输出 entry/exit 布尔序列) ─────────────

@njit(cache=True, nogil=True)
def supertrend_1d(h, lo, c, period, mult):
    """Supertrend (KivancOzbilgic v3, Pine v4) — 同一算法也是 Phantom Flow SECTION 1 “Phantom Shift”。
    up=src-m·atr; up1=nz(up[1],up); up:=close[1]>up1?max(up,up1):up (dn 镜像);
    trend:=trend==-1 and close>dn1?1:trend==1 and close<up1?-1:trend; buy=trend==1 and trend[1]==-1。"""
    n = c.shape[0]
    atr = _rma(_tr(h, lo, c, True), period)
    up = np.full(n, NAN)
    dn = np.full(n, NAN)
    trend = np.ones(n, dtype=np.int64)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        src = (h[j] + lo[j]) / 2.0
        ur = src - mult * atr[j]
        dr = src + mult * atr[j]
        up1 = up[j - 1] if (j > 0 and np.isfinite(up[j - 1])) else ur
        dn1 = dn[j - 1] if (j > 0 and np.isfinite(dn[j - 1])) else dr
        pc = c[j - 1] if j > 0 else NAN
        if np.isfinite(pc) and np.isfinite(up1) and pc > up1:
            up[j] = max(ur, up1) if np.isfinite(ur) else NAN
        else:
            up[j] = ur
        if np.isfinite(pc) and np.isfinite(dn1) and pc < dn1:
            dn[j] = min(dr, dn1) if np.isfinite(dr) else NAN
        else:
            dn[j] = dr
        t = trend[j - 1] if j > 0 else 1
        if t == -1 and np.isfinite(dn1) and c[j] > dn1:
            t = 1
        elif t == 1 and np.isfinite(up1) and c[j] < up1:
            t = -1
        trend[j] = t
        if j > 0:
            buy[j] = t == 1 and trend[j - 1] == -1
            sell[j] = t == -1 and trend[j - 1] == 1
    return buy, sell


@njit(cache=True, nogil=True)
def ut_bot_1d(c, key, period, h, lo):
    """UT Bot Alerts (QuantNomad, Pine v4)。src=close; nLoss=a·atr(c);
    stop:=iff(src>nz(stop[1],0) and src[1]>nz(stop[1],0), max(nz(stop[1]),src-nLoss), iff(src<… and src[1]<…,
    min(nz(stop[1]),src+nLoss), iff(src>nz(stop[1],0), src-nLoss, src+nLoss)));
    buy = src>stop and crossover(src, stop); sell = src<stop and crossover(stop, src)。"""
    n = c.shape[0]
    atr = _rma(_tr(h, lo, c, True), period)
    stop = np.full(n, NAN)
    for j in range(n):
        nloss = key * atr[j]
        prev = stop[j - 1] if j > 0 else NAN
        p0 = prev if np.isfinite(prev) else 0.0
        src = c[j]
        src1 = c[j - 1] if j > 0 else NAN
        if src > p0 and np.isfinite(src1) and src1 > p0:
            stop[j] = max(p0, src - nloss) if np.isfinite(nloss) else NAN
        elif src < p0 and np.isfinite(src1) and src1 < p0:
            stop[j] = min(p0, src + nloss) if np.isfinite(nloss) else NAN
        elif src > p0:
            stop[j] = src - nloss
        else:
            stop[j] = src + nloss
    above = _crossover(c, stop)
    below = _crossover(stop, c)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        if np.isfinite(stop[j]):
            buy[j] = c[j] > stop[j] and above[j]
            sell[j] = c[j] < stop[j] and below[j]
    return buy, sell


@njit(cache=True, nogil=True)
def chandelier_exit_1d(h, lo, c, v, length, mult, use_close, vol_filter, vol_mult, trend_filter, ema_len,
                       atr_filter, atr_sma_len):
    """Chandelier Exit (everget v8, Pine v6) + “CE Ultimate Pro (TR)” v2.2 的可选过滤 (默认全关 = 与 everget 逐根相同)。
    longStop=highest(close,22)-3·atr(22); longStop:=close[1]>longStopPrev?max(longStop,longStopPrev):longStop;
    dir:=close>shortStopPrev?1:close<longStopPrev?-1:dir; buy=dir==1 and dir[1]==-1。
    过滤 (Ultimate): 量>SMA(量,20)·k / close>EMA(close,200) / ATR>SMA(ATR,20)。"""
    n = c.shape[0]
    atr_raw = _rma(_tr(h, lo, c, True), length)
    hh = _highest(c if use_close else h, length)
    ll = _lowest(c if use_close else lo, length)
    ls = np.full(n, NAN)
    ss = np.full(n, NAN)
    d = np.ones(n, dtype=np.int64)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        a = mult * atr_raw[j]
        lr = hh[j] - a
        sr = ll[j] + a
        lp = ls[j - 1] if (j > 0 and np.isfinite(ls[j - 1])) else lr
        sp = ss[j - 1] if (j > 0 and np.isfinite(ss[j - 1])) else sr
        pc = c[j - 1] if j > 0 else NAN
        if np.isfinite(pc) and np.isfinite(lp) and pc > lp:
            ls[j] = max(lr, lp) if np.isfinite(lr) else NAN
        else:
            ls[j] = lr
        if np.isfinite(pc) and np.isfinite(sp) and pc < sp:
            ss[j] = min(sr, sp) if np.isfinite(sr) else NAN
        else:
            ss[j] = sr
        t = d[j - 1] if j > 0 else 1
        if np.isfinite(sp) and c[j] > sp:
            t = 1
        elif np.isfinite(lp) and c[j] < lp:
            t = -1
        d[j] = t
        if j > 0:
            buy[j] = t == 1 and d[j - 1] == -1
            sell[j] = t == -1 and d[j - 1] == 1
    if vol_filter or trend_filter or atr_filter:
        vs = _sma(v, 20)
        em = _ema(c, ema_len)
        asma = _sma(atr_raw, atr_sma_len)
        for j in range(n):
            ok = True
            if vol_filter:
                ok = ok and np.isfinite(vs[j]) and v[j] > vs[j] * vol_mult
            if atr_filter:
                ok = ok and np.isfinite(asma[j]) and atr_raw[j] > asma[j]
            okl = ok and ((not trend_filter) or (np.isfinite(em[j]) and c[j] > em[j]))
            oks = ok and ((not trend_filter) or (np.isfinite(em[j]) and c[j] < em[j]))
            buy[j] = buy[j] and okl
            sell[j] = sell[j] and oks
    return buy, sell


@njit(cache=True, nogil=True)
def zlsma_1d(c, length, offset):
    """ZLSMA (veryfid, Pine v4): lsma=linreg(src,len,off); zlsma=lsma+(lsma-linreg(lsma,len,off))。
    源码只画线, 信号取作者说明“价格上穿=多头趋势, 下穿=空头趋势”: buy=crossover(close,zlsma)。"""
    lsma = _linreg(c, length, offset)
    lsma2 = _linreg(lsma, length, offset)
    z = lsma + (lsma - lsma2)
    return _crossover(c, z), _crossunder(c, z)


@njit(cache=True, nogil=True)
def halftrend_1d(h, lo, c, amplitude, channel_dev, atr_gate):
    """HalfTrend (everget v3, Pine v6) 逐行状态机; buy = not na(arrowUp) and trend==0 and trend[1]==1。
    arrowUp = up - atr(100)/2: 图表前 99 根 ATR 为 na → 无箭头。atr_gate=False 时不做这一判断 —— 系统股票池要求
    >=130 根历史, 对池内股票该判断恒成立, 而生产只加载约 75 根窗口 (ATR(100) 在窗口内算不出); 趋势状态机本身不用 ATR。"""
    n = c.shape[0]
    atr2 = _rma(_tr(h, lo, c, True), 100) / 2.0
    highma = _sma(h, amplitude)
    lowma = _sma(lo, amplitude)
    trend = np.zeros(n, dtype=np.int64)
    up = np.full(n, NAN)
    down = np.full(n, NAN)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    t = 0
    nxt = 0
    max_low = lo[0] if n > 0 else NAN
    min_high = h[0] if n > 0 else NAN
    up_v = 0.0
    down_v = 0.0
    for j in range(n):
        # highPrice = high[abs(highestbars(amplitude))]  (首根窗口不足 → 取当根)
        k0 = max(0, j - amplitude + 1)
        hi_i = j
        lo_i = j
        for k in range(j - 1, k0 - 1, -1):
            if h[k] > h[hi_i]:
                hi_i = k
            if lo[k] < lo[lo_i]:
                lo_i = k
        high_price = h[hi_i]
        low_price = lo[lo_i]
        prev_low = lo[j - 1] if j > 0 else lo[j]
        prev_high = h[j - 1] if j > 0 else h[j]
        if nxt == 1:
            max_low = max(low_price, max_low)
            if np.isfinite(highma[j]) and highma[j] < max_low and c[j] < prev_low:
                t = 1
                nxt = 0
                min_high = high_price
        else:
            min_high = min(high_price, min_high)
            if np.isfinite(lowma[j]) and lowma[j] > min_high and c[j] > prev_high:
                t = 0
                nxt = 1
                max_low = low_price
        arrow_up = NAN
        arrow_down = NAN
        tp = trend[j - 1] if j > 0 else -1     # trend[1] 首根 = na
        if t == 0:
            if j > 0 and tp != 0:
                up_v = down[j - 1] if np.isfinite(down[j - 1]) else down_v
                arrow_up = up_v - atr2[j]
            else:
                up_v = max_low if (j == 0 or not np.isfinite(up[j - 1])) else max(max_low, up[j - 1])
        else:
            if j > 0 and tp != 1:
                down_v = up[j - 1] if np.isfinite(up[j - 1]) else up_v
                arrow_down = down_v + atr2[j]
            else:
                down_v = min_high if (j == 0 or not np.isfinite(down[j - 1])) else min(min_high, down[j - 1])
        trend[j] = t
        up[j] = up_v
        down[j] = down_v
        if j > 0:
            okb = np.isfinite(arrow_up) if atr_gate else (j > 0 and tp != 0 and t == 0)
            oks = np.isfinite(arrow_down) if atr_gate else (j > 0 and tp != 1 and t == 1)
            buy[j] = okb and t == 0 and trend[j - 1] == 1
            sell[j] = oks and t == 1 and trend[j - 1] == 0
    return buy, sell


@njit(cache=True, nogil=True)
def squeeze_momentum_1d(h, lo, c, length, mult, length_kc, mult_kc, use_tr):
    """Squeeze Momentum (LazyBear, 作者 v2 修正版)。指标本身不出买卖点; 信号按作者说明里 Carter 的用法:
    挤压 (黑十字 sqzOn) 之后第一个释放 (灰十字 sqzOff) 且动能 val>0 → 买; 持有中 val 由升转降 (颜色变) → 卖。"""
    n = c.shape[0]
    basis = _sma(c, length)
    dev = mult * _stdev(c, length)
    ma = _sma(c, length_kc)
    rng = _tr(h, lo, c, False) if use_tr else (h - lo)
    rangema = _sma(rng, length_kc)
    hh = _highest(h, length_kc)
    ll = _lowest(lo, length_kc)
    smac = _sma(c, length_kc)
    src = np.full(n, NAN)
    for j in range(n):
        src[j] = c[j] - ((hh[j] + ll[j]) / 2.0 + smac[j]) / 2.0
    val = _linreg(src, length_kc, 0)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    armed = False
    for j in range(n):
        ub = basis[j] + dev[j]
        lb = basis[j] - dev[j]
        uk = ma[j] + rangema[j] * mult_kc
        lk = ma[j] - rangema[j] * mult_kc
        sqz_on = (lb > lk) and (ub < uk)
        sqz_off = (lb < lk) and (ub > uk)
        if sqz_on:
            armed = True
        if sqz_off and armed:
            if np.isfinite(val[j]) and val[j] > 0:
                buy[j] = True
            armed = False
        if j > 0:
            pv = val[j - 1] if np.isfinite(val[j - 1]) else 0.0     # nz(val[1])
            sell[j] = np.isfinite(val[j]) and val[j] < pv
    return buy, sell


@njit(cache=True, nogil=True)
def wavetrend_1d(h, lo, c, ch_len, avg_len, sig_len, lower_thr, upper_thr):
    """WaveTrend [LazyBear] vX by DGT (Pine v6), 默认 crosses='Strong':
    esa=ema(hlc3,n1); d=ema(|hlc3-esa|,n1); ci=(hlc3-esa)/(0.015·d); wtOsc=ema(ci,n2); wtSig=sma(wtOsc,s1);
    强多 = crossover(wtOsc,wtSig) and wtOsc < avg(-60,-53); 强空 = crossunder and wtOsc > avg(60,53)。"""
    n = c.shape[0]
    s = (h + lo + c) / 3.0
    esa = _ema(s, ch_len)
    ad = np.full(n, NAN)
    for j in range(n):
        if np.isfinite(esa[j]):
            ad[j] = abs(s[j] - esa[j])
    d = _ema(ad, ch_len)
    ci = np.full(n, NAN)
    for j in range(n):
        if np.isfinite(d[j]) and d[j] != 0.0:
            ci[j] = (s[j] - esa[j]) / (0.015 * d[j])
    wt = _ema(ci, avg_len)
    sig = _sma(wt, sig_len)
    up = _crossover(wt, sig)
    dn = _crossunder(wt, sig)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        buy[j] = up[j] and wt[j] < lower_thr
        sell[j] = dn[j] and wt[j] > upper_thr
    return buy, sell


@njit(cache=True, nogil=True)
def smc_swing_1d(h, lo, c, size, choch_only):
    """Smart Money Concepts [LuxAlgo] 波段结构 (swing): leg(size) + getCurrentStructure + displayStructure。
    leg: high[size]>highest(size) → 空腿(0); low[size]<lowest(size) → 多腿(1); 0→1 记摆动低点 low[size], 1→0 记摆动高点 high[size]。
    多头结构: crossover(close, swingHigh) 且该高点未被穿越 → 原趋势为空则 CHoCH, 否则 BOS; 空头镜像。
    返回 (多头结构突破: choch_only 时只取 CHoCH, 否则 BOS|CHoCH; 空头结构突破 BOS|CHoCH)。"""
    n = c.shape[0]
    leg = 0
    prev_leg = -1
    hi_lvl = NAN
    lo_lvl = NAN
    hi_crossed = False
    lo_crossed = False
    bias = 0
    was_le = False
    was_ge = False
    bull = np.zeros(n, dtype=np.bool_)
    bear = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        if j >= size:
            mh = h[j - size + 1]
            ml = lo[j - size + 1]
            for k in range(j - size + 2, j + 1):
                if h[k] > mh:
                    mh = h[k]
                if lo[k] < ml:
                    ml = lo[k]
            if h[j - size] > mh:
                leg = 0
            elif lo[j - size] < ml:
                leg = 1
        if j > 0 and prev_leg >= 0:
            ch = leg - prev_leg
            if ch == 1:
                lo_lvl = lo[j - size]
                lo_crossed = False
            elif ch == -1:
                hi_lvl = h[j - size]
                hi_crossed = False
        prev_leg = leg
        # displayStructure (swing): 多头
        if np.isfinite(hi_lvl):
            xo = c[j] > hi_lvl and was_le
            was_le = c[j] <= hi_lvl
            if xo and not hi_crossed:
                bull[j] = (bias == -1) or not choch_only
                hi_crossed = True
                bias = 1
        if np.isfinite(lo_lvl):
            xu = c[j] < lo_lvl and was_ge
            was_ge = c[j] >= lo_lvl
            if xu and not lo_crossed:
                bear[j] = True
                lo_crossed = True
                bias = -1
    return bull, bear


@njit(cache=True, nogil=True)
def phantom_trail_1d(h, lo, c, length, mult):
    """Phantom Flow Core SECTION 4 “Phantom Trail” (Pine v6 第 935-958 行):
    trlAtr=ta.atr(14); longRaw=highest(high,22)-3·atr; shortRaw=lowest(low,22)+3·atr;
    trlLongStop:=close[1]>nz(trlLongStop[1],longRaw)?max(longRaw,nz(trlLongStop[1],longRaw)):longRaw (空头镜像);
    trlDir:=trlDir==1 and close<trlLongStop?-1:trlDir==-1 and close>trlShortStop?1:trlDir  (与“当根”轨道比较);
    转多 = trlDir==1 and trlDir[1]==-1。"""
    n = c.shape[0]
    atr = _rma(_tr(h, lo, c, True), 14)
    hh = _highest(h, length)
    ll = _lowest(lo, length)
    ls = np.full(n, NAN)
    ss = np.full(n, NAN)
    d = np.ones(n, dtype=np.int64)
    buy = np.zeros(n, dtype=np.bool_)
    sell = np.zeros(n, dtype=np.bool_)
    for j in range(n):
        lr = hh[j] - mult * atr[j]
        sr = ll[j] + mult * atr[j]
        lp = ls[j - 1] if (j > 0 and np.isfinite(ls[j - 1])) else lr
        sp = ss[j - 1] if (j > 0 and np.isfinite(ss[j - 1])) else sr
        pc = c[j - 1] if j > 0 else NAN
        if np.isfinite(pc) and np.isfinite(lp) and pc > lp:
            ls[j] = max(lr, lp) if np.isfinite(lr) else NAN
        else:
            ls[j] = lr
        if np.isfinite(pc) and np.isfinite(sp) and pc < sp:
            ss[j] = min(sr, sp) if np.isfinite(sr) else NAN
        else:
            ss[j] = sr
        t = d[j - 1] if j > 0 else 1
        if t == 1 and np.isfinite(ls[j]) and c[j] < ls[j]:
            t = -1
        elif t == -1 and np.isfinite(ss[j]) and c[j] > ss[j]:
            t = 1
        d[j] = t
        if j > 0:
            buy[j] = t == 1 and d[j - 1] == -1
            sell[j] = t == -1 and d[j - 1] == 1
    return buy, sell


@njit(cache=True, nogil=True)
def _rsi(c, n):
    m = c.shape[0]
    delta = np.full(m, NAN)
    for j in range(1, m):
        delta[j] = c[j] - c[j - 1]
    gain = np.zeros(m)
    loss = np.zeros(m)
    for j in range(1, m):
        if delta[j] > 0:
            gain[j] = delta[j]
        elif delta[j] < 0:
            loss[j] = -delta[j]
    avg_gain = _rma(gain, n)
    avg_loss = _rma(loss, n)
    out = np.full(m, NAN)
    for j in range(m):
        if not (np.isfinite(avg_gain[j]) and np.isfinite(avg_loss[j])):
            continue
        if avg_loss[j] == 0:
            out[j] = 100.0
        else:
            rs = avg_gain[j] / avg_loss[j]
            out[j] = 100.0 - 100.0 / (1.0 + rs)
    return out


@njit(cache=True, nogil=True)
def _stoch(c, h, lo, n):
    out = np.full(c.shape[0], NAN)
    for j in range(n - 1, c.shape[0]):
        hh = h[j]
        ll = lo[j]
        ok = True
        for k in range(j - n + 1, j + 1):
            if not (np.isfinite(h[k]) and np.isfinite(lo[k]) and np.isfinite(c[k])):
                ok = False
            if h[k] > hh:
                hh = h[k]
            if lo[k] < ll:
                ll = lo[k]
        if ok:
            out[j] = 0.0 if hh == ll else 100.0 * (c[j] - ll) / (hh - ll)
    return out


@njit(cache=True, nogil=True)
def _cci(hlc3, n):
    out = np.full(hlc3.shape[0], NAN)
    for j in range(n - 1, hlc3.shape[0]):
        s = 0.0
        ok = True
        for k in range(j - n + 1, j + 1):
            if not np.isfinite(hlc3[k]):
                ok = False
            s += hlc3[k]
        if not ok:
            continue
        mean = s / n
        dev = 0.0
        for k in range(j - n + 1, j + 1):
            dev += abs(hlc3[k] - mean)
        dev /= n
        out[j] = 0.0 if dev == 0 else (hlc3[j] - mean) / (0.015 * dev)
    return out


@njit(cache=True, nogil=True)
def _wpr(h, lo, c, n):
    out = np.full(c.shape[0], NAN)
    for j in range(n - 1, c.shape[0]):
        hh = h[j]
        ll = lo[j]
        ok = True
        for k in range(j - n + 1, j + 1):
            if not (np.isfinite(h[k]) and np.isfinite(lo[k])):
                ok = False
            if h[k] > hh:
                hh = h[k]
            if lo[k] < ll:
                ll = lo[k]
        if ok:
            out[j] = 0.0 if hh == ll else -100.0 * (hh - c[j]) / (hh - ll)
    return out


@njit(cache=True, nogil=True)
def _mfi(h, lo, c, v, n):
    m = c.shape[0]
    typical = (h + lo + c) / 3.0
    out = np.full(m, NAN)
    for j in range(n, m):
        pos = 0.0
        neg = 0.0
        ok = True
        for k in range(j - n + 1, j + 1):
            if not (np.isfinite(typical[k]) and np.isfinite(typical[k - 1]) and np.isfinite(v[k])):
                ok = False
            raw = typical[k] * v[k]
            if typical[k] > typical[k - 1]:
                pos += raw
            elif typical[k] < typical[k - 1]:
                neg += raw
        if ok:
            out[j] = 100.0 if neg == 0 else 100.0 - 100.0 / (1.0 + pos / neg)
    return out


@njit(cache=True, nogil=True)
def _ultosc(h, lo, c):
    m = c.shape[0]
    hh = np.empty(m)
    ll = np.empty(m)
    for j in range(m):
        prev = c[j - 1] if j > 0 else c[j]
        hh[j] = h[j] if h[j] > prev else prev
        ll[j] = lo[j] if lo[j] < prev else prev
    bp = c - ll
    tr = hh - ll
    out = np.full(m, NAN)
    for j in range(27, m):
        parts = 0.0
        weights = (4.0, 2.0, 1.0)
        lengths = (7, 14, 28)
        ok = True
        for i in range(3):
            bsum = 0.0
            tsum = 0.0
            length = lengths[i]
            for k in range(j - length + 1, j + 1):
                if not (np.isfinite(bp[k]) and np.isfinite(tr[k])):
                    ok = False
                bsum += bp[k]
                tsum += tr[k]
            if tsum == 0:
                ok = False
            else:
                parts += weights[i] * bsum / tsum
        if ok:
            out[j] = 100.0 * parts / 7.0
    return out


@njit(cache=True, nogil=True)
def tke_cross_1d(h, lo, c, v, period, level):
    """TKE v4.2 默认买点: TKE 上穿 level (默认 20)。卖出信号源脚本没有单独定义。"""
    m = c.shape[0]
    hlc3 = (h + lo + c) / 3.0
    mom = np.full(m, NAN)
    for j in range(period, m):
        prev = c[j - period]
        if np.isfinite(prev) and prev != 0 and np.isfinite(c[j]):
            mom[j] = c[j] / prev * 100.0
    tke = (
        _ultosc(h, lo, c)
        + _mfi(h, lo, c, v, period)
        + mom
        + _cci(hlc3, period)
        + _rsi(c, period)
        + _wpr(h, lo, c, period)
        + _stoch(c, h, lo, period)
    ) / 7.0
    line = np.full(m, level)
    buy = _crossover(tke, line)
    sell = np.zeros(m, dtype=np.bool_)
    return buy, sell


# ── 矩阵驱动: 逐只股票压缩有效 K 线 → 内核 → 写回 ─────────────────

(KIND_SUPERTREND, KIND_UTBOT, KIND_CE, KIND_ZLSMA, KIND_HALFTREND, KIND_SQUEEZE, KIND_WAVETREND, KIND_SMC,
 KIND_PHANTOM_TRAIL, KIND_TKE) = range(10)


@njit(cache=True, nogil=True, parallel=True)
def _drive(kind, o, h, lo, c, v, offsets, rows, ip, fp):
    T, N = c.shape  # noqa: N806 (矩阵轴: T 根 x N 只, 与 matrix.py 同名)
    entry = np.zeros((T, N), dtype=np.bool_)
    exit_ = np.zeros((T, N), dtype=np.bool_)
    for a in prange(N):
        s0 = offsets[a]
        s1 = offsets[a + 1]
        m = s1 - s0
        if m < 2:
            continue
        hh = np.empty(m)
        ll = np.empty(m)
        cc = np.empty(m)
        vv = np.empty(m)
        for k in range(m):
            r = rows[s0 + k]
            hh[k] = h[r, a]
            ll[k] = lo[r, a]
            cc[k] = c[r, a]
            x = v[r, a]
            vv[k] = x if np.isfinite(x) else 0.0
        if kind == KIND_SUPERTREND:
            b, s = supertrend_1d(hh, ll, cc, ip[0], fp[0])
        elif kind == KIND_UTBOT:
            b, s = ut_bot_1d(cc, fp[0], ip[0], hh, ll)
        elif kind == KIND_CE:
            b, s = chandelier_exit_1d(hh, ll, cc, vv, ip[0], fp[0], ip[1] == 1, ip[2] == 1, fp[1], ip[3] == 1, ip[4],
                                      ip[5] == 1, ip[6])
        elif kind == KIND_ZLSMA:
            b, s = zlsma_1d(cc, ip[0], ip[1])
        elif kind == KIND_HALFTREND:
            b, s = halftrend_1d(hh, ll, cc, ip[0], ip[1], ip[2] == 1)
        elif kind == KIND_SQUEEZE:
            b, s = squeeze_momentum_1d(hh, ll, cc, ip[0], fp[0], ip[1], fp[1], ip[2] == 1)
        elif kind == KIND_WAVETREND:
            b, s = wavetrend_1d(hh, ll, cc, ip[0], ip[1], ip[2], fp[0], fp[1])
        elif kind == KIND_SMC:
            b, s = smc_swing_1d(hh, ll, cc, ip[0], ip[1] == 1)
        elif kind == KIND_TKE:
            b, s = tke_cross_1d(hh, ll, cc, vv, ip[0], fp[0])
        else:
            b, s = phantom_trail_1d(hh, ll, cc, ip[0], fp[0])
        for k in range(m):
            r = rows[s0 + k]
            entry[r, a] = b[k]
            exit_[r, a] = s[k]
    return entry, exit_


def run_kernel(market, kind: int, ints: tuple, floats: tuple = ()) -> tuple[np.ndarray, np.ndarray]:
    """对 MarketDataMatrix 跑一个内核, 返回 (entry, exit) 两个 (T, N) 布尔矩阵。有效 K 线 = OHLC 都有值。"""
    o = np.asarray(market.open, dtype=np.float64)
    h = np.asarray(market.high, dtype=np.float64)
    lo = np.asarray(market.low, dtype=np.float64)
    c = np.asarray(market.close, dtype=np.float64)
    v = np.asarray(market.volume, dtype=np.float64)
    valid = np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & np.isfinite(c)
    idx = _build_valid_bar_index(valid)
    ip = np.asarray(ints, dtype=np.int64)
    fp = np.asarray(floats if floats else (0.0,), dtype=np.float64)
    return run_numba_parallel(lambda: _drive(int(kind), o, h, lo, c, v, idx.offsets.astype(np.int64),
                                             idx.rows.astype(np.int64), ip, fp))


def neutral_score(market) -> np.ndarray:
    """源脚本没有排序分: 用 (交易日, 代码) 的确定性伪随机数排序 = 从当日命中里随机抽, 不引入额外因子倾向。
    只依赖日期与代码字符串 (不依赖矩阵列序), 生产滑动窗口与全历史回测选出同一批。"""
    import zlib
    from datetime import date as _date
    days = np.array([_date.fromisoformat(str(x)[:10]).toordinal() for x in market.timestamp_labels], dtype=np.float64)
    syms = np.array([zlib.crc32(str(s).encode()) % 100003 for s in market.symbols], dtype=np.float64)
    x = np.sin(syms[None, :] * 12.9898 + days[:, None] * 78.233) * 43758.5453
    return (x - np.floor(x)).astype(np.float32)
