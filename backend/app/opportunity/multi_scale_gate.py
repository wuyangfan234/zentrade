"""多尺度方向门 ``multi_scale_v1`` (P1-A, 走 direction_gate 插件位)。

三尺度投票 (只用信号日收盘及以前的数据):

- 慢 = 收盘相对 MA50 (与现行 ``daily_ma50`` 同口径);
- 中 = 收盘相对 MA20 且 MA20 的 5 日斜率同向 (两者不一致 = 中性);
- 快 = 4h Phantom 方向状态 (扫描里 Phantom 4h 来源已算好的 ``phantom_4h_state``, 直接复用, 不重算)。

判定:

- 慢中同意 → 放行该方向 (正常级);
- 慢中冲突 (含中性) → 看快层: 快层与慢或中之一同向 → 只放行「观察级」(机会板标观察级, 不进纸面下单簿);
- 其余 (快层缺 / 三者对不上) → 不放行 (fail-closed)。数据不足 → 不放行。

默认不生效: ``preferred=False``, 只有 ``rules/swing.json`` 的 ``"gate": "multi_scale_v1"`` 才启用;
把 ``gate`` 改回 ``daily_ma50`` (或删掉该字段) 即回到现网行为, 无需改代码。
观察级名单通过 ``ctx.precomputed["gate_grades"]`` 交回扫描 (decision_scan_job 写进快照 ``gate_grade``)。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np

from app.opportunity import direction_gate

GATE_ID = "multi_scale_v1"
MID_MA = 20
MID_SLOPE_DAYS = 5
FULL, OBSERVE = "full", "observe"


def slow_side(closes) -> str | None:
    return direction_gate._ma_side(closes, direction_gate.MA_WINDOW)


def mid_side(closes) -> str | None:
    """收盘 > MA20 且 MA20 较 5 日前上行 = long; 收盘 < MA20 且下行 = short; 其余 = None (中性)。"""
    c = np.asarray(closes, dtype=float)
    c = c[np.isfinite(c)]
    if c.size < MID_MA + MID_SLOPE_DAYS:
        return None
    ma_now = float(c[-MID_MA:].mean())
    ma_prev = float(c[-MID_MA - MID_SLOPE_DAYS:-MID_SLOPE_DAYS].mean())
    last = float(c[-1])
    if last > ma_now and ma_now > ma_prev:
        return "long"
    if last < ma_now and ma_now < ma_prev:
        return "short"
    return None


def vote(slow: str | None, mid: str | None, fast: str | None) -> tuple[str | None, str | None]:
    """→ (方向, 级别)。级别: full / observe; 不放行 = (None, None)。"""
    if slow not in ("long", "short"):
        return None, None                      # 慢层数据不足 = fail-closed
    if mid == slow:
        return slow, FULL
    if fast in ("long", "short") and fast in (slow, mid):
        return fast, OBSERVE
    return None, None


def _closes_upto(frame, as_of: date):
    closes = direction_gate._close_series(frame)
    if closes is None:
        return None
    if "date" in frame.columns:
        try:
            import polars as pl
            closes = frame.filter(pl.col("date") <= as_of).sort("date")["close"].to_numpy()
        except Exception:
            pass
    return closes


def _states(ctx: direction_gate.GateContext) -> dict[str, str]:
    fast_map = (ctx.precomputed or {}).get("phantom_4h_state") or {}
    out: dict[str, str] = {}
    grades: dict[str, str] = {}
    series: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for sym in ctx.symbols:
        closes = _closes_upto((ctx.frames or {}).get(sym), ctx.as_of)
        if closes is None:
            missing.append(sym)
        else:
            series[sym] = closes
    if missing and ctx.repo is not None:
        try:
            b = ctx.repo.get_daily_batch(sorted(missing), ctx.as_of - timedelta(days=direction_gate.MA_WINDOW * 2 + 30),
                                         ctx.as_of, columns=["symbol", "date", "close"])
            if b is not None and not b.is_empty():
                for g in b.partition_by("symbol"):
                    series[str(g["symbol"][0])] = g.sort("date")["close"].to_numpy()
        except Exception as e:
            direction_gate.logger.warning("multi_scale gate: daily bars unavailable: %s", type(e).__name__)
    for sym, closes in series.items():
        side, grade = vote(slow_side(closes), mid_side(closes), fast_map.get(sym))
        if side:
            out[sym] = side
            if grade == OBSERVE:
                grades[sym] = OBSERVE
    if isinstance(ctx.precomputed, dict):
        ctx.precomputed["gate_grades"] = grades
    return out


def _matrix(data_dir: Path, repo, symbols: list[str], days: list[date]) -> np.ndarray:
    """研究回放 (T, N): 做多放行 (含观察级)。快层用 phantom_4h 插件的回放矩阵 (只给做多放行);
    插件或 30 分钟K 不可用时快层视为缺 (慢中冲突的格子全部不放行)。"""
    n_days, n_syms = len(days), len(symbols)
    gate = np.zeros((n_days, n_syms), bool)
    if not days or not symbols:
        return gate
    fast = None
    try:
        from app import optional
        p = optional.load("app.services.phantom_4h_gate")
        if p is not None:
            fast = p._matrix(data_dir, repo, symbols, days)
    except Exception as e:
        direction_gate.logger.warning("multi_scale matrix: 4h fast layer unavailable: %s", type(e).__name__)
    batch = repo.get_daily_batch(sorted(symbols), days[0] - timedelta(days=direction_gate.MA_WINDOW * 2 + 30),
                                 days[-1], columns=["symbol", "date", "close"])
    if batch is None or batch.is_empty():
        return gate
    col = {s: j for j, s in enumerate(symbols)}
    day_idx = np.array([np.datetime64(d) for d in days])
    for g in batch.partition_by("symbol"):
        j = col.get(str(g["symbol"][0]))
        if j is None:
            continue
        g = g.sort("date")
        c = g["close"].to_numpy().astype(float)
        dts = np.array([np.datetime64(d) for d in g["date"].to_list()])
        pos = np.searchsorted(dts, day_idx, side="right") - 1
        for t, p in enumerate(pos):
            if p < 0:
                continue
            w = c[: p + 1]
            f = "long" if (fast is not None and fast[t, j]) else None
            side, _grade = vote(slow_side(w), mid_side(w), f)
            gate[t, j] = side == "long"
    return gate


direction_gate.register(direction_gate.DirectionGate(
    id=GATE_ID, name="多尺度方向门 (MA50 + MA20 + 4h 结构投票)", states=_states, matrix=_matrix, preferred=False))
