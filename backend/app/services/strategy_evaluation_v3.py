"""证据 v3 运行器 (2026-10-05)。规则见 design/evidence-v3-preregistration.md, 统计在 evidence_v3。

输出写并行文件 evaluation_v3.json (不改 v1/v2, 不改许可); 许可是否改用 v3 由 evidence_mode 决定。
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

from app.services import evidence_v3 as ev3

logger = logging.getLogger(__name__)

LOOKBACK = 70
BASELINE_SAMPLE = 100


# ── 统一成卡的数组版 (与 decision_desk.card_from_pick 逐步同口径; 测试逐张核对) ─────────

GEOMETRY_BARS = 80   # 成卡只用到最近 60 根最高价 + 15 根真实波幅; 取最近 80 根“有效”日K (与生产的整段日K等价)


def card_geometry(o, h, l, c, t: int, stop_loss: float | None):
    """o/h/l/c: 单只股票完整序列 (float32, 与矩阵同精度)。与生产 card_from_pick 同口径: 生产拿到的是该股
    已有的全部日K 行 (缺失日本来就没有行), 所以这里取截至 t 的最近 80 根**有效**日K, 而不是最近 70 个交易日再去缺失
    (2026-10-06 审查: 后者在近 70 日缺 >10 根时 60 日高点口径与生产不一致)。
    返回 (lo, hi, invalidation, (tlo, thi), risk_per_share) 或 None。"""
    valid = np.flatnonzero(~np.isnan(c[:t + 1]))
    if valid.size < 20:
        return None
    idx = valid[-GEOMETRY_BARS:]
    hh, ll, cc = h[idx], l[idx], c[idx]
    last = float(cc[-1])
    if not math.isfinite(last) or last <= 0:
        return None
    if cc.shape[0] < 6:
        return None
    pc = cc[:-1]
    tr = np.maximum(hh[1:] - ll[1:], np.maximum(np.abs(hh[1:] - pc), np.abs(ll[1:] - pc)))
    a = float(np.mean(tr[-14:]))
    if not math.isfinite(a) or a <= 0:
        return None
    lo, hi = round(last, 4), round(last * 1.01, 4)
    mid = (lo + hi) / 2
    cands = [mid - 2.0 * a]
    if stop_loss is not None and -0.5 < stop_loss < 0:
        cands.append(mid * (1 + stop_loss))
    invalidation = round(max(cands), 4)
    if not math.isfinite(invalidation) or invalidation <= 0 or invalidation >= lo:
        return None
    risk = mid - invalidation
    tail = hh[-60:]
    if np.all(np.isnan(tail)):
        return None
    recent_high = float(np.nanmax(tail))
    target = max(recent_high, mid + 2.0 * risk)
    rr = (target - mid) / risk
    if rr < 2.0 or risk / mid < 0.005:
        return None
    tlo, thi = round(target * 0.98, 4), round(target, 4)
    rps, rr_r = round(risk, 4), round(rr, 2)
    if not (lo > 0 and hi >= lo) or not (thi >= tlo > hi) or rr_r < 2.0 or rps <= 0:
        return None
    return lo, hi, invalidation, (tlo, thi), rps


class Sim:
    """单个 (股票, 信号日, 止损) → 回放 R; 结果按键缓存 (同一股票同日同止损的卡几何完全相同)。"""

    def __init__(self, md, last_valid: np.ndarray, delisted: np.ndarray):
        from app.services import strategy_evaluation as se
        self.se = se
        self.o, self.h, self.l, self.c = md.open, md.high, md.low, md.close
        self.T = md.close.shape[0]
        self.last_valid = last_valid
        self.delisted = delisted
        self.cache: dict = {}

    def run(self, i: int, t: int, stop: float | None):
        key = (i, t, stop)
        if key in self.cache:
            return self.cache[key]
        geo = card_geometry(self.o[:, i], self.h[:, i], self.l[:, i], self.c[:, i], t, stop)
        res = None
        if geo is not None:
            lo, hi, inval, tz, rps = geo
            se = self.se
            f0, f1 = t + 1, min(t + 1 + se.ENTRY_WINDOW + se.MAX_HOLD + 2, self.T)
            last_idx = None
            if self.delisted[i] and f0 <= self.last_valid[i] < f1:
                last_idx = int(self.last_valid[i] - f0)
            r = se.simulate_native_daily(self.o[f0:f1, i].astype(float), self.h[f0:f1, i].astype(float),
                                         self.l[f0:f1, i].astype(float), self.c[f0:f1, i].astype(float),
                                         entry_zone=(lo, hi), invalidation=inval, target=tz, risk=rps,
                                         last_index=last_idx)
            res = {"status": r["status"], "r": r.get("r"), "exit_reason": r.get("exit_reason"),
                   "fill": (t + 1 + r["fill_index"]) if r.get("fill_index") is not None else None,
                   "exit": (t + 1 + r["exit_index"]) if r.get("exit_index") is not None else None, "rr": None}
        else:
            res = {"status": "no_card", "r": None}
        self.cache[key] = res
        return res


class Baseline:
    """同日同池随机挑股 (固定种子) 的期望 R, 按止损比例分别计算。"""

    def __init__(self, sim: Sim, pool: np.ndarray, seed: int = 20261005):
        self.sim, self.pool, self.seed = sim, pool, seed
        self.cache: dict = {}

    def mean(self, t: int, stop: float | None) -> float | None:
        key = (t, stop)
        if key in self.cache:
            return self.cache[key]
        elig = np.flatnonzero(self.pool[t])
        val = None
        if elig.size:
            rng = np.random.default_rng(self.seed + t)
            pick = rng.choice(elig, size=min(BASELINE_SAMPLE, elig.size), replace=False)
            rs = [x["r"] for x in (self.sim.run(int(i), t, stop) for i in pick) if x["status"] == "closed"]
            val = float(np.mean(rs)) if rs else None
        self.cache[key] = val
        return val


def _instruments(repo) -> dict[str, dict]:
    df = repo.get_instruments()
    cols = [c for c in ("symbol", "name", "instrument_type", "is_etf", "is_preferred", "is_warrant", "is_unit")
            if c in df.columns]
    return {r["symbol"]: r for r in df.select(cols).to_dicts()}


def _evaluate_picks(sid: str, picks_by_t: dict[int, list[int]], stop, *, md, sim: Sim, base: Baseline, days,
                    watchlist: set[str]) -> list[dict]:
    trades = []
    for t, idxs in sorted(picks_by_t.items()):
        b = base.mean(t, stop)
        for i in idxs:
            res = sim.run(i, t, stop)
            if res["status"] != "closed":
                continue
            sym = md.symbols[i]
            trades.append({"symbol": sym, "signal_day": str(days[t]),
                           "entry_day": str(days[res["fill"]]) if res.get("fill") is not None else None,
                           "exit_day": str(days[min(res["exit"], len(days) - 1)]) if res.get("exit") is not None else None,
                           "r": res["r"], "base_r": None if b is None else round(b, 4),
                           "excess": None if b is None else round(res["r"] - b, 4),
                           "exit_reason": res["exit_reason"], "watchlist": sym in watchlist})
    return trades


def run_v3(data_dir: Path, start: date, end: date, *, repo=None, engine=None, only: list[str] | None = None,
           earn_dir: Path | None = None, sec_dir: Path | None = None, out_path: Path | None = None,
           cancel=None, hypothesis_dir: Path | None = None) -> dict:
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import strategy_evaluation as se
    from app.services import strategy_evaluation_runner as runner
    from app.storage.repository import DataStore, KlineRepository

    data_dir = Path(data_dir)
    repo = repo or KlineRepository(DataStore(data_dir))
    engine = engine or runner._strategy_engine(data_dir)
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    try:
        import polars as pl
        watchlist = set(pl.read_parquet(data_dir / "user_data" / "watchlist.parquet")["symbol"].to_list())
    except Exception:  # noqa: BLE001
        watchlist = set()
    ids = [m["id"] for m in engine.list_strategies()
           if not m.get("research_only") and m.get("execution_backend") == "matrix_native"]
    if only:
        ids = [i for i in ids if i in set(only)]
    out_path = Path(out_path) if out_path else evaluation_v3_path(data_dir)
    inst = _instruments(repo)
    payload = {"contract": ev3.CONTRACT_V3, "window": [str(start), str(end)],
               "holdout_start": str(date(ev3.DISCOVERY_END.year + 1, 1, 1)),
               "pool": "signal-day trailing 21-session mean $vol >= $20M, close >= $5, >=130 bars, common/ADR only "
                       "(no ETF/leveraged/fund/preferred/warrant/unit); delisted names included (ticker~delist_date)",
               "baseline": f"same day, same pool, same card+replay rules, {BASELINE_SAMPLE} random names (fixed seed), "
                           "per stop-loss ratio", "picks_per_day": se.PICKS_PER_DAY,
               "sources": {}, "trades": {}, "evaluated": [], "preregistration": "design/evidence-v3-preregistration.md",
               "cluster_unit": ev3.CLUSTER_UNIT, "explore_enabled": ev3.EXPLORE_ENABLED, "calibration": ev3.CALIBRATION}
    t0 = time.perf_counter()

    partial_path = out_path.with_name(out_path.stem + ".partial.json")

    def _write(final: bool = False):
        """运行中只写 .partial (进度可见); 全部算完 (含 Holm/许可档) 才原子替换正式文件 —
        中途失败不会用半成品覆盖上一份完整结果 (2026-10-06 审查)。"""
        from app.services.fs_utils import atomic_write_text
        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out_path if final else partial_path, json.dumps(payload, ensure_ascii=False))
        if final and partial_path.exists():
            partial_path.unlink(missing_ok=True)

    shared = {}

    def _prepare(md):
        """同一底层数据 (符号轴/日期轴一致) 只建一次池子/基线/回放缓存。"""
        key = (md.symbols, md.timestamp_labels[0], md.timestamp_labels[-1])
        if shared.get("key") == key:
            return shared
        days = [date.fromisoformat(x[:10]) for x in md.timestamp_labels]
        asset_ok = ev3.tradable_asset_mask(md.symbols, inst)
        pool = ev3.pool_mask(*ev3.real_price_volume(md), asset_ok)
        finite = np.isfinite(md.close)
        T = finite.shape[0]
        last_valid = np.where(finite.any(axis=0), T - 1 - np.argmax(finite[::-1], axis=0), -1)
        delisted = np.array(["~" in s for s in md.symbols])
        sim = Sim(md, last_valid, delisted)
        shared.clear()
        shared.update({"key": key, "days": days, "pool": pool, "sim": sim, "base": Baseline(sim, pool), "md": md,
                       "formal": [t for t, d in enumerate(days) if start <= d <= end]})
        logger.info("v3 pool prepared: %d days x %d symbols, mean pool size %.0f", T, len(md.symbols),
                    float(pool[shared["formal"]].sum(axis=1).mean()) if shared["formal"] else 0)
        return shared

    for sid in ids:
        if cancel is not None and cancel():
            payload["cancelled"] = True
            break
        ts = time.perf_counter()
        try:
            md, entry, score, stop_loss = runner._signals_for(sid, svc, engine, start, end)
            S = _prepare(md)
            desc = bool(engine.get(sid).meta.get("descending", True))
            picks = {}
            for t in S["formal"]:
                hit = np.flatnonzero(entry[t] & S["pool"][t])
                if hit.size == 0:
                    continue
                key = np.nan_to_num(score[t, hit], nan=-np.inf if desc else np.inf)
                picks[t] = [int(i) for i in hit[np.argsort(-key if desc else key, kind="stable")][: se.PICKS_PER_DAY]]
            trades = _evaluate_picks(sid, picks, stop_loss, md=md, sim=S["sim"], base=S["base"], days=S["days"],
                                     watchlist=watchlist)
            summary = ev3.finalize_source(trades, holdout_start=date.fromisoformat(payload["holdout_start"]))
            summary.update({"source": sid, "kind": "strategy", "stop_loss": stop_loss, "signal_days": len(picks),
                            "fingerprint": runner.strategy_fingerprint(engine, sid, contract=ev3.CONTRACT_V3),
                            "elapsed_s": round(time.perf_counter() - ts, 1)})
        except Exception as e:  # noqa: BLE001
            logger.exception("evaluation v3 %s failed", sid)
            payload["sources"][sid] = {"source": sid, "error": f"{type(e).__name__}: {e}"[:300]}
            continue
        payload["sources"][sid] = summary
        payload["trades"][sid] = trades[-400:]
        payload["evaluated"].append(sid)
        logger.info("evaluation v3 %s: n=%s mean=%s excess=%s holdout=%s %.0fs", sid, summary["n"], summary["mean"],
                    summary["excess"].get("mean"), summary["holdout"].get("mean"), summary["elapsed_s"])
        _write()

    if (earn_dir or sec_dir) and shared.get("md") is not None and not payload.get("cancelled"):
        try:
            _event_sources(payload, shared, earn_dir, sec_dir, watchlist)
        except Exception as e:  # noqa: BLE001 - 事件数据坏 (格式变/压缩包损坏) 不拖垮策略来源的评估
            logger.exception("evaluation v3 event sources failed")
            payload["event_sources_error"] = f"{type(e).__name__}: {e}"[:300]
    ev3.apply_holm(payload["sources"])
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["elapsed_s"] = round(time.perf_counter() - t0, 1)
    payload["hypotheses_tested"] = sum(1 for v in payload["sources"].values() if isinstance(v, dict) and "excess" in v)
    if payload.get("cancelled"):
        _write()            # 取消: 只留 .partial, 正式文件保持上一份完整结果
        return payload
    _write(final=True)
    _append_hypothesis_ledger(Path(hypothesis_dir) if hypothesis_dir else data_dir, payload)
    return payload


def _event_sources(payload: dict, S: dict, earn_dir, sec_dir, watchlist: set[str]) -> None:
    md, days, pool = S["md"], S["days"], S["pool"]
    day_index = {d: t for t, d in enumerate(days)}
    formal = set(S["formal"])
    by_ticker: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(md.symbols):
        by_ticker[s.split("~")[0]].append(i)

    def resolve(sym: str, t: int) -> int | None:
        for i in by_ticker.get(sym, ()):
            if np.isfinite(md.close[t, i]):
                return i
        return None

    signals = {}
    try:
        _load_event_signals(signals, md, days, by_ticker, earn_dir, sec_dir)
    except Exception as e:  # noqa: BLE001
        logger.exception("v3 event signal load failed")
        payload["event_sources_error"] = f"{type(e).__name__}: {e}"[:300]
    _score_event_sources(payload, S, signals, resolve, formal, day_index, watchlist)


def _load_event_signals(signals: dict, md, days, by_ticker, earn_dir, sec_dir) -> None:
    from app.services import event_sources as es
    if earn_dir and Path(earn_dir).exists():
        closes = {}
        need = set()
        for r in es.earnings_rows(Path(earn_dir)):
            need.add(r["symbol"])
        for sym in need:
            idxs = by_ticker.get(sym)
            if not idxs:
                continue
            m = {}
            for i in idxs:
                col = md.close[:, i]
                for t in np.flatnonzero(np.isfinite(col)):
                    m[days[t]] = float(col[t])
            closes[sym] = m
        try:
            signals["earnings_beat_drift"] = (es.earnings_beat_signals(Path(earn_dir), days, closes), None)
        except Exception:  # noqa: BLE001
            logger.exception("v3 E1 earnings signals failed")
    if sec_dir and Path(sec_dir).exists():
        try:
            cov = es.sec_coverage_end(Path(sec_dir))
            signals["insider_cluster_buy"] = (es.insider_cluster_signals(Path(sec_dir), days), cov)
        except Exception:  # noqa: BLE001
            logger.exception("v3 E2 insider signals failed")


def _score_event_sources(payload, S, signals, resolve, formal, day_index, watchlist) -> None:
    from app.services import event_sources as es
    from app.services import strategy_evaluation as se
    md, days, pool = S["md"], S["days"], S["pool"]
    for sid, (sig, cov_end) in signals.items():
        ts = time.perf_counter()
        picks = {}
        for d, rows in sig.items():
            t = day_index.get(d)
            if t is None or t not in formal:
                continue
            got = []
            for sym, _score in rows:
                i = resolve(sym, t)
                if i is not None and pool[t, i] and i not in got:
                    got.append(i)
                if len(got) >= se.PICKS_PER_DAY:
                    break
            if got:
                picks[t] = got
        trades = _evaluate_picks(sid, picks, es.EVENT_STOP_LOSS, md=md, sim=S["sim"], base=S["base"], days=days,
                                 watchlist=watchlist)
        summary = ev3.finalize_source(trades, holdout_start=date.fromisoformat(payload["holdout_start"]))
        summary.update({"source": sid, "kind": "event", "name": es.EVENT_SOURCES[sid], "stop_loss": es.EVENT_STOP_LOSS,
                        "signal_days": len(picks), "raw_events": sum(len(v) for v in sig.values()),
                        "data_coverage_end": str(cov_end) if cov_end else None,
                        "elapsed_s": round(time.perf_counter() - ts, 1)})
        payload["sources"][sid] = summary
        payload["trades"][sid] = trades[-400:]
        payload["evaluated"].append(sid)
        logger.info("evaluation v3 event %s: n=%s mean=%s excess=%s holdout=%s", sid, summary["n"], summary["mean"],
                    summary["excess"].get("mean"), summary["holdout"].get("mean"))


def evaluation_v3_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "evaluation_v3.json"


def hypothesis_ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "hypothesis_ledger.jsonl"


def _append_hypothesis_ledger(data_dir: Path, payload: dict) -> None:
    """累计检验台账: 每个曾被检验的来源一行 (只增不删; 同一来源同一契约只记首次)。"""
    p = hypothesis_ledger_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                seen.add((r.get("source"), r.get("contract")))
            except ValueError:
                continue
    with open(p, "a", encoding="utf-8") as f:
        for sid, v in payload["sources"].items():
            if not isinstance(v, dict) or "excess" not in v or (sid, payload["contract"]) in seen:
                continue
            f.write(json.dumps({"source": sid, "contract": payload["contract"], "kind": v.get("kind"),
                                "first_tested": payload.get("generated_at"), "window": payload["window"],
                                "tier_v3": v.get("tier_v3"), "p": (v.get("excess") or {}).get("p")},
                               ensure_ascii=False) + "\n")
