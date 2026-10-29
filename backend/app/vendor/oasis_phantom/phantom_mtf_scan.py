"""逐字摘自 oasis-quant engine/phantom_mtf.py @52730bb（只摘函数体，未改一行逻辑）。

摘录: resample_ohlcv / simplified_phantom_scan。原模块顶层有副作用（创建运行时状态目录、导入
premarket_radar / data_feed / yfinance），这里只换 import 来源，函数代码与原文件逐字一致
（tests/test_oasis_phantom_vendor.py 校验与原文件的 sha256 片段一致）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.vendor.oasis_phantom.phantom_state_machine import (  # noqa: F401  原文件同名导入
    atr, ema, phantom_shift, phantom_trail, dmi,
    leg_pivots, structure_bias, pivots_symmetric,
)

BULLISH, BEARISH = 1, -1


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """重采样 OHLCV。"""
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])


def simplified_phantom_scan(df: pd.DataFrame, ticker: str, timeframe: str, daily_atr_pct: float | None = None) -> dict:
    """简化版 Phantom 扫描（4h/2h 专用）。

    跳过 OSC 严格归一化（数据量不足），用以下替代：
    - 趋势: Phantom Shift + EMA20/50
    - 结构: leg() 之字 + bias
    - 动能: MACD 柱状图斜率 + RSI
    - 风险: ATR% + 跳空

    2026-09-02 P0-2（产品决策）：protection 用日线 ATR(14) 重算——4h/2h bar ATR 太小
    导致保护位距触发价仅 ~1.66%（真实 Phantom 日线 4.84%）→ 60 个 INVALIDATED 里
    52 个距离 <4.84%（盘中插针误判"未进场先失效"）。传入 daily_atr_pct（日线 ATR%）
    后，protection = close ± 3×daily_atr_pct（约 3-5% 距离，对齐真实 Phantom）。
    传 None 时回退旧逻辑（4h/2h bar ATR），保持向后兼容。
    """
    r = {
        "ticker": ticker, "timeframe": timeframe, "data_ok": False,
        "close": np.nan, "direction": "", "action": "",
        "trigger": "", "score": 0.0, "adx": np.nan, "atr_pct": np.nan,
        "rsi": np.nan, "macd_hist_slope": np.nan,
        "ema20": np.nan, "ema50": np.nan,
        "shift_trend": 0, "structure_bias": 0,
        "protection": np.nan, "risk_flags": "",
    }
    if len(df) < 60:
        r["error"] = f"数据不足({len(df)}根)"
        return r

    df = df.copy().dropna(subset=["open", "high", "low", "close"])
    c = df["close"]
    n = len(df)

    # 趋势
    shift_trend, up_shift, dn_shift = phantom_shift(df, 14, 3.2)
    trl_dir, long_trail, short_trail = phantom_trail(df, 22, 3.0)
    ema20 = ema(c, 20)
    ema50 = ema(c, 50)

    # 结构
    iph, ipl = leg_pivots(df, 5)
    internal_bias, internal_bull, internal_bear = structure_bias(df, iph, ipl)

    # 动能
    ema12 = ema(c, 12)
    ema26 = ema(c, 26)
    macd = ema12 - ema26
    macd_signal = ema(macd, 9)
    macd_hist = macd - macd_signal
    macd_slope = macd_hist.diff(3)  # 3根斜率

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # ATR
    atr14 = atr(df, 14)
    atr_pct = atr14 / c * 100

    # DMI
    plus_di, minus_di, adx = dmi(df, 14, 14)

    # 风险护盾（简化）
    gap_abs = (df["open"] - c.shift(1)).abs()
    rsk_gap = gap_abs > 0.5 * atr14
    rsk_huge = (df["high"] - df["low"]) > 2 * atr14
    rsk_extended = (c - ema50).abs() > 2.5 * atr14
    risk_count = int(rsk_gap.iloc[-1]) + int(rsk_huge.iloc[-1]) + int(rsk_extended.iloc[-1])
    risk_flags = []
    if rsk_gap.iloc[-1]: risk_flags.append("跳空")
    if rsk_huge.iloc[-1]: risk_flags.append("巨K")
    if rsk_extended.iloc[-1]: risk_flags.append("乖离")

    # 最后状态
    j = n - 1
    r["close"] = float(c.iloc[j])
    r["adx"] = float(adx.iloc[j]) if not np.isnan(adx.iloc[j]) else np.nan
    r["atr_pct"] = float(atr_pct.iloc[j]) if not np.isnan(atr_pct.iloc[j]) else np.nan
    r["rsi"] = float(rsi.iloc[j]) if not np.isnan(rsi.iloc[j]) else np.nan
    r["macd_hist_slope"] = float(macd_slope.iloc[j]) if not np.isnan(macd_slope.iloc[j]) else np.nan
    r["ema20"] = float(ema20.iloc[j]) if not np.isnan(ema20.iloc[j]) else np.nan
    r["ema50"] = float(ema50.iloc[j]) if not np.isnan(ema50.iloc[j]) else np.nan
    r["shift_trend"] = int(shift_trend.iloc[j])
    r["structure_bias"] = int(internal_bias.iloc[j])
    # 2026-09-02 P0-2：protection 优先用日线 ATR(14) 重算（宽保护位 ~3-5%）
    # 4h/2h bar ATR 保护的窄距离（~1.66%）是"被冤枉 INVALIDATED"的根源（60 个里 52 个<4.84%）。
    # 日线 ATR 口径：long protection = close − 1.6×daily_atr%；short = close + 1.6×daily_atr%。
    # 1.6× 校准：真实日线 ATR% 均值 2.71% → 距离 ~4.34%（对齐 watchlist 真实 Phantom 4.84%，
    # 实测 3×ATR=9% 太宽会漏止损；1.6× 最贴近实测 4.84%）。
    if daily_atr_pct is not None and np.isfinite(daily_atr_pct) and daily_atr_pct > 0:
        _c_last = float(c.iloc[j])
        if shift_trend.iloc[j] == 1:
            _prot = _c_last * (1 - 1.6 * daily_atr_pct / 100)
        else:
            _prot = _c_last * (1 + 1.6 * daily_atr_pct / 100)
        r["protection"] = _prot
        r["protection_basis"] = "daily_atr"   # 标注口径：日线 ATR（P0-2 新口径）
    else:
        r["protection"] = float(long_trail.iloc[j]) if shift_trend.iloc[j] == 1 else float(short_trail.iloc[j])
        r["protection_basis"] = "tf_atr"      # 旧口径：时间框架 bar ATR
    r["risk_flags"] = " ".join(risk_flags)
    r["data_ok"] = True

    # 方向判断
    trend_up = shift_trend.iloc[j] == 1 and c.iloc[j] > ema50.iloc[j]
    trend_dn = shift_trend.iloc[j] == -1 and c.iloc[j] < ema50.iloc[j]

    # 触发检测（最近3根）
    recent_shift_buy = any(shift_trend.iloc[k] == 1 and shift_trend.iloc[k-1] == -1 for k in range(max(1, j-2), j+1))
    recent_shift_sell = any(shift_trend.iloc[k] == -1 and shift_trend.iloc[k-1] == 1 for k in range(max(1, j-2), j+1))
    recent_bull_struct = internal_bull.iloc[max(0, j-2):j+1].any()
    recent_bear_struct = internal_bear.iloc[max(0, j-2):j+1].any()

    # 做多信号
    if trend_up and internal_bias.iloc[j] == BULLISH:
        if recent_shift_buy or recent_bull_struct:
            if macd_hist.iloc[j] > macd_hist.iloc[j-1] and macd_hist.iloc[j] > 0:
                r["direction"] = "↑ 看多"
                r["action"] = "多头条件齐备"
                r["trigger"] = "Shift翻多" if recent_shift_buy else "结构转多"
                r["score"] = 70 + min(max(r["adx"] - 20, 0), 15) if not np.isnan(r["adx"]) else 70
                if r["rsi"] < 70: r["score"] += 5
                if r["macd_hist_slope"] > 0: r["score"] += 5
                r["score"] -= risk_count * 5
            else:
                r["direction"] = "↑ 看多"
                r["action"] = "动能待确认"
                r["score"] = 45
        else:
            r["direction"] = "↑ 看多"
            r["action"] = "等待触发"
            r["score"] = 30
    # 做空信号
    elif trend_dn and internal_bias.iloc[j] == BEARISH:
        if recent_shift_sell or recent_bear_struct:
            if macd_hist.iloc[j] < macd_hist.iloc[j-1] and macd_hist.iloc[j] < 0:
                r["direction"] = "↓ 看空"
                r["action"] = "空头条件齐备"
                r["trigger"] = "Shift翻空" if recent_shift_sell else "结构转空"
                r["score"] = 70 + min(max(r["adx"] - 20, 0), 15) if not np.isnan(r["adx"]) else 70
                if r["rsi"] > 30: r["score"] += 5
                if r["macd_hist_slope"] < 0: r["score"] += 5
                r["score"] -= risk_count * 5
            else:
                r["direction"] = "↓ 看空"
                r["action"] = "动能待确认"
                r["score"] = 45
        else:
            r["direction"] = "↓ 看空"
            r["action"] = "等待触发"
            r["score"] = 30
    else:
        r["direction"] = "↔ 震荡"
        r["action"] = "观望"
        r["score"] = 0

    r["score"] = round(max(min(r["score"], 100), 0), 1)
    return r
