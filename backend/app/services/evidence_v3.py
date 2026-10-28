"""证据 v3 (2026-10-05): 修尺子。预登记见 design/evidence-v3-preregistration.md (跑数之前已提交)。

相对 v2 的四处修正:
1. 池子与生产对齐: 信号日之前 30 天日均成交额 ≥ $2000 万、收盘 ≥ $5、历史 ≥130 天、普通股/ADR (排除 ETF/杠杆/基金/优先/权证/单位)。
   不再用“成交额前 1000”。
2. 零点: 同日、同池、同一套成卡与回放规则下, 随机挑与来源当日相同数量的股票 (20 次), 得到“随机挑选”基线;
   来源的超额 R = 来源当日均值 − 同日基线均值, 按交易日聚类自助法求区间与单侧 p 值。
3. 时间切分: 发现期 2022-01-13 ~ 2024-12-31 / 留出期 2025-01-01 ~ 窗口末。
4. 多重检验: Holm–Bonferroni (m = 本轮检验的全部来源)。
另: 已退市股由研究数据集提供 (代码~退市日); 持仓期间退市 → 最后一个交易日收盘平仓 (delisted_exit)。

本模块只做计算; 许可映射在 decision_desk.source_weight 读取 v3 字段 (tier_v3) 时生效。
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np

CONTRACT_V3 = "v3.1-calibrated-2026-10-05"
# v3.1 校准 (预登记第 11/14 节, 只依据 80 次安慰剂; 2026-10-06 按真实成交价进池后重跑):
# 纯随机来源 p<0.05 的比例: 按日 3.75% / 按周 3.75% / 按月 6.25%; 选按月 (最保守地处理同月持仓重叠, 与 5% 相差在
# 80 次抽样误差 ±2.4% 内)。探索档: “p<0.10”附加条件下纯随机来源误入 8.75%, 原规则 27.5% → 探索档继续停用
# (只有一档 1% 风险的“证据为正”, 不让近乎一成的纯噪声来源拿到真钱额度)。
CLUSTER_UNIT = "month"
EXPLORE_ENABLED = False
CALIBRATION = {"placebo_runs": 80, "pool": "real traded price & dollar volume",
               "p_below_05": {"day": 0.0375, "week": 0.0375, "month": 0.0625},
               "explore_false_positive": {"week_p<0.10": 0.0875, "month_p<0.10": 0.0875, "old_rule": 0.275},
               "decision": "cluster by calendar month; exploration tier disabled",
               "script": "offline false-positive calibration"}
DISCOVERY_END = date(2024, 12, 31)
POOL_MIN_ADV = 20_000_000.0
POOL_MIN_PRICE = 5.0
POOL_MIN_HISTORY = 130
POOL_ADV_DAYS = 30
BASELINE_DRAWS = 20
BOOT_DRAWS = 2000
ALPHA = 0.05
MIN_N_PROVEN = 300
MIN_N_HOLDOUT = 100
EXCLUDED_TYPES = frozenset({"etf", "fund", "preferred", "warrant", "unit", "right"})
LEVERAGED_WORDS = ("2x", "3x", "-1x", "1.5x", "ultra", "leveraged", "inverse", "daily target", "bull ", "bear ",
                   " etf", " etn", " fund", "proshares", "direxion", "granite", "t-rex", "tradr", "defiance")


def tradable_asset_mask(symbols, instruments: dict[str, dict]) -> np.ndarray:
    """普通股/ADR 才进池: reference 标 ETF/基金/优先/权证/单位/权利, 或名称像杠杆/ETF 产品的一律排除。
    instrument_type=unknown 的按名称判断 (GOOG/TSM 等大盘股常被标 unknown, 不能整体排除)。"""
    out = np.zeros(len(symbols), dtype=bool)
    for i, s in enumerate(symbols):
        m = instruments.get(s) or {}
        t = (m.get("instrument_type") or "unknown").lower()
        if t in EXCLUDED_TYPES or m.get("is_etf") or m.get("is_preferred") or m.get("is_warrant") or m.get("is_unit"):
            continue
        name = " " + (m.get("name") or "").lower() + " "
        if any(w in name for w in LEVERAGED_WORDS):
            continue
        out[i] = True
    return out


def real_price_volume(md) -> tuple[np.ndarray, np.ndarray]:
    """进池判断要用“当时真实成交”的价格与成交额, 不能用复权价 (2026-10-06 父审实证: ACB 2023-06 真实 $0.50,
    1:10 并股后复权价 $5.04, 用复权价会被当成 ≥$5 进池)。返回 (真实收盘, 真实成交量=真实成交额/真实收盘)。
    缺 raw_close/amount 字段时退回复权价并在结果里可见 (不静默)。"""
    f = getattr(md, "fields", {}) or {}
    if "raw_close" in f and "amount" in f:
        raw = np.asarray(f["raw_close"], dtype=np.float64)
        amt = np.asarray(f["amount"], dtype=np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            vol = np.where((raw > 0) & np.isfinite(amt), amt / raw, np.nan)
        return raw, vol
    return np.asarray(md.close, dtype=np.float64), np.asarray(md.volume, dtype=np.float64)


def pool_mask(close: np.ndarray, volume: np.ndarray, asset_ok: np.ndarray) -> np.ndarray:
    """(T, N) 布尔: 第 t 天收盘后能否进池 —— 只用 t 及之前的数据 (信号在 t 收盘后产生, 与生产盘后扫描一致)。
    生产 _load_universe: 近 30 自然日均成交额 ≥ 2000 万、最后收盘 ≥ 5、历史 ≥130 根。这里 30 自然日 ≈ 21 个交易日。
    逐日滚动累加 (内存 O(N), NAS 7.5G 上不额外占用整张 T×N 浮点表)。"""
    T, N = close.shape
    w = 21
    out = np.zeros((T, N), dtype=bool)
    run_s = np.zeros(N, dtype=np.float64)
    run_c = np.zeros(N, dtype=np.int64)
    hist = np.zeros(N, dtype=np.int64)
    ring = np.zeros((w, N), dtype=np.float64)       # 最近 w 天的成交额 (缺失记 0)
    ring_f = np.zeros((w, N), dtype=bool)
    for t in range(T):
        c = np.asarray(close[t], dtype=np.float64)
        v = np.asarray(volume[t], dtype=np.float64)
        dv = c * v
        f = np.isfinite(dv)
        k = t % w
        if t >= w:
            run_s -= ring[k]
            run_c -= ring_f[k]
        ring[k] = np.where(f, dv, 0.0)
        ring_f[k] = f
        run_s += ring[k]
        run_c += f
        fc = np.isfinite(c)
        hist += fc
        with np.errstate(invalid="ignore", divide="ignore"):
            adv = np.where(run_c > 0, run_s / np.maximum(run_c, 1), np.nan)
            out[t] = (adv >= POOL_MIN_ADV) & (c >= POOL_MIN_PRICE) & (hist >= POOL_MIN_HISTORY) & fc
    return out & asset_ok[None, :]


# ── 统计 ────────────────────────────────────────────────────


def _cluster_key(day: str, unit: str):
    if unit == "day":
        return day
    d = date.fromisoformat(day)
    return d.isocalendar()[:2] if unit == "week" else (d.year, d.month)


def _day_groups(rows: list[dict], key: str = "excess", unit: str = "day") -> list[list[float]]:
    by = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[_cluster_key(r["signal_day"], unit)].append(r[key])
    return list(by.values())


def cluster_boot(rows: list[dict], key: str, draws: int = BOOT_DRAWS, seed: int = 7, unit: str | None = None) -> dict:
    """按信号所在的自然月 (v3.1 校准) 聚类自助法: 均值、95% 区间、单侧 p (H0: 均值 ≤ 0)。"""
    groups = _day_groups(rows, key, unit or CLUSTER_UNIT)
    vals = [x for g in groups for x in g]
    if not vals:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "p": None, "days": 0}
    mean = statistics.fmean(vals)
    if len(groups) < 2:
        return {"n": len(vals), "mean": round(mean, 4), "ci_low": None, "ci_high": None, "p": None, "days": len(groups)}
    rng = random.Random(seed)
    sums = [(sum(g), len(g)) for g in groups]
    means = []
    k = len(sums)
    for _ in range(draws):
        s = c = 0.0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            s += a
            c += b
        means.append(s / c)
    means.sort()
    # 单侧 p: 以 0 为界的自助分布占比 (中心化到 H0 的近似: 用“均值 ≤ 0 的重抽比例”)
    p = (sum(1 for m in means if m <= 0) + 1) / (draws + 1)
    return {"n": len(vals), "mean": round(mean, 4), "ci_low": round(means[int(0.025 * draws)], 4),
            "ci_high": round(means[int(0.975 * draws) - 1], 4), "p": round(p, 5), "days": k}


def holm(pvals: dict[str, float | None], alpha: float = ALPHA) -> dict[str, bool]:
    """Holm–Bonferroni: 按 p 从小到大, 第 k 个需 ≤ alpha/(m−k+1); 一旦失败, 其后全部不过。p 为 None 视为不过。"""
    items = sorted(((p if p is not None else 1.0), s) for s, p in pvals.items())
    m = len(items)
    out = {s: False for s in pvals}
    for k, (p, s) in enumerate(items):
        if p <= alpha / (m - k):
            out[s] = True
        else:
            break
    return out


def verdict(full_abs: dict, full_ex: dict, hold_ex: dict, holm_pass: bool) -> tuple[str, str]:
    """预登记第 7 节的许可规则。返回 (tier, 白话理由)。"""
    n = full_abs.get("n") or 0
    abs_mean = full_abs.get("mean")
    hold_n = hold_ex.get("n") or 0
    hold_mean = hold_ex.get("mean")
    ex_mean = full_ex.get("mean")
    base = (f"{n} 笔平均 {abs_mean:+.3f}R，比随机挑选{'多' if (ex_mean or 0) >= 0 else '少'} {abs(ex_mean or 0):.3f}R"
            if abs_mean is not None and ex_mean is not None else f"{n} 笔")
    hold = (f"；2025 年以来 {hold_n} 笔超额 {hold_mean:+.3f}R" if hold_mean is not None else "；2025 年以来无样本")
    if n >= MIN_N_PROVEN and holm_pass and hold_n >= MIN_N_HOLDOUT and (hold_mean or 0) > 0 and (abs_mean or 0) > 0:
        return "proven", base + hold + "；校正多重检验后仍显著：可正常开仓"
    if (EXPLORE_ENABLED and n >= MIN_N_PROVEN and (abs_mean or 0) > 0 and hold_n >= MIN_N_HOLDOUT
            and (hold_mean or 0) > 0):
        return "explore", base + hold + "；方向对但校正多重检验后不显著：探索额度（单笔 0.25%，每天最多 1 张）"
    why = []
    if n < MIN_N_PROVEN:
        why.append(f"样本 {n} 笔 <{MIN_N_PROVEN}")
    if (abs_mean or 0) <= 0:
        why.append("扣成本后本身不赚钱")
    if hold_n < MIN_N_HOLDOUT:
        why.append(f"2025 年以来样本 {hold_n} 笔 <{MIN_N_HOLDOUT}")
    elif (hold_mean or 0) <= 0:
        why.append("2025 年以来没有跑赢随机挑选")
    if not holm_pass:
        why.append("同时检验多个来源后，优势不足以排除运气")
    return "negative", base + hold + "；" + "、".join(why) + "：只展示不开仓"


@dataclass
class DayPick:
    t: int
    picks: list[int]           # 来源当天选中的资产下标 (已按评分排序截前 10)
    eligible: np.ndarray       # 当天池内资产下标 (随机基线的抽样总体)


def finalize_source(trades: list[dict], *, holdout_start: date) -> dict:
    """trades: 每笔 {signal_day, r, base_r(同日随机基线均值), excess}。"""
    ex = [t for t in trades if t.get("excess") is not None]
    full_abs = cluster_boot(trades, "r", seed=7)
    full_ex = cluster_boot(ex, "excess", seed=11)
    disc = [t for t in ex if date.fromisoformat(t["signal_day"]) < holdout_start]
    hold = [t for t in ex if date.fromisoformat(t["signal_day"]) >= holdout_start]
    by_year = defaultdict(list)
    for t in ex:
        by_year[t["signal_day"][:4]].append(t)
    years = {y: {"n": len(v), "mean_r": round(statistics.fmean(x["r"] for x in v), 4),
                 "mean_excess": round(statistics.fmean(x["excess"] for x in v), 4)} for y, v in sorted(by_year.items())}
    rs = sorted((t["r"] for t in trades), reverse=True)
    concentration = {f"drop_top_{k}": round(statistics.fmean(rs[k:]), 4) for k in (10, 20, 50) if len(rs) > k + 10}
    base = [t["base_r"] for t in ex if t.get("base_r") is not None]
    return {"n": full_abs["n"], "mean": full_abs["mean"], "ci_low": full_abs["ci_low"], "ci_high": full_abs["ci_high"],
            "median": round(statistics.median(rs), 4) if rs else None,
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 4) if rs else None,
            "baseline_mean": round(statistics.fmean(base), 4) if base else None,
            "excess": full_ex, "discovery": cluster_boot(disc, "excess", seed=13),
            "holdout": cluster_boot(hold, "excess", seed=17), "by_year": years, "concentration": concentration,
            "delisted_trades": sum(1 for t in trades if "~" in t.get("symbol", "")),
            "delisted_exits": sum(1 for t in trades if t.get("exit_reason") == "delisted_exit")}


def apply_holm(sources: dict[str, dict]) -> None:
    """在全部来源算完后调用: 写入 holm_pass / tier_v3 / reason_v3。"""
    pv = {s: (v.get("excess") or {}).get("p") for s, v in sources.items() if isinstance(v, dict) and "excess" in v}
    passed = holm(pv)
    for s, v in sources.items():
        if not isinstance(v, dict) or "excess" not in v:
            continue
        v["holm_m"] = len(pv)
        v["holm_pass"] = passed.get(s, False)
        tier, reason = verdict({"n": v["n"], "mean": v["mean"]}, v["excess"], v["holdout"], v["holm_pass"])
        v["tier_v3"], v["reason_v3"] = tier, reason
