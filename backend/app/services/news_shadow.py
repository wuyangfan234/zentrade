"""新闻催化 · 影子证据 (只观察, 不交易) — 2026-10-02 产品决策「只展示 → 只记账攒证据 → 达标后先启用利空否决，利好加权看证据」。

做两件事, 都不影响开仓:
1. 每日决策台扫描后, 给当日全部候选 (含观察卡与因额度/约束未开的) 打一个不可变快照:
   当时 48 小时内有无「高重要度利空 / 高重要度利好」主角新闻 (只用快照时刻之前已入库并打标的新闻, 无前视)。
   追加到 data/decision_desk/news_shadow.jsonl (一候选一行, 按 as_of+symbol 去重)。
2. 候选满 7 个交易日后, 按决策台统一 7 日纪律 (strategy_evaluation.simulate_7d, 与来源证据同口径)
   重放其结局 R, 回填到同一行; 按分组汇总:
     利空否决: 有高重要度利空 vs 无 → 若「有利空」组显著更差, 才说明否决有用
     利好加权: 有高重要度利好 vs 无
   启用门槛 (预先登记, 方案第五节): 每组样本 >= 30, 差值 95% 区间不跨零, 且分月方向一致。
   达不到 → 继续只观察; 达到也只「达标待你拍板」, 代码里没有任何自动启用路径。
"""
from __future__ import annotations

import json
import logging
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SOURCE_ID = "news_catalyst"
SOURCE_NAME = "新闻催化（只观察）"
MIN_N = 30
LOOKBACK_H = 48


def _path(data_dir: Path) -> Path:
    p = Path(data_dir) / "decision_desk" / "news_shadow.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_rows(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _save_rows(data_dir: Path, rows: list[dict]) -> None:
    from app.services.fs_utils import atomic_write_text
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    atomic_write_text(_path(data_dir), text + ("\n" if text else ""))


def _flags(views: list[dict], snapshot_at: datetime) -> dict:
    """快照时刻之前 48 小时内、已打标的主角新闻 (不含旧闻重炒)。"""
    from app.services.news_store import is_main
    hits = {"high_bearish": [], "high_bullish": []}
    for v in views:
        if not (v.get("tagged") and is_main(v) and v.get("importance") == "high"):
            continue
        try:
            t = datetime.fromisoformat(str(v["published_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if not (snapshot_at - timedelta(hours=LOOKBACK_H) <= t <= snapshot_at):
            continue
        if v.get("direction") == "bearish":
            hits["high_bearish"].append(v["id"])
        elif v.get("direction") == "bullish":
            hits["high_bullish"].append(v["id"])
    return hits


def record_snapshot(data_dir: Path, as_of: date, candidates: list[dict], *, now: datetime | None = None) -> int:
    """candidates: [{symbol, entry_zone, invalidation, target_zone, risk_per_share, sources, opened: bool}]。

    只追加未记录过的 (as_of, symbol)。返回新增条数。失败不抛 (不能影响决策台)。
    """
    from app.services import news_store
    now = now or datetime.now(timezone.utc)
    try:
        rows = load_rows(data_dir)
        seen = {(r["as_of"], r["symbol"]) for r in rows}
        recent = news_store.load_recent(data_dir, days=3, now=now)
        added = 0
        for c in candidates:
            sym = str(c.get("symbol") or "").upper()
            if not sym or (as_of.isoformat(), sym) in seen:
                continue
            views = news_store.for_symbol(data_dir, sym, rows=recent, now=now, limit=200)
            flags = _flags(views, now)
            rows.append({
                "as_of": as_of.isoformat(), "symbol": sym, "snapshot_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "opened": bool(c.get("opened")), "sources": sorted(c.get("sources") or []),
                "side": c.get("side", "long"),
                "entry_zone": list(c["entry_zone"]), "invalidation": float(c["invalidation"]),
                "target_zone": list(c["target_zone"]), "risk": float(c["risk_per_share"]),
                "high_bearish": bool(flags["high_bearish"]), "high_bullish": bool(flags["high_bullish"]),
                "news_ids": flags["high_bearish"] + flags["high_bullish"],
                "outcome": None,
            })
            seen.add((as_of.isoformat(), sym))
            added += 1
        if added:
            _save_rows(data_dir, rows)
        return added
    except Exception as e:  # noqa: BLE001
        logger.warning("news shadow snapshot failed (ignored): %s", type(e).__name__)
        return 0


TERMINAL_STATUSES = frozenset({"closed", "no_fill", "gap_expired"})


def resolve_outcomes(data_dir: Path, repo, *, today: date) -> int:
    """给满 7 个交易日 (+入场窗) 的快照回填统一 7 日纪律重放结局。返回本次回填条数。"""
    from app.services.strategy_evaluation import simulate_7d
    rows = load_rows(data_dir)
    # 旧版本把 open_at_end 也写成了结局 —— 视为未结算, 重新尝试
    pending = [r for r in rows if ((r.get("outcome") or {}).get("status") not in TERMINAL_STATUSES)
               and (today - date.fromisoformat(r["as_of"])).days >= 14]
    if not pending:
        return 0
    syms = sorted({r["symbol"] for r in pending})
    start = min(date.fromisoformat(r["as_of"]) for r in pending)
    bars = repo.get_daily_batch(syms, start, today, columns=["symbol", "date", "open", "high", "low", "close"])
    if bars is None or bars.is_empty():
        return 0
    by_sym = {g["symbol"][0]: g.sort("date") for g in bars.partition_by("symbol")}
    n = 0
    for r in pending:
        b = by_sym.get(r["symbol"])
        if b is None:
            continue
        after = b.filter(b["date"] > date.fromisoformat(r["as_of"]))
        if after.height < 10:
            continue
        res = simulate_7d(after["open"].to_numpy().astype(float), after["high"].to_numpy().astype(float),
                          after["low"].to_numpy().astype(float), after["close"].to_numpy().astype(float),
                          entry_zone=tuple(r["entry_zone"]), invalidation=r["invalidation"],
                          target=tuple(r["target_zone"]), risk=r["risk"], side=r.get("side", "long"))
        if res.get("status") not in TERMINAL_STATUSES:
            continue  # 数据还没走完 (open_at_end): 保持待结算, 下周再试, 不永久封账 (2026-10-05)
        r["outcome"] = {"status": res.get("status"), "r": res.get("r"), "exit_reason": res.get("exit_reason"),
                        "resolved_on": today.isoformat()}
        n += 1
    if n:
        _save_rows(data_dir, rows)
    return n


def _diff_ci(a: list[float], b: list[float], draws: int = 2000, seed: int = 11) -> tuple[float | None, float | None]:
    if len(a) < 2 or len(b) < 2:
        return None, None
    rng = random.Random(seed)
    ds = sorted(statistics.fmean(rng.choices(a, k=len(a))) - statistics.fmean(rng.choices(b, k=len(b))) for _ in range(draws))
    return round(ds[int(0.025 * draws)], 4), round(ds[int(0.975 * draws) - 1], 4)


def _compare(rows: list[dict], flag: str) -> dict:
    closed = [r for r in rows if (r.get("outcome") or {}).get("status") == "closed" and r["outcome"].get("r") is not None]
    yes = [r for r in closed if r[flag]]
    no = [r for r in closed if not r[flag]]
    ry, rn = [r["outcome"]["r"] for r in yes], [r["outcome"]["r"] for r in no]
    lo, hi = _diff_ci(ry, rn)
    months = {}
    for r in closed:
        months.setdefault(r["as_of"][:7], {"yes": [], "no": []})["yes" if r[flag] else "no"].append(r["outcome"]["r"])
    signs = [np.sign(statistics.fmean(m["yes"]) - statistics.fmean(m["no"])) for m in months.values() if m["yes"] and m["no"]]
    consistent = bool(signs) and len(set(signs)) == 1
    meets = len(ry) >= MIN_N and len(rn) >= MIN_N and lo is not None and (lo > 0 or hi < 0) and consistent
    return {"n_with": len(ry), "n_without": len(rn),
            "mean_with": round(statistics.fmean(ry), 4) if ry else None,
            "mean_without": round(statistics.fmean(rn), 4) if rn else None,
            "diff_ci": [lo, hi], "months_compared": len(signs), "months_consistent": consistent, "meets_bar": meets}


def evidence(data_dir: Path) -> dict:
    rows = load_rows(data_dir)
    resolved = [r for r in rows if (r.get("outcome") or {}).get("status") in TERMINAL_STATUSES]
    # 已批准的利空否决/利好加权是多头假设；空头单独保存和结算，不混入多头证据。
    long_rows = [r for r in rows if r.get("side", "long") == "long"]
    bearish = _compare(long_rows, "high_bearish")
    bullish = _compare(long_rows, "high_bullish")
    mw, mo = bearish["mean_with"], bearish["mean_without"]
    if bearish["meets_bar"] and mw is not None and mo is not None and mw < mo:
        verdict = "利空否决已达启用门槛（待你拍板，系统不会自动启用）"
    elif bearish["n_with"] < MIN_N:
        verdict = f"只观察：带高重要度利空的候选已结算 {bearish['n_with']} 例，未到 {MIN_N} 例门槛"
    else:
        verdict = "只观察：样本已够但差异不显著或分月方向不一致"
    return {"source": SOURCE_ID, "name": SOURCE_NAME, "status": "observe", "snapshots": len(rows),
            "resolved": len(resolved), "pending": len(rows) - len(resolved),
            "bearish_veto": bearish, "bullish_boost": bullish, "verdict": verdict,
            "rule": f"每组 ≥{MIN_N} 例 + 差值 95% 区间不跨零 + 分月方向一致；达标后仍需人工拍板",
            "since": rows[0]["as_of"] if rows else None}
