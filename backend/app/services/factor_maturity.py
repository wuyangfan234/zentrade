"""因子成熟度 + 重复 / 垫底提示 (因子页, 2026-11-15; 判据先预注册再跑数, 预注册文本 sha256 = PREREG_SHA)。

- 每月一次, 在主题池 (周度挖掘同一池) 上算每个因子的日截面秩 IC (目标 = 未来 5 个交易日收益)。
- 月末读数: 窗口 (月末 - 365 自然日, 月末]; ICIR = |IC 均值| ÷ IC 标准差; 样本天数; 过期 = 最后有效 IC 早于月末 - 60 天。
- 状态由读数派生, 不另存可改的状态列:
  实验 = 自定义因子并入不足 180 天 (读数不显示); 观察 = 样本不足 120 天或数据过期 (不算弱) 或低于门槛未满 3 个月;
  在用 = ICIR ≥ 0.2; 退役 = 连续 3 个月末 ICIR < 0.2。
- 重复 = 最近一次周度挖掘的因子相关表里 |rho| ≥ 0.9 且对方 ICIR 更高; 垫底 = 已评估因子里 ICIR 后 20%。
- 只当标签: 不进挖掘候选池, 不改策略打分权重, 不删因子。结果写 research/factor_maturity/<月>.json。
- 重计算在 factor_lab worker 子进程里跑 (op = maturity), 交易日 09:00-17:00 ET 不跑。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "factor-maturity-v1"
PREREG_SHA = "3d2f73c9dd6772ac6094e79f3c1cfa494dc157fdcb01cb83fa761103648b77fc"
WINDOW_DAYS = 365
ICIR_FLOOR = 0.2
STREAK_MONTHS = 3
MIN_N_DAYS = 120
STALE_DAYS = 60
BLIND_DAYS = 180
DUP_RHO = 0.9
WEAK_QUANTILE = 0.2
MONTHS = 4                      # 每次算最近 4 个月末 (判退役要连续 3 个)
TARGET = "_forward_return_5d"
STATES = ("experiment", "observing", "active", "retired")


def out_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "factor_maturity"


# ── 纯函数 ─────────────────────────────────────────────────────────

def month_ends(days: list[date], n: int = MONTHS, *, month_last_trading=None) -> list[date]:
    """有 IC 的交易日 → 最近 n 个走完的月份各自最后一个有 IC 的交易日 (升序)。
    month_last_trading(某日) = 该月最后一个交易日; 最新一个月的最后 IC 日还没到它 = 月份没走完, 不算 (预注册: 只看完整月末)。"""
    last: dict[tuple[int, int], date] = {}
    for d in days:
        k = (d.year, d.month)
        if k not in last or d > last[k]:
            last[k] = d
    ends = sorted(last.values())
    if ends and month_last_trading is not None:
        try:
            if ends[-1] < month_last_trading(ends[-1]):
                ends = ends[:-1]
        except Exception:
            ends = ends[:-1]
    return ends[-n:]


def _month_last_trading(d: date) -> date:
    from app.market_clock import us_clock
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    days = us_clock.trading_days_between(d.replace(day=1), nxt - timedelta(days=1))
    return max(days) if days else nxt - timedelta(days=1)


def reading(series: list[tuple[date, float]], m: date) -> dict:
    """月末 m 的读数: {icir, ic_mean, n, last, stale}。series = [(日, IC)] 升序。"""
    lo = m - timedelta(days=WINDOW_DAYS)
    xs = [v for d, v in series if lo < d <= m and v is not None and math.isfinite(v)]
    ds = [d for d, v in series if lo < d <= m and v is not None and math.isfinite(v)]
    n = len(xs)
    last = max(ds) if ds else None
    stale = last is None or last < m - timedelta(days=STALE_DAYS)
    if n < 2:
        return {"icir": None, "ic_mean": None, "n": n, "last": last.isoformat() if last else None, "stale": stale}
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / n)
    icir = abs(mean) / sd if sd > 1e-12 else None
    return {"icir": round(icir, 4) if icir is not None else None, "ic_mean": round(mean, 4), "n": n,
            "last": last.isoformat() if last else None, "stale": stale}


def state_at(readings: list[dict], *, young: bool) -> tuple[str, int]:
    """readings = 按月末升序的读数 (最后一个 = 本月)。返回 (状态, 连续低于门槛的月数)。"""
    if young:
        return "experiment", 0
    cur = readings[-1] if readings else None
    if cur is None or cur["n"] < MIN_N_DAYS or cur["stale"] or cur["icir"] is None:
        return "observing", 0
    streak = 0
    for r in reversed(readings):
        if r["n"] >= MIN_N_DAYS and not r["stale"] and r["icir"] is not None and r["icir"] < ICIR_FLOOR:
            streak += 1
        else:
            break
    if cur["icir"] >= ICIR_FLOOR:
        return "active", 0
    return ("retired" if streak >= STREAK_MONTHS else "observing"), streak


def derive(series_by_factor: dict[str, list[tuple[date, float]]], *, months: list[date],
           young: set[str] | None = None) -> dict[str, dict]:
    """全部因子 → {因子: {state, streak, readings[{month, icir, ic_mean, n, stale}]}}。"""
    young = young or set()
    out = {}
    for f, series in series_by_factor.items():
        rs = [{"month": m.isoformat(), **reading(series, m)} for m in months]
        st, streak = state_at(rs, young=f in young)
        cur = rs[-1] if rs else {}
        out[f] = {"state": st, "streak": streak, "icir": None if st == "experiment" else cur.get("icir"),
                  "ic_mean": None if st == "experiment" else cur.get("ic_mean"), "n": cur.get("n"),
                  "readings": [] if st == "experiment" else rs}
    return out


def duplicates(corr_rows: list[dict], icir: dict[str, float | None]) -> dict[str, dict]:
    """{因子: {of, rho}}: |rho| ≥ 0.9 且对方 ICIR 更高 (比不了的不下结论)。每个因子只给最强的那个对方。"""
    best: dict[str, dict] = {}
    for r in corr_rows:
        x, y, rho = r.get("factor_x"), r.get("factor_y"), r.get("rho")
        if not x or not y or x == y or not isinstance(rho, (int, float)) or not math.isfinite(rho) or abs(rho) < DUP_RHO:
            continue
        ix, iy = icir.get(x), icir.get(y)
        if ix is None or iy is None or iy <= ix:
            continue
        cur = best.get(x)
        if cur is None or iy > (icir.get(cur["of"]) or 0):
            best[x] = {"of": y, "rho": round(float(rho), 2)}
    return best


def weak(states: dict[str, dict]) -> set[str]:
    """已评估 (在用 / 退役) 里 ICIR 后 20%。"""
    ev = sorted((v["icir"], f) for f, v in states.items() if v["state"] in ("active", "retired") and v.get("icir") is not None)
    k = int(len(ev) * WEAK_QUANTILE)
    return {f for _, f in ev[:k]}


def distribution(states: dict[str, dict]) -> dict[str, Any]:
    cnt = {s: 0 for s in STATES}
    for v in states.values():
        cnt[v["state"]] = cnt.get(v["state"], 0) + 1
    total = sum(cnt.values())
    top = max(cnt.values()) if total else 0
    return {"counts": cnt, "total": total, "max_share": round(top / total, 4) if total else None,
            "launch_ok": bool(total) and top / total < 0.95}


# ── 计算 (worker 子进程) ────────────────────────────────────────────

def _young_custom(data_dir: Path, ref: date) -> tuple[set[str], set[str]]:
    """(并入不足 180 天的自定义因子, 全部自定义因子)。"""
    from app.factors import store
    young, custom = set(), set()
    try:
        defs = store.load_all(data_dir)
    except Exception:
        return young, custom
    for d in defs:
        fid = str(d.get("id") or "")
        if not fid:
            continue
        custom.add(fid)
        raw = str(d.get("created_at") or d.get("updated_at") or "")[:10]
        try:
            if (ref - date.fromisoformat(raw)).days < BLIND_DAYS:
                young.add(fid)
        except ValueError:
            young.add(fid)                       # 读不出并入日 = 当作新因子, 不显示读数
    return young, custom


def ic_series(repo, symbols: list[str], factor_names: list[str], start: date, end: date) -> dict[str, list[tuple[date, float]]]:
    """主题池日截面秩 IC (目标 = 未来 5 个交易日收益), 与因子检验同一套面板与标签。"""
    import polars as pl

    from app.backtest.engine import BacktestEngine
    from app.backtest.factor import FactorBacktestService, FactorBatchConfig
    svc = FactorBacktestService(BacktestEngine(repo))
    cfg = FactorBatchConfig(factor_names=list(factor_names), symbols=list(symbols), start=start, end=end, rebalance="daily")
    gen = svc._data_generation("stock")
    panel = svc._load_factor_panel(cfg, list(factor_names), expected_generation=gen)
    if panel.is_empty():
        return {}
    panel = svc._attach_shared_next_return(panel, cfg, trading_dates=svc._global_trading_dates(cfg))
    if TARGET not in panel.columns:
        raise RuntimeError("forward 5d label missing")
    panel = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end) & pl.col(TARGET).is_not_null())
    out: dict[str, list[tuple[date, float]]] = {}
    for f in factor_names:
        if f not in panel.columns:
            continue
        sub = panel.select(["date", f, TARGET]).filter(pl.col(f).is_not_null() & pl.col(f).is_finite())
        if sub.is_empty():
            out[f] = []
            continue
        ic = (sub.group_by("date").agg(pl.corr(pl.col(f).rank("average"), pl.col(TARGET).rank("average")).alias("ic"))
              .sort("date"))
        out[f] = [(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), float(v))
                  for d, v in zip(ic["date"].to_list(), ic["ic"].to_list(), strict=True)
                  if v is not None and math.isfinite(v)]
    return out


def _latest_weekly_corr(data_dir: Path) -> tuple[str | None, list[dict]]:
    import polars as pl

    from app.services.factor_discover import _latest_weekly_run
    from app.services.mining_jobs import MiningRunStore
    latest = _latest_weekly_run(Path(data_dir))
    if latest is None:
        return None, []
    run_id = latest[0]
    p = MiningRunStore(data_dir).artifact_path(run_id, "correlation")
    if not p.exists():
        return run_id, []
    return run_id, pl.read_parquet(p).to_dicts()


def compute(data_dir: Path, repo, *, end: date | None = None, write: bool = True) -> dict:
    from app.factors.registry import all_factors, factor_history_available
    from app.services.mining_pool import build_mining_pool
    data_dir = Path(data_dir)
    end = end or repo.latest_enriched_date("stock")
    if end is None:
        raise RuntimeError("stock enriched data is unavailable")
    pool = list(build_mining_pool(data_dir).symbols)
    factors = [s.id for s in all_factors(asset_type="stock") if factor_history_available(s)]
    start = end - timedelta(days=WINDOW_DAYS + 31 * MONTHS)
    series = ic_series(repo, pool, factors, start, end)
    days = sorted({d for xs in series.values() for d, _ in xs})
    months = month_ends(days, month_last_trading=_month_last_trading)
    young, custom = _young_custom(data_dir, end)
    states = derive(series, months=months, young=young)
    icir = {f: v.get("icir") for f, v in states.items()}
    run_id, corr = _latest_weekly_corr(data_dir)
    dup = duplicates(corr, icir)
    wk = weak(states)
    for f, v in states.items():
        v["custom"] = f in custom
        if f in dup:
            v["dup"] = dup[f]
        if f in wk:
            v["weak"] = True
    out = {"contract": CONTRACT, "prereg_sha256": PREREG_SHA, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "data_end": end.isoformat(), "months": [m.isoformat() for m in months], "pool": len(pool),
           "pool_source": "mining_pool", "target": "5d", "corr_run": run_id,
           "params": {"window_days": WINDOW_DAYS, "icir_floor": ICIR_FLOOR, "streak_months": STREAK_MONTHS,
                      "min_n_days": MIN_N_DAYS, "stale_days": STALE_DAYS, "blind_days": BLIND_DAYS, "dup_rho": DUP_RHO,
                      "weak_quantile": WEAK_QUANTILE},
           "distribution": distribution(states), "factors": states}
    if write and months:
        from app.services.fs_utils import atomic_write_text
        d = out_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / f"{months[-1].strftime('%Y-%m')}.json", json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return out


def latest(data_dir: Path) -> dict | None:
    """最近一份月度结果 (只读); 没有或读坏 = None。"""
    d = out_dir(data_dir)
    files = sorted(d.glob("20*.json")) if d.exists() else []
    for f in reversed(files):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(v, dict) and v.get("contract") == CONTRACT:
            return v
    return None


def due(data_dir: Path, today: date) -> bool:
    """本月还没有上个月末的结果 → 该跑 (每月第一个周六补)。"""
    cur = latest(data_dir)
    if not cur or not cur.get("months"):
        return True
    last_month = date.fromisoformat(cur["months"][-1])
    first = today.replace(day=1)
    prev_end = first - timedelta(days=1)
    return (last_month.year, last_month.month) < (prev_end.year, prev_end.month)


def library_view(data_dir: Path) -> dict:
    """因子库用: {因子: {state, icir, n, streak, dup, weak}} + 月份; 模块未上线 (预注册不过) → launched=False, 不给状态。"""
    cur = latest(data_dir)
    if not cur:
        return {"launched": False, "month": None, "factors": {}}
    dist = cur.get("distribution") or {}
    launched = bool(dist.get("launch_ok"))
    keep = ("state", "icir", "n", "streak", "dup", "weak")
    facs = {f: {k: v.get(k) for k in keep if k in v} for f, v in (cur.get("factors") or {}).items()}
    if not launched:
        facs = {f: {k: v[k] for k in ("dup",) if k in v} for f, v in facs.items()}
    return {"launched": launched, "month": (cur.get("months") or [None])[-1], "generated_at": cur.get("generated_at"),
            "pool": cur.get("pool"), "distribution": dist, "factors": facs}


def run_scheduled(data_dir: Path, repo, *, force: bool = False) -> dict:
    """每月一次 (周六 11:30 ET 检查; 本月还没有上月末结果才跑)。在 factor_lab worker 子进程里算, 占一个普通重任务额度;
    交易日 09:00-17:00 ET 不跑。失败只记日志, 页面继续显示上一份读数并标月份。"""
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.market_clock import us_clock
    from app.services.factor_discover import can_run_heavy
    from app.services.heavy_job_limiter import shared_heavy_job_limiter
    today = us_clock.market_datetime(us_clock.now()).date()
    if not force and not due(Path(data_dir), today):
        return {"skipped": "not_due"}
    if not can_run_heavy():
        return {"skipped": "session"}
    with shared_heavy_job_limiter.slot("normal"):
        return run_worker_task(make_worker_task("factor_lab", Path(data_dir), {"op": "maturity"}))
