"""挖掘试验台账与冷却 (因子页, 2026-11-15; 判据先预注册再跑数)。

- 从挖掘运行的候选表派生 (research/mining/runs/<run>/candidates.parquet), 不另存一份可改的台账:
  每次成功的挖掘运行 (周度或手动) 里出现一次 = 一次试验。
- 结构签名 = 候选里的因子 + 方向, 排序后拼接 (例 annual_vol_20d:low)。
- 冷却: 近 90 天内在周度运行里作为候选出现 >= 3 次 → 冷却到“最后一次出现 + 90 天”; 冷却期内再出现只计次数, 不延长;
  到期自动解除 (没有要人恢复的状态)。
- 只影响发现页展示: 冷却中的结构不再当作本周新候选回测, 单列「冷却至 MM-DD · 已试 N 次」。挖掘本身照跑, 不剔除因子,
  不改 3+3、策略文件与任何打分权重。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "trial-ledger-v1"
COOLDOWN_DAYS = 90
MIN_WEEKLY_REPEATS = 3
ENFORCED_FROM = date(2026, 11, 15)      # 冷却从上线起才生效: 之前的运行没有被冷却挡过, 照常计入“最后一次出现”
OK_STATUSES = ("succeeded", "succeeded_with_budget_exhausted")
RUNS_LIMIT = 200                        # MiningRunStore.list_runs 的上限 (超过直接报错, 11-16 线上实测)
_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def structure_key(scoring: dict | None, directions: dict | None) -> str | None:
    """因子 + 方向 → 签名; 读不出 = None。方向 low / high (挖掘写 -1 / 1 时换算)。"""
    if not isinstance(scoring, dict) or not scoring:
        return None
    parts = []
    for f in sorted(str(k) for k in scoring):
        d = (directions or {}).get(f)
        d = {"-1": "low", "1": "high"}.get(str(d), str(d) if d else "high")
        parts.append(f"{f}:{d}")
    return "|".join(parts)


def _run_day(manifest: dict) -> date | None:
    from app.market_clock import us_clock
    raw = str(manifest.get("created_at") or manifest.get("started_at") or "")
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return t.date()
    return t.astimezone(us_clock.tz).date()


def collect(data_dir: Path) -> list[dict]:
    """全部成功挖掘运行里的候选 → [{run_id, weekly, day, key, oos_sharpe, signature}] (按日期、运行升序)。
    按运行目录签名缓存。"""
    import polars as pl

    from app.services.mining_jobs import MiningRunStore
    store = MiningRunStore(Path(data_dir))
    runs = [r for r in store.list_runs(limit=RUNS_LIMIT) if r.get("status") in OK_STATUSES]
    sig_parts = []
    for r in runs:
        p = store.artifact_path(str(r["run_id"]), "candidates")
        try:
            st = p.stat()
            sig_parts.append((str(r["run_id"]), st.st_mtime_ns, st.st_size))
        except OSError:
            sig_parts.append((str(r["run_id"]), 0, 0))
    sig = tuple(sorted(sig_parts))
    hit = _CACHE.get(str(data_dir))
    if hit and hit[0] == sig:
        return hit[1]
    out: list[dict] = []
    for r in runs:
        run_id = str(r["run_id"])
        day = _run_day(r)
        p = store.artifact_path(run_id, "candidates")
        if day is None or not p.exists():
            continue
        try:
            rows = pl.read_parquet(p).to_dicts()
        except Exception as e:
            logger.warning("trial ledger: candidates unreadable %s: %s", run_id, type(e).__name__)
            continue
        for row in rows:
            try:
                d = json.loads(row.get("definition_json") or "{}")
            except ValueError:
                continue
            key = structure_key(d.get("scoring"), d.get("directions"))
            if key is None:
                continue
            out.append({"run_id": run_id, "weekly": run_id.startswith("weekly-"), "day": day.isoformat(), "key": key,
                        "oos_sharpe": row.get("oos_sharpe"), "signature": row.get("signature")})
    out.sort(key=lambda t: (t["day"], t["run_id"]))
    if len(_CACHE) > 4:
        _CACHE.clear()
    _CACHE[str(data_dir)] = (sig, out)
    return out


def summarize(trials: list[dict], today: date, *, exclude_run: str | None = None) -> dict[str, dict]:
    """{签名: {n, weekly_n, sharpes, runs, last, cooldown_until, cooling}}。exclude_run = 判冷却时不算这一轮 (本周正在评的那一轮)。"""
    by: dict[str, list[dict]] = {}
    for t in trials:
        by.setdefault(t["key"], []).append(t)
    out = {}
    for key, xs in by.items():
        until: date | None = None
        window: list[date] = []
        for t in xs:
            d = date.fromisoformat(t["day"])
            if t["run_id"] == exclude_run:
                continue
            if until is not None and d < until and d >= ENFORCED_FROM:
                continue                          # 冷却期内再出现: 只计次数, 不延长
            if not t["weekly"]:
                continue
            window = [x for x in window if (d - x).days < COOLDOWN_DAYS] + [d]
            if len(window) >= MIN_WEEKLY_REPEATS:
                until = d + timedelta(days=COOLDOWN_DAYS)
        out[key] = {"n": len(xs), "weekly_n": sum(1 for t in xs if t["weekly"]),
                    "sharpes": [round(float(t["oos_sharpe"]), 2) for t in xs if t["weekly"] and isinstance(t.get("oos_sharpe"), (int, float))],
                    "runs": [t["run_id"] for t in xs], "first": xs[0]["day"], "last": xs[-1]["day"],
                    "cooldown_until": until.isoformat() if until else None, "cooling": bool(until and today < until)}
    return out


def view(data_dir: Path, today: date, *, exclude_run: str | None = None) -> dict[str, dict]:
    try:
        return summarize(collect(data_dir), today, exclude_run=exclude_run)
    except Exception as e:  # 台账读不出: 发现页照常, 只是不带“已试 N 次”
        logger.warning("trial ledger unavailable: %s", type(e).__name__)
        return {}
