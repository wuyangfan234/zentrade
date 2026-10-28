"""S1 突破 setup — 强势股盘整后的区间突破 (纯函数, 零 IO)。

形态 (docs/architecture.md):
- 腿部: 近 leg_window_days 内出现过 ≥leg_min_gain_pct 的上涨腿 (≤leg_max_days 完成)
- 整理: 随后 cons_min_days~cons_max_days 的窄幅整理 (回撤 ≤ 腿幅×cons_max_retrace_frac,
  ATR 收缩到腿内 ATR 的 cons_atr_shrink 以下, 收盘贴 MA10 ±ma10_tolerance_pct)
- 触发: 整理区上沿 +entry_zone_above_pct 为入场区
- 失效: 整理区下沿或 MA10 下方 (取紧者)
- 目标: R≥min_reward_risk 的投射位 (腿幅投射)

时间锚定: 只消费 as_of 及之前的 bars, 回放安全。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl


@dataclass(frozen=True)
class BreakoutHit:
    """setup 识别命中 (未成卡, 提案构造在 engine 层)。"""
    symbol: str
    as_of: date
    cons_top: float              # 整理区上沿
    cons_bottom: float           # 整理区下沿
    ma10: float
    leg_gain_pct: float          # 腿幅 %
    cons_days: int               # 整理天数
    atr_ratio: float             # 整理期 ATR / 腿内 ATR
    last_close: float
    leg_target: float            # 腿幅投射目标 (cons_top + leg 高度)


MIN_ATR_BARS = 5


def _atr(df: pl.DataFrame, n: int = 14) -> float:
    """Wilder ATR 简化版 (SMA of true range)。

    rev3: 不足 n+1 根时按实际可用根数计算 (至少 MIN_ATR_BARS 根)。旧实现直接返回 0,
    整理期 10–14 天的卡因此静默跳过 “整理期 ATR ≤ 腿内 ATR × 收缩比” 检查 (重放 65%)。
    返回 0 只表示样本确实不足, 调用方必须把 0 当“无法判断”而不是“极度收缩”。
    """
    if df.height < MIN_ATR_BARS + 1:
        return 0.0
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    prev_close = np.roll(close, 1)[1:]
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)))
    return float(np.mean(tr[-n:]))


def _strongest_leg(closes: np.ndarray, first: int, last: int, leg_max_days: int) -> tuple[int, int, float]:
    """枚举 i∈[first,last), j∈(i, min(i+leg_max_days, n)) 的最大涨幅 (%) 及首个达到它的 (i, j)。

    与逐对双循环严格等价: 起点 close≤0 的腿按 0% 计; 只接受 >0 的涨幅;
    同值取 (i, j) 字典序最先者。向量化只为速度 (全市场逐日扫描)。
    """
    n = len(closes)
    first = max(first, 0)
    if last <= first or n < 2 or leg_max_days <= 1:
        return -1, -1, 0.0
    starts = np.arange(first, last)
    offsets = np.arange(1, leg_max_days)
    ends = starts[:, None] + offsets[None, :]
    valid = ends < n
    base = closes[starts][:, None]
    target = closes[np.minimum(ends, n - 1)]
    with np.errstate(divide="ignore", invalid="ignore"):
        gains = np.where(base > 0, (target - base) / base * 100, 0.0)
    # 原循环的 NaN > best 恒为 False；不能让 argmax 的 NaN 吞掉合法上涨腿。
    gains = np.where(valid & ~np.isnan(gains), gains, -np.inf)
    flat = int(np.argmax(gains))
    best = float(gains.flat[flat])
    if not best > 0:
        return -1, -1, 0.0
    i, k = divmod(flat, gains.shape[1])
    return int(starts[i]), int(ends[i, k]), best


def detect_breakout(
    symbol: str,
    bars: pl.DataFrame,
    params: dict,
    as_of: date,
) -> BreakoutHit | None:
    """识别突破 setup。bars 须按 date 升序, 且只含 as_of 及之前的数据。"""
    if bars.height < params["universe"]["min_history_days"]:
        return None
    p = params["breakout"]
    closes = bars["close"].to_numpy()
    highs = bars["high"].to_numpy()
    last_close = float(closes[-1])

    # ── 1. 找腿: 近 leg_window_days 内的最强上涨 ──
    n = bars.height
    win = min(p["leg_window_days"], n - 1)
    leg_start_idx, leg_end_idx, leg_gain = _strongest_leg(closes, n - win - 1, n - 1, p["leg_max_days"])
    if leg_gain < p["leg_min_gain_pct"]:
        return None
    leg_height = closes[leg_end_idx] - closes[leg_start_idx]

    # ── 2. 整理: 腿顶之后 cons_min_days ~ cons_max_days 的整理区 ──
    cons_start = leg_end_idx + 1
    cons_len = n - 1 - cons_start  # 到 as_of (最后一根)
    if cons_len < p["cons_min_days"] or cons_len > p["cons_max_days"]:
        return None
    cons = bars.slice(cons_start, cons_len)
    cons_top = float(cons["high"].max())
    cons_bottom = float(cons["low"].min())
    # 回撤约束: 整理区下沿不能跌破腿幅的 cons_max_retrace_frac
    if leg_height > 0 and (closes[leg_end_idx] - cons_bottom) > leg_height * p["cons_max_retrace_frac"]:
        return None

    # ATR 收缩: 整理期 ATR ≤ 腿内 ATR × cons_atr_shrink
    leg_bars = bars.slice(leg_start_idx, leg_end_idx - leg_start_idx + 1)
    leg_atr = _atr(leg_bars)
    cons_atr = _atr(cons)
    if leg_atr <= 0 or cons_atr <= 0:
        return None  # 无法验证收缩 → 不成卡 (不再静默放行)
    if cons_atr > leg_atr * p["cons_atr_shrink"]:
        return None

    # MA10 贴合: 最近收盘距 MA10 ≤ ma10_tolerance_pct
    ma10 = float(np.mean(closes[-10:])) if n >= 10 else last_close
    if ma10 <= 0 or abs(last_close - ma10) / ma10 > p["ma10_tolerance_pct"] / 100:
        return None

    # 整理区不能贴在腿上沿之上过多 (突破前不能在天上): 整理区下沿不低于腿顶 - 腿幅×1.0
    if cons_bottom < closes[leg_end_idx] - leg_height:
        return None

    leg_target = cons_top + leg_height  # 腿幅投射
    return BreakoutHit(
        symbol=symbol,
        as_of=as_of,
        cons_top=cons_top,
        cons_bottom=cons_bottom,
        ma10=ma10,
        leg_gain_pct=round(leg_gain, 2),
        cons_days=cons_len,
        atr_ratio=round(cons_atr / leg_atr, 3),
        last_close=last_close,
        leg_target=leg_target,
    )
