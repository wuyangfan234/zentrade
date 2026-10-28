"""前向确认 (预登记第 12 节, 2026-10-05 写定于影子账本产生任何数据之前)。

候选集合 C = 最新 v3.1 评估中“全期超额 > 0 且留出期超额 > 0”的来源 (按规则算, 不由人挑)。
前向样本 = 影子前向账本中信号日 ≥ FORWARD_START 的已结算影子卡; 超额 = R − 同日同止损随机基线均值。
晋级 (1% 风险) 须同时满足: n ≥ 300、覆盖 ≥ 6 个自然月、按月聚类单侧 p < 0.05/|C|、前向绝对 R 均值 > 0。
已晋级来源若最近 6 个月的前向超额均值 ≤ 0 → 回到只展示。规则在代码里自动执行, 不需要人工拍板。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

FORWARD_START = date(2026, 10, 6)
MIN_N = 300
MIN_MONTHS = 6
ALPHA = 0.05


def candidates(v3: dict) -> list[str]:
    out = []
    for sid, v in (v3.get("sources") or {}).items():
        if not isinstance(v, dict) or "excess" not in v:
            continue
        if ((v.get("excess") or {}).get("mean") or 0) > 0 and ((v.get("holdout") or {}).get("mean") or 0) > 0:
            out.append(sid)
    return sorted(out)


def forward_rows(data_dir: Path) -> dict[str, list[dict]]:
    """来源 → [{signal_day, r, excess}] (只含 FORWARD_START 之后、已结算、有同日同止损基线的影子卡)。"""
    from app.services import shadow_forward as sf
    rows = sf.load(data_dir)
    closed = [r for r in rows if (r.get("outcome") or {}).get("status") == "closed" and r["outcome"].get("r") is not None
              and date.fromisoformat(r["as_of"]) >= FORWARD_START]
    base: dict[tuple, list[float]] = defaultdict(list)
    for r in closed:
        if r["source"] == sf.BASELINE_ID:
            base[(r["as_of"], r.get("stop_loss"))].append(r["outcome"]["r"])
    out: dict[str, list[dict]] = defaultdict(list)
    for r in closed:
        if r["source"] == sf.BASELINE_ID:
            continue
        b = base.get((r["as_of"], r.get("stop_loss")))
        out[r["source"]].append({"signal_day": r["as_of"], "r": r["outcome"]["r"],
                                 "excess": (r["outcome"]["r"] - statistics.fmean(b)) if b else None})
    return out


def status(data_dir: Path, v3: dict) -> dict[str, dict]:
    """每个 v3 来源的前向确认状态 (候选与否、进度、是否晋级)。读失败 → 全部未晋级 (fail-closed)。"""
    from app.services import evidence_v3 as ev3
    try:
        cands = candidates(v3)
        rows = forward_rows(data_dir)
    except Exception:  # noqa: BLE001
        return {}
    m = max(len(cands), 1)
    out = {}
    for sid in (v3.get("sources") or {}):
        fr = [x for x in rows.get(sid, []) if x["excess"] is not None]
        months = sorted({x["signal_day"][:7] for x in fr})
        stats = ev3.cluster_boot(fr, "excess", unit="month") if fr else {"n": 0, "mean": None, "p": None}
        abs_mean = statistics.fmean(x["r"] for x in fr) if fr else None
        is_cand = sid in cands
        promoted = bool(is_cand and len(fr) >= MIN_N and len(months) >= MIN_MONTHS and stats.get("p") is not None
                        and stats["p"] < ALPHA / m and (abs_mean or 0) > 0)
        if promoted:
            recent_m = set(months[-6:])
            recent = [x["excess"] for x in fr if x["signal_day"][:7] in recent_m]
            if recent and statistics.fmean(recent) <= 0:
                promoted = False
        p_txt = f"{stats['p']:.3f}" if stats.get("p") is not None else "—"
        progress = f"{len(fr)}/{MIN_N} 笔、{len(months)}/{MIN_MONTHS} 个月、p={p_txt}（需 <{ALPHA / m:.4f}）"
        out[sid] = {"candidate": is_cand, "n": len(fr), "months": len(months), "excess": stats.get("mean"),
                    "abs_mean": round(abs_mean, 4) if abs_mean is not None else None, "p": stats.get("p"),
                    "threshold": round(ALPHA / m, 5), "promoted": promoted, "progress_text": progress,
                    "reason": (f"前向确认通过（信号产生时未来未知的影子样本）：{progress}，平均 {abs_mean:+.3f}R"
                               if promoted else None)}
    return out
