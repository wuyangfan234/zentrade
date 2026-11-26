"""方向门的滞后 / 假翻转指标 (P1-A 一级指标, 纯函数)。

- 拐点: ZigZag (收盘价, 阈值 7% / 10%)。高点拐点之后趋势转空, 低点拐点之后趋势转多。
- 门状态序列: 每个交易日 "long" / "short" / None (None = 不放行任一方向)。
- 只统计拐点当天门仍在旧方向的拐点 (真正需要翻转的); 拐点当天门已不在旧方向的计入 "aligned", 不进滞后分布。
- 翻转滞后: 拐点日 → 门首次给出新方向 (高点后 short / 低点后 long) 的交易日数; 到下一个拐点仍未翻 = 漏翻。
- 停放行滞后: 拐点日 → 门首次不再放行旧方向 (高点后 != long / 低点后 != short) 的交易日数。
- 假翻转: 门翻到新方向后 20 个交易日内又回到旧方向的次数; 汇总时除以拐点数。
"""
from __future__ import annotations

import numpy as np

FALSE_FLIP_WINDOW = 20


def zigzag(closes, pct: float) -> list[tuple[int, str]]:
    """→ [(下标, "H"|"L")], 交替出现。pct = 0.07 表示 7%。首个枢轴之前的走势不计。"""
    c = np.asarray(closes, dtype=float)
    n = c.size
    if n < 3:
        return []
    piv: list[tuple[int, str]] = []
    trend = 0                      # 1 = 向上找高点, -1 = 向下找低点, 0 = 未定
    hi_i = lo_i = 0
    for i in range(1, n):
        x = c[i]
        if not np.isfinite(x):
            continue
        if trend == 0:
            if x > c[hi_i]:
                hi_i = i
            if x < c[lo_i]:
                lo_i = i
            if x <= c[hi_i] * (1 - pct):
                piv.append((hi_i, "H"))
                trend, lo_i = -1, i
            elif x >= c[lo_i] * (1 + pct):
                piv.append((lo_i, "L"))
                trend, hi_i = 1, i
        elif trend == 1:
            if x > c[hi_i]:
                hi_i = i
            elif x <= c[hi_i] * (1 - pct):
                piv.append((hi_i, "H"))
                trend, lo_i = -1, i
        else:
            if x < c[lo_i]:
                lo_i = i
            elif x >= c[lo_i] * (1 + pct):
                piv.append((lo_i, "L"))
                trend, hi_i = 1, i
    return piv


def lags(states: list, pivots: list[tuple[int, str]]) -> dict:
    """→ {"flip": [...], "exit": [...], "missed": n, "false_flips": n, "pivots": n, "aligned": n}。"""
    n = len(states)
    flip, exit_, missed, false_flips, aligned = [], [], 0, 0, 0
    for k, (p, kind) in enumerate(pivots):
        new, old = ("short", "long") if kind == "H" else ("long", "short")
        if states[p] != old:
            aligned += 1
            continue
        end = pivots[k + 1][0] if k + 1 < len(pivots) else n
        f = next((t for t in range(p, end) if states[t] == new), None)
        e = next((t for t in range(p, end) if states[t] != old), None)
        if e is not None:
            exit_.append(e - p)
        if f is None:
            missed += 1
            continue
        flip.append(f - p)
        prev = states[f]
        for t in range(f + 1, min(n, f + 1 + FALSE_FLIP_WINDOW)):
            if states[t] == old and prev != old:
                false_flips += 1
            prev = states[t]
    return {"flip": flip, "exit": exit_, "missed": missed, "false_flips": false_flips, "pivots": len(pivots),
            "aligned": aligned}


def summarize(parts: list[dict]) -> dict:
    flip = [x for p in parts for x in p["flip"]]
    ex = [x for p in parts for x in p["exit"]]
    piv = sum(p["pivots"] for p in parts)
    ff = sum(p["false_flips"] for p in parts)
    missed = sum(p["missed"] for p in parts)
    aligned = sum(p.get("aligned", 0) for p in parts)

    def q(a, v):
        return round(float(np.percentile(a, v)), 1) if a else None
    return {"pivots": piv, "aligned": aligned, "measured": piv - aligned, "flipped": len(flip), "missed": missed,
            "flip_median": q(flip, 50), "flip_p80": q(flip, 80),
            "exit_median": q(ex, 50), "exit_p80": q(ex, 80),
            "false_flips": ff, "false_per_pivot": round(ff / piv, 3) if piv else None}
