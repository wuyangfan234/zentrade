"""Phantom 4h 逐根信号 (vendor 原代码, 无前视)。

`simplified_phantom_scan(df[:j+1])` 只读最后一根; 其内部所有序列 (EMA/RMA/rolling/棘轮/结构状态机) 都是因果的,
所以在整段历史上算一次、逐根读取，与“每根都对截至该根的前缀调用一次原函数”完全等价。
`scan_series` 复刻原函数“最后状态”段的逐根版本（字段与判定逐字对应原函数 178–262 行）；
`tests/test_phantom_4h.py` 抽样把它和逐前缀调用原函数做对照，必须 100% 一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.vendor.oasis_phantom.phantom_state_machine import (
    atr, dmi, ema, leg_pivots, phantom_shift, phantom_trail, structure_bias,
)

BULLISH, BEARISH = 1, -1
MIN_BARS = 60  # 原函数门槛


def scan_series(df: pd.DataFrame, daily_atr_pct: pd.Series | None = None) -> pd.DataFrame:
    """df: 4h OHLCV (DatetimeIndex 升序)。daily_atr_pct: 与 df 同索引的“截至该根已知的日线 ATR%”(可缺)。

    返回逐根: action/direction/trigger/score/protection/long_trail/shift_trend/close。前 59 根为空(数据不足)。
    """
    df = df.copy().dropna(subset=["open", "high", "low", "close"])
    c = df["close"]
    n = len(df)
    out = pd.DataFrame(index=df.index)
    if n < MIN_BARS:
        out["action"] = ""
        return out

    # —— 以下指标段逐字对应原函数 136–176 行 ——
    shift_trend, up_shift, dn_shift = phantom_shift(df, 14, 3.2)
    trl_dir, long_trail, short_trail = phantom_trail(df, 22, 3.0)
    ema50 = ema(c, 50)  # 原函数另算 ema20 只用于输出字段, 不参与判定
    iph, ipl = leg_pivots(df, 5)
    internal_bias, internal_bull, internal_bear = structure_bias(df, iph, ipl)
    ema12 = ema(c, 12)
    ema26 = ema(c, 26)
    macd = ema12 - ema26
    macd_signal = ema(macd, 9)
    macd_hist = macd - macd_signal
    macd_slope = macd_hist.diff(3)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    atr14 = atr(df, 14)
    plus_di, minus_di, adx = dmi(df, 14, 14)
    gap_abs = (df["open"] - c.shift(1)).abs()
    rsk_gap = gap_abs > 0.5 * atr14
    rsk_huge = (df["high"] - df["low"]) > 2 * atr14
    rsk_extended = (c - ema50).abs() > 2.5 * atr14
    risk_count = rsk_gap.astype(int) + rsk_huge.astype(int) + rsk_extended.astype(int)

    # —— 逐根“最后状态”(原函数 178–262 行, j 从 n-1 推广到每一根) ——
    sb = (shift_trend == 1) & (shift_trend.shift(1) == -1)
    ss = (shift_trend == -1) & (shift_trend.shift(1) == 1)
    # 原: any(k in range(max(1, j-2), j+1)) → 最近 3 根 (k>=1)
    sb_ = sb.copy(); sb_.iloc[0] = False
    ss_ = ss.copy(); ss_.iloc[0] = False
    recent_shift_buy = sb_.rolling(3, min_periods=1).max().astype(bool)
    recent_shift_sell = ss_.rolling(3, min_periods=1).max().astype(bool)
    recent_bull_struct = internal_bull.rolling(3, min_periods=1).max().astype(bool)
    recent_bear_struct = internal_bear.rolling(3, min_periods=1).max().astype(bool)
    trend_up = (shift_trend == 1) & (c > ema50)
    trend_dn = (shift_trend == -1) & (c < ema50)
    hist_up = (macd_hist > macd_hist.shift(1)) & (macd_hist > 0)
    hist_dn = (macd_hist < macd_hist.shift(1)) & (macd_hist < 0)

    tu, td = trend_up.to_numpy(), trend_dn.to_numpy()
    ib = internal_bias.to_numpy()
    rsb, rss = recent_shift_buy.to_numpy(), recent_shift_sell.to_numpy()
    rbs, rbe = recent_bull_struct.to_numpy(), recent_bear_struct.to_numpy()
    hu, hd = hist_up.to_numpy(), hist_dn.to_numpy()
    adx_v = adx.to_numpy()
    rsi_v = rsi.to_numpy()
    slope_v = macd_slope.to_numpy()
    rc = risk_count.to_numpy()
    action = np.full(n, "", dtype=object)
    direction = np.full(n, "", dtype=object)
    trigger = np.full(n, "", dtype=object)
    score = np.zeros(n)
    for j in range(n):
        if j < MIN_BARS - 1:
            continue
        if tu[j] and ib[j] == BULLISH:
            direction[j] = "↑ 看多"
            if rsb[j] or rbs[j]:
                if hu[j]:
                    action[j] = "多头条件齐备"
                    trigger[j] = "Shift翻多" if rsb[j] else "结构转多"
                    s = 70 + min(max(adx_v[j] - 20, 0), 15) if not np.isnan(adx_v[j]) else 70
                    if rsi_v[j] < 70:
                        s += 5
                    if slope_v[j] > 0:
                        s += 5
                    s -= rc[j] * 5
                    score[j] = s
                else:
                    action[j], score[j] = "动能待确认", 45
            else:
                action[j], score[j] = "等待触发", 30
        elif td[j] and ib[j] == BEARISH:
            direction[j] = "↓ 看空"
            if rss[j] or rbe[j]:
                if hd[j]:
                    action[j] = "空头条件齐备"
                    trigger[j] = "Shift翻空" if rss[j] else "结构转空"
                    s = 70 + min(max(adx_v[j] - 20, 0), 15) if not np.isnan(adx_v[j]) else 70
                    if rsi_v[j] > 30:
                        s += 5
                    if slope_v[j] < 0:
                        s += 5
                    s -= rc[j] * 5
                    score[j] = s
                else:
                    action[j], score[j] = "动能待确认", 45
            else:
                action[j], score[j] = "等待触发", 30
        else:
            direction[j], action[j], score[j] = "↔ 震荡", "观望", 0
    score = np.round(np.clip(score, 0, 100), 1)

    # 保护位 (原 189–204 行): 有日线 ATR% → close ∓ 1.6×ATR%; 否则 long_trail / short_trail
    prot = np.where(shift_trend.to_numpy() == 1, long_trail.to_numpy(), short_trail.to_numpy())
    if daily_atr_pct is not None:
        da = daily_atr_pct.reindex(df.index).to_numpy(dtype=float)
        ok = np.isfinite(da) & (da > 0)
        cv = c.to_numpy()
        alt = np.where(shift_trend.to_numpy() == 1, cv * (1 - 1.6 * da / 100), cv * (1 + 1.6 * da / 100))
        prot = np.where(ok, alt, prot)
    out["close"] = c
    out["action"] = action
    out["direction"] = direction
    out["trigger"] = trigger
    out["score"] = score
    out["protection"] = prot
    out["long_trail"] = long_trail
    out["short_trail"] = short_trail
    out["shift_trend"] = shift_trend
    return out
