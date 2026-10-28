"""S2 回踩 setup — 上升趋势中的缩量回踩均线反弹 (纯函数, 零 IO)。

形态 (docs/architecture.md):
- 趋势: 收盘 > MA50 且 MA50 上行, 近 3 月相对强度前 rs_top_pct%
- 回踩: 从近 lookback_high_days 日高点回撤 retrace_pct[0]%~retrace_pct[1]%, 缩量
  (量能 < volume_ma_days 日均量 × volume_dryup_ratio)
- 企稳: 出现下影线/小实体企稳 K 线, 或收复 MA20
- 入场区: 企稳 K 线高点附近 ±entry_zone_pct%
- 失效: 回踩低点下方一个 ATR 缓冲
- 目标: 前高 (RR≥min_reward_risk 才成卡)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl


@dataclass(frozen=True)
class PullbackHit:
    symbol: str
    as_of: date
    entry_zone: tuple[float, float]
    invalidation: float
    target: float                # 前高
    retrace_pct: float           # 实际回撤 %
    rs_pctile: float             # 相对强度分位 (由引擎注入, 本模块不横截面)
    last_close: float
    consolidation_low: float


def _ma(closes: np.ndarray, n: int) -> float:
    return float(np.mean(closes[-n:])) if len(closes) >= n else float(closes[-1])


def _atr(df: pl.DataFrame, n: int = 14) -> float:
    if df.height < n + 1:
        return 0.0
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    prev_close = np.roll(close, 1)[1:]
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)))
    return float(np.mean(tr[-n:]))


def detect_pullback(
    symbol: str,
    bars: pl.DataFrame,
    params: dict,
    as_of: date,
) -> PullbackHit | None:
    """识别回踩 setup。bars 须按 date 升序, 只含 as_of 及之前。"""
    if bars.height < params["universe"]["min_history_days"]:
        return None
    p = params["pullback"]
    closes = bars["close"].to_numpy()
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    volumes = bars["volume"].to_numpy()
    last_close = float(closes[-1])
    n = bars.height

    # ── 1. 趋势: 收盘 > MA50 且 MA50 上行 ──
    ma50 = _ma(closes, p["trend_ma"])
    ma50_prev = _ma(closes[:-5], p["trend_ma"]) if n > p["trend_ma"] + 5 else ma50
    if last_close <= ma50 or ma50 <= ma50_prev:
        return None

    # ── 2. 回踩: 近 lookback_high_days 高点回撤 retrace_pct 区间 ──
    lb = p["lookback_high_days"]
    recent_high = float(highs[-lb:].max())
    if recent_high <= 0:
        return None
    retrace = (recent_high - last_close) / recent_high * 100
    if not (p["retrace_pct"][0] <= retrace <= p["retrace_pct"][1]):
        return None

    # ── 3. 缩量: 最近量能 < 均量 × dryup_ratio ──
    vol_ma = float(np.mean(volumes[-p["volume_ma_days"]:]))
    if vol_ma <= 0:
        return None
    last_vol = float(volumes[-1])
    if last_vol > vol_ma * p["volume_dryup_ratio"]:
        return None

    # ── 4. 企稳 K 线: 下影线 (低点明显低于收盘) 或小实体 ──
    last_low = float(lows[-1])
    last_high = float(highs[-1])
    body = abs(last_close - float(bars["open"][-1]))
    rng = last_high - last_low
    if rng <= 0:
        return None
    lower_shadow = (min(last_close, float(bars["open"][-1])) - last_low) / rng
    small_body = body / rng < 0.4
    # 收复 MA20: 收盘 > MA20
    ma20 = _ma(closes, 20)
    reclaimed = last_close > ma20
    if not (lower_shadow > 0.4 or small_body or reclaimed):
        return None

    # ── 5. 提案数值 ──
    atr = _atr(bars)
    cons_low = float(lows[-5:].min())  # 回踩低点 (近 5 日)
    invalidation = round(cons_low - atr, 4)
    entry_mid = last_close
    entry_zone = (round(entry_mid * (1 - p["entry_zone_pct"] / 100), 4),
                  round(entry_mid * (1 + p["entry_zone_pct"] / 100), 4))
    if invalidation >= entry_zone[0]:
        return None
    risk = entry_mid - invalidation
    if risk <= 0:
        return None
    rr = (recent_high - entry_mid) / risk
    if rr < p["min_reward_risk"]:
        return None

    return PullbackHit(
        symbol=symbol,
        as_of=as_of,
        entry_zone=entry_zone,
        invalidation=invalidation,
        target=recent_high,
        retrace_pct=round(retrace, 2),
        rs_pctile=0.0,   # 由引擎横截面注入
        last_close=last_close,
        consolidation_low=cons_low,
    )
