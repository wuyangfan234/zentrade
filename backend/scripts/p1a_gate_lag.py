# ruff: noqa: RUF002
"""P1-A 研究: 方向门滞后 / 假翻转 + 现行门 vs 多尺度门的卡量、成交、每笔 R (只读, 只写 --out)。

门:
- daily_ma50   : 收盘相对 MA50 (现行内置门);
- ma20_single  : 收盘相对 MA20 (方案书 A6-5 的单尺度对照);
- multi_scale_v1: multi_scale_gate.vote(MA50, MA20+斜率, 4h Phantom 方向) —— 与生产门同一个投票函数;
- phantom_4h   : 4h Phantom 方向 (私有完整版现行门, 作参照)。

4h Phantom 方向: 每个信号日取标签 ≤ 当日 09:30 的最后一根 4h 的 direction (该根 13:30 前已完成, 盘后扫描时可见, 无前视)。

卡量/成交/每笔 R 是「门内全部格子」的日K近似结算 (与 S1 基线「同日同侧门内格子、同一张紧卡」同口径, 不含 3+3 命中筛选):
入场区 [c-0.50A, c-0.25A] 限价, 2 日入场窗; 止损 = 区上沿 -1.0A; 目标 = 区上沿 +2.0A; 持有 5 日; 同日同时触及止损与目标按止损
(保守); 跳空穿越按开盘价成交。R = 盈亏 ÷ (区中值 − 止损)。不是 swing-settle-v3 (该合同用 30 分钟K, 本机无 30 分钟K)。

用法 (backend/ 下):
  .venv/bin/python -m scripts.p1a_gate_lag --daily <zt_daily.parquet> --bars4h <bars4h.parquet> \
      --universe <daily_wl.parquet> --start 2023-01-03 --out <out.json>
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from app.opportunity import gate_metrics as gm
from app.opportunity import multi_scale_gate as ms
from app.opportunity import regime as rg

K_NEAR, K_FAR, STOP_A, TARGET_A, ENTRY_DAYS, HOLD_DAYS = 0.25, 0.50, 1.0, 2.0, 2, 5


def _sma(c, n):
    return rg.sma(np.asarray(c, float), n)


def gate_series(c: np.ndarray, fast: list) -> dict[str, list]:
    n = c.size
    ma50, ma20 = _sma(c, 50), _sma(c, 20)
    out = {"daily_ma50": [None] * n, "ma20_single": [None] * n, "multi_scale_v1": [None] * n,
           "multi_scale_v1_full": [None] * n, "phantom_4h": list(fast)}
    for t in range(n):
        if np.isfinite(ma50[t]):
            out["daily_ma50"][t] = "long" if c[t] > ma50[t] else ("short" if c[t] < ma50[t] else None)
        if np.isfinite(ma20[t]):
            out["ma20_single"][t] = "long" if c[t] > ma20[t] else ("short" if c[t] < ma20[t] else None)
        w = c[: t + 1]
        side, grade = ms.vote(ms.slow_side(w), ms.mid_side(w), fast[t])
        out["multi_scale_v1"][t] = side
        out["multi_scale_v1_full"][t] = side if grade == ms.FULL else None
    return out


def phantom_daily(bars4h: pd.DataFrame | None, days: list[date]) -> list:
    from app import optional
    p4 = optional.load("app.services.phantom_4h")          # 开源核心版没有该插件: 快层视为缺失
    src = optional.load("app.services.phantom_4h_source")
    if bars4h is None or len(bars4h) < 60 or p4 is None or src is None:
        return [None] * len(days)
    side_of = src.DIRECTION_SIDE
    s = p4.scan_series(bars4h)
    idx = s.index.values
    dr = s["direction"].values if "direction" in s.columns else np.array([""] * len(s))
    out = []
    for d in days:
        b = np.searchsorted(idx, np.datetime64(datetime.combine(d, time(9, 30))), "right")
        out.append(side_of.get(str(dr[b - 1])) if b > 0 else None)
    return out


def atr14(h, lo, c):
    pc = np.roll(c, 1)
    tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
    tr[0] = h[0] - lo[0]
    out = np.full(c.size, np.nan)
    for t in range(14, c.size):
        out[t] = tr[t - 13:t + 1].mean()
    return out


def settle_long(o, h, lo, c, t: int, a: float):
    """信号日 t 收盘出卡 → (filled, R) 或 None (数据不够)。"""
    n = c.size
    if not np.isfinite(a) or a <= 0 or t + 1 >= n:
        return None
    hi_z, lo_z = c[t] - K_NEAR * a, c[t] - K_FAR * a
    stop, target = hi_z - STOP_A * a, hi_z + TARGET_A * a
    risk = (hi_z + lo_z) / 2 - stop
    fill_t, px = None, None
    for k in range(t + 1, min(n, t + 1 + ENTRY_DAYS)):
        if lo[k] <= hi_z:
            fill_t, px = k, min(o[k], hi_z)
            break
    if fill_t is None:
        return (False, None) if t + ENTRY_DAYS < n else None
    last = fill_t + HOLD_DAYS - 1
    if last >= n:
        return None
    for k in range(fill_t, last + 1):
        op = o[k] if k > fill_t else px
        if lo[k] <= stop:
            return True, (min(op, stop) - px) / risk
        if h[k] >= target:
            return True, (max(op, target) - px) / risk if k > fill_t else (target - px) / risk
    return True, (c[last] - px) / risk


def week_ci(pairs):
    from app.services import rank_lab
    ci, _sd = rank_lab.week_boot(pairs)
    return ci


def regime_history(spy: pl.DataFrame, start: date, breadth: dict[date, float], min_dwell: int) -> dict:
    """日频回放 regime (只用收盘: 慢层 + 收盘快层; 盘中整点无历史数据, 不回放)。宽度 = 本研究股票池收盘 > MA20 占比。
    min_dwell = 0 → 无驻留限制 (首版口径); 非 0 → 现行 rg.step 的最小驻留 (rg.MIN_DWELL_DAYS, 降级不受限)。"""
    spy = spy.sort("date")
    c, h, lo = (spy[k].to_numpy().astype(float) for k in ("close", "high", "low"))
    days = spy["date"].to_list()
    state, counts, switches, by_day = None, {s: 0 for s in rg.STATES}, 0, {}
    since, runs, run = None, [], 0
    for t in range(len(days)):
        if days[t] < start or t < 80:
            continue
        sv = rg.slow_values(c[: t + 1], breadth.get(days[t]))
        st, _ = rg.slow_target(sv)
        fv = rg.fast_values(c[:t], h[:t], lo[:t], c[t])
        ft, _ = rg.fast_target(fv)
        dwell = None if (since is None or not min_dwell) else t - since      # 交易日数 (SPY 日K 下标差)
        r = rg.step(state, mode="close", slow=st, fast=ft, day=str(days[t]), dwell=dwell)
        if r.changed or state is None:
            if state is not None:
                switches += 1
                runs.append(run)
            since, run = t, 0
        state = r.state
        run += 1
        counts[state] += 1
        by_day[days[t]] = state
    runs.append(run)
    return {"min_dwell": min_dwell, "days": counts, "switches": switches,
            "mean_dwell_days": round(float(np.mean(runs)), 2), "median_dwell_days": float(np.median(runs)),
            "by_day": by_day, "note": "close-only replay (slow + close-fast), breadth = study universe"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True)
    ap.add_argument("--bars4h", required=True)
    ap.add_argument("--universe", required=True, help="parquet with a symbol column (watchlist universe)")
    ap.add_argument("--start", default="2023-01-03")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    daily = pl.read_parquet(a.daily, columns=["symbol", "date", "open", "high", "low", "close"])
    b4 = pl.read_parquet(a.bars4h)
    end = date.fromisoformat(a.end) if a.end else min(daily["date"].max(), b4["bar"].max().date())
    uni = set(pl.read_parquet(a.universe, columns=["symbol"])["symbol"].unique().to_list())
    syms = sorted(uni & set(daily["symbol"].unique().to_list()) & set(b4["symbol"].unique().to_list()))
    gates = ("daily_ma50", "ma20_single", "multi_scale_v1", "phantom_4h")
    lag_parts = {(g, p): [] for g in gates for p in (0.07, 0.10)}
    cards = {g: {"cells": 0, "filled": 0, "r": []} for g in (*gates, "multi_scale_v1_full")}
    flips = {g: 0 for g in gates}
    sym_days = 0
    above_n: dict[date, int] = {}
    total_n: dict[date, int] = {}
    for sym in syms:
        d = daily.filter((pl.col("symbol") == sym) & (pl.col("date") <= end)).sort("date")
        if d.height < 120:
            continue
        days = d["date"].to_list()
        o, h, lo, c = (d[k].to_numpy().astype(float) for k in ("open", "high", "low", "close"))
        bb = b4.filter(pl.col("symbol") == sym).sort("bar").to_pandas().set_index("bar")
        fast = phantom_daily(bb, days)
        gs = gate_series(c, fast)
        i0 = next((i for i, x in enumerate(days) if x >= start), None)
        if i0 is None:
            continue
        sym_days += len(days) - i0
        cw = c[i0:]
        for pct in (0.07, 0.10):
            piv = gm.zigzag(cw, pct)
            for g in gates:
                lag_parts[(g, pct)].append(gm.lags(gs[g][i0:], piv))
        for g in gates:
            s = gs[g][i0:]
            flips[g] += sum(1 for t in range(1, len(s)) if s[t] != s[t - 1] and s[t] is not None)
        ma20 = _sma(c, 20)
        for t in range(len(days)):
            if np.isfinite(ma20[t]):
                total_n[days[t]] = total_n.get(days[t], 0) + 1
                above_n[days[t]] = above_n.get(days[t], 0) + int(c[t] > ma20[t])
        a14 = atr14(h, lo, c)
        for g in (*gates, "multi_scale_v1_full"):
            s = gs[g]
            for t in range(i0, len(days)):
                if s[t] != "long":
                    continue
                res = settle_long(o, h, lo, c, t, a14[t])
                if res is None:
                    continue
                cards[g]["cells"] += 1
                if res[0]:
                    cards[g]["filled"] += 1
                    cards[g]["r"].append((t, float(res[1])))
                    cards[g].setdefault("by_day", []).append((days[t], float(res[1])))
    out = {"window": [start.isoformat(), end.isoformat()], "symbols": len(syms), "symbol_days": sym_days,
           "lag": {}, "cards": {}, "flips_per_symbol_year": {}}
    for (g, p), parts in lag_parts.items():
        out["lag"].setdefault(g, {})[f"zz{int(p * 100)}"] = gm.summarize(parts)
    years = sym_days / 252 if sym_days else 1
    for g in gates:
        out["flips_per_symbol_year"][g] = round(flips[g] / years, 2)
    for g, v in cards.items():
        rs = [x for _t, x in v["r"]]
        out["cards"][g] = {"cells": v["cells"], "filled": v["filled"],
                           "fill_rate": round(v["filled"] / v["cells"], 4) if v["cells"] else None,
                           "mean_r": round(float(np.mean(rs)), 4) if rs else None,
                           "ci_week": week_ci(v["r"])}
    spy = daily.filter((pl.col("symbol") == "SPY") & (pl.col("date") <= end))
    if spy.height:
        br = {d: above_n.get(d, 0) / n for d, n in total_n.items() if n >= 5}
        out["regime_history"], out["r_by_regime"] = {}, {}
        for md in (0, rg.MIN_DWELL_DAYS):
            rh = regime_history(spy, start, br, md)
            by_day = rh.pop("by_day")
            key = f"dwell{md}"
            out["regime_history"][key] = rh
            grp: dict = {}
            for g in ("daily_ma50", "multi_scale_v1"):
                for d, r in cards[g].get("by_day", []):
                    stt = by_day.get(d)
                    if stt is None:
                        continue
                    grp.setdefault(g, {}).setdefault(stt, []).append(r)
            out["r_by_regime"][key] = {g: {s: {"filled": len(v), "mean_r": round(float(np.mean(v)), 4)}
                                           for s, v in m.items()} for g, m in grp.items()}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
