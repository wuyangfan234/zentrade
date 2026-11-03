"""Weekly discovery (0.47.0): after the weekly mining run, backtest each candidate that passed the
out-of-sample gate on the watchlist (default 2023-01-03 -> latest close) next to the "current card
engine" (the long rules in rules/swing.json, equal capital, run independently, NAV averaged), and
write research/discover/<week>.json for the factor page "Discover" home tab.

Never publishes. Publishing stays a human click in the candidates drawer; 3+3 is never touched.

Checks are display-only (approved 2026-11-02: no "must beat the engine" gate):
  trades >= 30 · OOS Sharpe >= 0.5 · 2023-2025 each year positive · avg hold <= 7 days ·
  max drawdown <= 25% · corr with the current engine < 0.7 · corr with SPY < 0.7
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CONTRACT = "factor-discover-v1"
DEFAULT_START = date(2023, 1, 3)
CURVE_POINTS = 41
CHECK_LIMITS = {"trades": 30, "oos_sharpe": 0.5, "hold_days": 7.0, "mdd": -0.25, "corr": 0.7}
# Same template the miner evaluates and the publisher writes (mining_runtime._backtest_config / mined_factor_* META):
# score >= 70 enters, <= 40 exits, at most 20 names a day. The Discover page shows the strategy that would be published.
RANK_PARAMS = {"entry_score": 70.0, "exit_score": 40.0, "top_rank": 20}
_RUN_LOCK = threading.Lock()
_DECISIONS_LOCK = threading.Lock()  # decisions.json read-modify-write (two quick clicks must both land)
# Weekly discover job time (jobs/daily_pipeline.py zen_factor_discover); the page shows the next run from it.
DISCOVER_RUN_ET = (17, 40)


# ---------------------------------------------------------------- helpers
def _dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "discover"


def can_run_heavy(now: datetime | None = None) -> bool:
    """Off-hours only: trading day 09:00-17:00 ET the enriched data is republished every ~10 s."""
    from app.services.swing_candidates import _busy
    return not _busy(now, (17, 0))


def watchlist_symbols() -> list[str]:
    from app.services import watchlist
    try:
        return [r["symbol"] for r in watchlist.list_symbols() if r.get("symbol")]
    except Exception:  # unreadable watchlist: no watchlist backtest
        return []


def engine_rules(data_dir: Path) -> list[str]:
    """Long rules of the current card engine (rules/swing.json), read at run time."""
    try:
        raw = json.loads((Path(data_dir) / "rules" / "swing.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    longs = raw.get("long") if isinstance(raw, dict) else None
    return [str(x) for x in longs if isinstance(x, str)][:6] if isinstance(longs, list) else []


def _service(repo, data_dir: Path):
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.backtest.worker import _strategy_dirs
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine

    se = StrategyEngine(strategy_dirs=_strategy_dirs(Path(data_dir)),
                        override_loader=lambda sid: strategy_config.load_override(Path(data_dir), sid))
    se.reload()
    return StrategyBacktestService(BacktestEngine(repo), se)


def _config(strategy_id: str, symbols: list[str], start: date, end: date, params: dict | None):
    from app.backtest.strategy import StrategyBacktestConfig
    return StrategyBacktestConfig(
        strategy_id=strategy_id, symbols=symbols, start=start, end=end, params=params, overrides=None,
        matching="open_t+1", entry_fill=None, exit_fill=None, fees_pct=None, cost_model="us_per_share_v1",
        commission_per_share=0.005, commission_min=1.0, commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=10, max_exposure_pct=1.0, initial_capital=1_000_000.0, position_sizing="equal", mode="position",
        holding_days=5, asset_type="stock", minute_fill=False, regime_filter=None,
    )


def _run(service, strategy_id: str, symbols: list[str], start: date, end: date, params: dict | None = None) -> dict:
    result = asdict(service.run(_config(strategy_id, symbols, start, end, params), None, None))
    if result.get("error"):
        raise ValueError(str(result["error"]))
    return result


def _series(result: dict, key: str = "equity_curve") -> dict[str, float]:
    out = {}
    for p in result.get(key) or []:
        d = str(p.get("date") or "")[:10]
        v = p.get("value", p.get("equity"))
        if d and isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            out[d] = float(v)
    return out


def _norm(s: dict[str, float], dates: list[str]) -> list[float]:
    v0 = s[dates[0]]
    return [s[d] / v0 for d in dates]


def _rets(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values))]


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 30:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False)) / math.sqrt(va * vb), 3)


def _stats_from_nav(dates: list[str], nav: list[float]) -> dict:
    r = _rets(nav)
    n = len(r)
    years = max(n / 252, 1e-9)
    total = nav[-1] / nav[0] - 1
    ann = (nav[-1] / nav[0]) ** (1 / years) - 1 if nav[-1] > 0 else -1.0
    mu = sum(r) / n if n else 0.0
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    sharpe = mu / sd * math.sqrt(252) if sd > 0 else None
    peak, mdd = nav[0], 0.0
    for x in nav:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return {"total_return": round(total, 4), "annual_return": round(ann, 4), "sharpe": None if sharpe is None else round(sharpe, 2),
            "max_drawdown": round(mdd, 4), "calmar": round(ann / -mdd, 2) if mdd < 0 else None}


def _yearly(dates: list[str], nav: list[float]) -> dict[str, float]:
    out: dict[str, float] = {}
    prev = nav[0]
    for y in sorted({d[:4] for d in dates}):
        idx = [i for i, d in enumerate(dates) if d.startswith(y)]
        if not idx:
            continue
        out[y] = round(nav[idx[-1]] / prev - 1, 4)
        prev = nav[idx[-1]]
    return out


def _sample(dates: list[str], n: int = CURVE_POINTS) -> list[int]:
    if len(dates) <= n:
        return list(range(len(dates)))
    step = max(1, len(dates) // (n - 1))
    idx = list(range(0, len(dates), step))
    if idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    return idx


def _gross_profit_factor(trades: list[dict]) -> float | None:
    """Gross profit / gross loss on pnl_amount (the engine's profit_factor field is avg win / avg loss)."""
    gw = sum(float(t["pnl_amount"]) for t in trades if isinstance(t.get("pnl_amount"), (int, float)) and t["pnl_amount"] > 0)
    gl = -sum(float(t["pnl_amount"]) for t in trades if isinstance(t.get("pnl_amount"), (int, float)) and t["pnl_amount"] < 0)
    return round(gw / gl, 2) if gl > 0 else None


def _last_trades(trades: list[dict], k: int = 4) -> list[dict]:
    rows = sorted((t for t in trades if t.get("exit_date")), key=lambda t: (str(t.get("exit_date")), str(t.get("entry_date"))), reverse=True)[:k]
    return [{"symbol": t.get("symbol"), "entry": str(t.get("entry_date"))[:10], "exit": str(t.get("exit_date"))[:10],
             "pnl_pct": t.get("pnl_pct")} for t in rows]


def _summary(result: dict) -> dict:
    st = result.get("stats") or {}
    trades = result.get("trades") or []
    return {k: st.get(k) for k in ("total_return", "annual_return", "sharpe", "calmar", "max_drawdown", "n_trades", "win_rate",
                                    "avg_holding_days", "avg_win", "avg_loss", "avg_exposure")} | {"profit_factor": _gross_profit_factor(trades)}


# ---------------------------------------------------------------- public: one scoring backtest (intake reuses it)
def backtest_scoring(repo, scoring: dict[str, float], directions: dict[str, str], symbols: list[str], start: date, end: date,
                     *, top_rank: int = RANK_PARAMS["top_rank"], data_dir: Path | None = None) -> dict:
    data_dir = Path(data_dir or repo.store.data_dir)
    service = _service(repo, data_dir)
    params = {"scoring": scoring, "directions": directions, **RANK_PARAMS, "top_rank": int(top_rank)}
    res = _run(service, "factor_rank_research", symbols, start, end, params)
    nav = _series(res)
    dates = sorted(nav)
    out = _summary(res)
    if dates:
        v = _norm(nav, dates)
        out["yearly"] = _yearly(dates, v)
        idx = _sample(dates)
        out["curve"] = {"dates": [dates[i] for i in idx], "values": [round(v[i], 4) for i in idx]}
    out["last_trades"] = _last_trades(res.get("trades") or [])
    return out


# ---------------------------------------------------------------- engine reference
def engine_reference(service, data_dir: Path, symbols: list[str], start: date, end: date) -> dict | None:
    rules = engine_rules(data_dir)
    if not rules:
        return None
    legs, navs, trades, spy = [], [], [], None
    for rid in rules:
        try:
            res = _run(service, rid, symbols, start, end)
        except Exception as exc:  # a missing rule just drops out of the reference
            logger.warning("discover: engine leg %s failed: %s", rid, type(exc).__name__)
            continue
        s = _series(res)
        if not s:
            continue
        name = service.strategy_engine.get(rid).meta.get("name", rid) if service.strategy_engine.has(rid) else rid
        legs.append({"id": rid, "name": name, **{k: (res.get("stats") or {}).get(k)
                                                 for k in ("annual_return", "sharpe", "max_drawdown", "n_trades", "avg_holding_days", "avg_exposure")}})
        navs.append(s)
        trades.extend(res.get("trades") or [])
        spy = spy or _series(res, "benchmark_curve")
    if not navs:
        return None
    dates = sorted(set.intersection(*[set(s) for s in navs]))
    if len(dates) < 30:
        return None
    combo = [sum(_norm(s, dates)[i] for s in navs) / len(navs) for i in range(len(dates))]
    stats = _stats_from_nav(dates, combo)
    n_tr = sum(int(leg.get("n_trades") or 0) for leg in legs)
    pnl = [t for t in trades if isinstance(t.get("pnl_pct"), (int, float))]
    wins = [t["pnl_pct"] for t in pnl if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in pnl if t["pnl_pct"] <= 0]
    hold_w = [(int(leg.get("n_trades") or 0), leg.get("avg_holding_days")) for leg in legs]
    hold_n = sum(n for n, h in hold_w if isinstance(h, (int, float)))
    expo = [leg.get("avg_exposure") for leg in legs if isinstance(leg.get("avg_exposure"), (int, float))]
    out = {
        "rules": rules, "legs": legs, "dates": dates, "nav": combo,
        "stats": {**stats, "n_trades": n_tr, "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
                  "avg_holding_days": round(sum(n * h for n, h in hold_w if isinstance(h, (int, float))) / hold_n, 1) if hold_n else None,
                  "avg_exposure": round(sum(expo) / len(expo), 4) if expo else None,
                  "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
                  "avg_loss": round(abs(sum(losses) / len(losses)), 4) if losses else None,
                  "profit_factor": _gross_profit_factor(trades)},
        "yearly": _yearly(dates, combo), "last_trades": _last_trades(trades),
    }
    if spy:
        common = [d for d in dates if d in spy]
        if len(common) >= 30:
            out["spy"] = {"dates": common, "nav": _norm(spy, common), "stats": _stats_from_nav(common, _norm(spy, common)),
                          "yearly": _yearly(common, _norm(spy, common))}
    return out


# ---------------------------------------------------------------- weekly run
def _latest_weekly_run(data_dir: Path) -> tuple[str, dict, dict] | None:
    from app.services.mining_jobs import MiningRunStore

    store = MiningRunStore(data_dir)
    runs = [r for r in store.list_runs(limit=40) if str(r.get("run_id", "")).startswith("weekly-")
            and r.get("status") in ("succeeded", "succeeded_with_budget_exhausted")]
    if not runs:
        return None
    runs.sort(key=lambda r: str(r.get("run_id")))
    m = runs[-1]
    return str(m["run_id"]), m, store.read_summary(str(m["run_id"]))


def _candidates(data_dir: Path, run_id: str) -> list[dict]:
    import polars as pl

    from app.backtest.mining import evaluate_candidate_gate
    from app.services.mining_jobs import MiningRunStore

    store = MiningRunStore(data_dir)
    path = store.artifact_path(run_id, "candidates")
    if not path.exists():
        return []
    frame = pl.read_parquet(path)
    out = []
    for row in frame.to_dicts():
        try:
            definition = json.loads(row.get("definition_json") or "{}")
        except json.JSONDecodeError:
            continue
        if definition.get("kind") != "factor_rank":
            continue
        gate = evaluate_candidate_gate(confidence=row.get("confidence"), valid_folds=row.get("valid_folds"),
                                       positive_fold_ratio=row.get("oos_positive_fold_ratio"), sharpe=row.get("oos_sharpe"),
                                       max_drawdown=row.get("oos_max_drawdown"), n_trades=row.get("oos_n_trades"))
        out.append({"signature": row.get("signature"), "definition": definition, "oos_sharpe": row.get("oos_sharpe"),
                    "oos_return": row.get("oos_return"), "oos_max_drawdown": row.get("oos_max_drawdown"),
                    "oos_n_trades": row.get("oos_n_trades"), "valid_folds": row.get("valid_folds"),
                    "passed_oos": gate.qualified or (row.get("oos_sharpe") or 0) >= CHECK_LIMITS["oos_sharpe"],
                    "published_strategy_id": row.get("published_strategy_id")})
    return out


def _checks(c: dict, corr_engine: float | None, corr_spy: float | None, oos_sharpe) -> list[list]:
    yr = c.get("yearly") or {}
    full_years = [y for y in ("2023", "2024", "2025") if y in yr]
    pos = sum(1 for y in full_years if (yr.get(y) or 0) > 0)
    hold = c.get("avg_holding_days")
    mdd = c.get("max_drawdown")
    return [
        [int((c.get("n_trades") or 0) >= CHECK_LIMITS["trades"]), "trades", c.get("n_trades")],
        [int(oos_sharpe is not None and oos_sharpe >= CHECK_LIMITS["oos_sharpe"]), "oos_sharpe", oos_sharpe],
        [int(bool(full_years) and pos == len(full_years)), "years_positive", f"{pos} / {len(full_years)}"],
        [int(hold is not None and hold <= CHECK_LIMITS["hold_days"]), "hold_days", hold],
        [int(mdd is not None and mdd >= CHECK_LIMITS["mdd"]), "max_drawdown", mdd],
        [int(corr_engine is not None and corr_engine < CHECK_LIMITS["corr"]), "corr_engine", corr_engine],
        [int(corr_spy is not None and corr_spy < CHECK_LIMITS["corr"]), "corr_spy", corr_spy],
    ]


def _label(get_factor, fid: str) -> str:
    spec = get_factor(fid)
    return spec.label if spec is not None else fid


def run_weekly(data_dir: Path, repo, *, start: date | None = None, end: date | None = None, symbols: list[str] | None = None,
               now: datetime | None = None) -> dict:
    """Backtest this week's OOS-passing candidates on the watchlist next to the current card engine.

    Runs in a spawned worker (same isolation as API backtests); the parent holds one normal heavy-job slot."""
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    if not can_run_heavy(now):
        return {"skipped": "session"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "running"}
    try:
        cfg = {"op": "discover", "symbols": symbols or watchlist_symbols(),
               "start": (start or DEFAULT_START).isoformat(), "end": end.isoformat() if end else None}
        with shared_heavy_job_limiter.slot("normal"):
            return run_worker_task(make_worker_task("factor_lab", Path(data_dir), cfg))
    finally:
        _RUN_LOCK.release()


def worker_run(data_dir: Path, repo, cfg: dict) -> dict:
    """Worker entry (backtest/worker.py kind=factor_lab)."""
    op = cfg.get("op")
    if op == "discover":
        return _run_weekly_impl(data_dir, repo, symbols=list(cfg.get("symbols") or []),
                                start=date.fromisoformat(cfg["start"]),
                                end=date.fromisoformat(cfg["end"]) if cfg.get("end") else None)
    if op == "recalc":
        return _recalc_impl(data_dir, repo, cfg["signature"], symbols=list(cfg["symbols"]),
                            start=date.fromisoformat(cfg["start"]), end=date.fromisoformat(cfg["end"]))
    if op == "screen":
        from app.services import factor_intake
        return factor_intake.screen_impl(data_dir, repo, cfg)
    raise ValueError(f"unsupported factor_lab op: {op!r}")


def _run_weekly_impl(data_dir: Path, repo, *, symbols: list[str], start: date, end: date | None) -> dict:
    from app.factors.registry import get_factor

    latest = _latest_weekly_run(Path(data_dir))
    if latest is None:
        return {"skipped": "no_weekly_run"}
    run_id, manifest, summary = latest
    if not symbols:
        return {"skipped": "empty_watchlist"}
    end = end or repo.latest_enriched_date("stock") or date.today()
    service = _service(repo, Path(data_dir))
    ref = engine_reference(service, Path(data_dir), symbols, start, end)
    ref_dates = set(ref["dates"]) if ref else set()
    ref_pos = {x: i for i, x in enumerate(ref["dates"])} if ref else {}
    spy_pos = {x: i for i, x in enumerate(ref["spy"]["dates"])} if ref and ref.get("spy") else {}
    cands = []
    for c in _candidates(Path(data_dir), run_id):
        if not c["passed_oos"]:
            continue
        d = c["definition"]
        scoring = {str(k): float(v) for k, v in (d.get("scoring") or {}).items()}
        directions = {str(k): str(v) for k, v in (d.get("directions") or {}).items()}
        try:
            res = _run(service, "factor_rank_research", symbols, start, end,
                       {"scoring": scoring, "directions": directions, **RANK_PARAMS})
        except Exception as exc:  # one candidate failing never stops the week
            cands.append({"signature": c["signature"], "error": type(exc).__name__})
            continue
        nav = _series(res)
        dates = sorted(nav)
        if len(dates) < 30:
            continue
        stats = _summary(res)
        stats["yearly"] = _yearly(dates, _norm(nav, dates))
        ce = cs = None
        if ref:
            common = [x for x in dates if x in ref_dates]
            ce = _corr(_rets([nav[x] for x in common]), _rets([ref["nav"][ref_pos[x]] for x in common]))
            if spy_pos:
                c2 = [x for x in dates if x in spy_pos]
                cs = _corr(_rets([nav[x] for x in c2]), _rets([ref["spy"]["nav"][spy_pos[x]] for x in c2]))
        legs = [{"factor": k, "label": _label(get_factor, k), "direction": directions.get(k), "weight": w}
                for k, w in scoring.items()]
        cands.append({
            "signature": c["signature"], "legs": legs, "scoring": scoring, "directions": directions,
            "name": " + ".join(leg["label"] + ("" if len(legs) > 1 else (" 低" if leg["direction"] == "low" else " 高")) for leg in legs),
            "oos": {k: c[k] for k in ("oos_sharpe", "oos_return", "oos_max_drawdown", "oos_n_trades", "valid_folds")},
            "stats": stats, "corr_engine": ce, "corr_spy": cs,
            "checks": _checks(stats, ce, cs, c.get("oos_sharpe")),
            "nav": {x: round(nav[x] / nav[dates[0]], 4) for x in dates},
            "last_trades": _last_trades(res.get("trades") or []), "published_strategy_id": c.get("published_strategy_id"),
        })
    out = _assemble(run_id, manifest, summary, symbols, start, end, ref, cands)
    week = run_id.removeprefix("weekly-")
    _dir(Path(data_dir)).mkdir(parents=True, exist_ok=True)
    atomic_write_text(_dir(Path(data_dir)) / f"{week}.json", json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return {"week": week, "candidates": len([c for c in cands if not c.get("error")])}


def _assemble(run_id, manifest, summary, symbols, start, end, ref, cands) -> dict:
    dates = list(ref["dates"]) if ref else sorted({d for c in cands if not c.get("error") for d in c["nav"]})
    idx = _sample(dates)
    sampled = [dates[i] for i in idx]
    curves: dict[str, list] = {"dates": sampled}
    if ref:
        curves["engine"] = [round(ref["nav"][i], 4) for i in idx]
        if ref.get("spy"):
            sp = {d: v for d, v in zip(ref["spy"]["dates"], ref["spy"]["nav"], strict=False)}
            curves["spy"] = [round(sp[d], 4) if d in sp else None for d in sampled]
    for c in cands:
        if c.get("error"):
            continue
        nav = c.pop("nav")
        curves[c["signature"]] = [nav.get(d) for d in sampled]
    req = manifest.get("request") or {}
    ref_out = None
    if ref:
        ref_out = {k: ref[k] for k in ("rules", "legs", "stats", "yearly", "last_trades")}
        ref_out["corr_spy"] = None
        if ref.get("spy"):
            sp = {d: v for d, v in zip(ref["spy"]["dates"], ref["spy"]["nav"], strict=False)}
            common = [d for d in ref["dates"] if d in sp]
            pos = {d: i for i, d in enumerate(ref["dates"])}
            ref_out["corr_spy"] = _corr(_rets([ref["nav"][pos[d]] for d in common]), _rets([sp[d] for d in common]))
        ref_out["checks"] = _checks({**ref["stats"], "yearly": ref["yearly"]}, None, ref_out["corr_spy"], None)
        ref_out["checks"] = [c for c in ref_out["checks"] if c[1] not in ("oos_sharpe", "corr_engine")]
    return {
        "contract": CONTRACT, "week": run_id.removeprefix("weekly-"), "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_as_of": summary.get("data_as_of"), "window": [str(start), str(end)], "symbols": len(symbols),
        "funnel": {"factors_selected": summary.get("selected_factor_count"), "factors_total": summary.get("factor_count"),
                   "combos": summary.get("proxy_trials_used"), "valid_folds": summary.get("valid_fold_count"),
                   "skipped_folds": summary.get("skipped_fold_count"), "pool": len(req.get("symbols") or []),
                   "passed": len([c for c in cands if not c.get("error")])},
        "engine": ref_out, "spy": ({"stats": ref["spy"]["stats"], "yearly": ref["spy"]["yearly"]} if ref and ref.get("spy") else None),
        "candidates": cands, "curves": curves,
    }


# ---------------------------------------------------------------- read model
def load_week(data_dir: Path, week: str | None = None) -> dict | None:
    d = _dir(Path(data_dir))
    files = sorted(d.glob("*.json")) if d.exists() else []
    files = [f for f in files if not f.name.startswith(".")]
    if not files:
        return None
    path = d / f"{week}.json" if week else files[-1]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def weeks(data_dir: Path, n: int = 4) -> list[dict]:
    d = _dir(Path(data_dir))
    out = []
    for f in sorted((d.glob("*.json") if d.exists() else []), reverse=True)[:n]:
        if f.name.startswith("."):
            continue
        try:
            w = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"week": w.get("week"), "data_as_of": w.get("data_as_of"), "funnel": w.get("funnel"),
                    "candidates": len(w.get("candidates") or [])})
    return out


def page(data_dir: Path) -> dict:
    """Discover page aggregate: one section failing never blanks the page."""
    from app.services import factor_intake

    errors: dict[str, str] = {}

    def section(name, fn):
        try:
            return fn()
        except Exception as exc:
            logger.warning("discover page: %s failed: %s", name, type(exc).__name__)
            errors[name] = type(exc).__name__
            return None

    week = section("week", lambda: load_week(data_dir))
    hist = section("weeks", lambda: weeks(data_dir))
    intake = section("intake", lambda: factor_intake.overview(data_dir))
    decisions = section("decisions", lambda: _decisions(data_dir))
    sched = section("schedule", _schedule)
    return {"contract": CONTRACT, "week": week, "weeks": hist or [], "intake": intake, "decisions": decisions or {},
            "schedule": sched or {}, "running": _RUN_LOCK.locked(), "errors": errors}


def _decisions_path(data_dir: Path) -> Path:
    return _dir(Path(data_dir)) / "decisions" / "decisions.json"


def _decisions(data_dir: Path) -> dict:
    try:
        return json.loads(_decisions_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def decide(data_dir: Path, week: str, signature: str, action: str, *, promoter=None) -> dict:
    """Save / discard a discovered candidate. Save = a pending research candidate (candidates drawer); never publishes.

    promoter: callable(run_id, signature) -> dict (the app's MiningCandidateService.promote); injected by the API."""
    if action not in ("save", "discard", "reset"):
        raise ValueError("action 只能是 save / discard / reset")
    w = load_week(data_dir, week)
    if w is None:
        raise KeyError(week)
    cand = next((c for c in w.get("candidates") or [] if c.get("signature") == signature), None)
    if cand is None:
        raise KeyError(signature)
    out: dict[str, Any] = {"week": week, "signature": signature, "action": action}
    if action == "save" and promoter is not None:
        try:
            item = promoter(w["run_id"], signature)
            out["candidate_id"] = item.get("id") if isinstance(item, dict) else None
        except Exception as exc:  # not promotable (e.g. origin artifact gone): record the decision only
            out["candidate_error"] = type(exc).__name__
    path = _decisions_path(data_dir)
    key = f"{week}|{signature}"
    with _DECISIONS_LOCK:
        cur = _decisions(data_dir)
        if action == "reset":
            cur.pop(key, None)
        else:
            cur[key] = {"action": action, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        **({"candidate_id": out.get("candidate_id")} if out.get("candidate_id") else {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(cur, ensure_ascii=False, indent=1))
    return out


def _schedule() -> dict:
    """Next weekly discover run: the first trading Friday whose DISCOVER_RUN_ET has not passed yet."""
    from app.market_clock import us_clock
    now = us_clock.market_datetime(us_clock.now())
    today = now.date()
    before_run = (now.hour, now.minute) < DISCOVER_RUN_ET
    d = today
    for _ in range(14):
        if d.weekday() == 4 and us_clock.is_trading_day(d) and (d > today or before_run):
            break
        d += timedelta(days=1)
    return {"next_weekly": d.isoformat(), "intake_every_hours": 6}


def recalc(data_dir: Path, repo, signature: str, *, symbols: list[str], start: date, end: date) -> dict:
    """On-demand backtest of one candidate / the engine over a custom pool or window (not persisted)."""
    if not can_run_heavy():
        raise RuntimeError("盘中不重算")
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    cfg = {"op": "recalc", "signature": signature, "symbols": symbols, "start": start.isoformat(), "end": end.isoformat()}
    with shared_heavy_job_limiter.slot("normal"):
        out = run_worker_task(make_worker_task("factor_lab", Path(data_dir), cfg))
    if out.get("_error") == "not_found":
        raise KeyError(signature)
    if out.get("_error"):
        raise ValueError(out["_error"])
    return out


def _recalc_impl(data_dir: Path, repo, signature: str, *, symbols: list[str], start: date, end: date) -> dict:
    service = _service(repo, Path(data_dir))
    if signature == "engine":
        ref = engine_reference(service, Path(data_dir), symbols, start, end)
        if ref is None:
            return {"_error": "当前出卡引擎没有可回测的做多规则"}
        # same flat shape as a candidate recalc (the page reads DiscoverStats fields directly)
        return {**ref["stats"], "yearly": ref["yearly"], "legs": ref["legs"]}
    w = load_week(data_dir) or {}
    cand = next((c for c in w.get("candidates") or [] if c.get("signature") == signature), None)
    if cand is None:
        return {"_error": "not_found"}
    res = _run(service, "factor_rank_research", symbols, start, end,
               {"scoring": cand["scoring"], "directions": cand["directions"], **RANK_PARAMS})
    nav = _series(res)
    dates = sorted(nav)
    out = _summary(res)
    if dates:
        out["yearly"] = _yearly(dates, _norm(nav, dates))
    return out
