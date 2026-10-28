"""统一口径策略评估运行器 (应用内; 周度自动任务与 scripts/evaluate_strategies.py 共用)。

口径见 strategy_evaluation 模块文档。逐个策略运行, 每个策略算完立即落盘 (中断不丢已完成部分)。
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)


def _strategy_engine(data_dir: Path):
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine
    root = Path(__file__).resolve().parents[1] / "strategy" / "builtin"
    dirs = [root, data_dir / "strategies" / "custom", data_dir / "strategies" / "ai", data_dir / "strategies" / "composite"]
    return StrategyEngine(strategy_dirs=dirs, override_loader=lambda sid: strategy_config.load_override(data_dir, sid))


def _signals_for(sid, svc, engine, start: date, end: date):
    """返回 (market_data, entry bool (T,N), score (T,N)) —— 与回测页完全同一条信号路径。"""
    md, sig, stop_loss = _signal_matrix(sid, svc, engine, start, end)
    return md, np.asarray(sig.entry, bool), np.asarray(sig.score, float), stop_loss


def _signal_matrix(sid, svc, engine, start: date, end: date, *, symbols: list[str] | None = None,
                   warm_days: int | None = None, disk_cache: bool | None = None):
    """(market_data, SignalMatrix, stop_loss): _signals_for 的底层 (同一条信号路径)。
    symbols=None = 全市场; warm_days=None = 按策略预热 (与证据评估一致)。0.26.0 每日对账用 symbols=自选 +
    warm_days=900 (结构类策略从序列开头累积, 预热越长越接近回测全历史) + disk_cache=False (小矩阵只在内存建,
    不往回测磁盘缓存里每天加一份、也不触发按大小淘汰把全市场那份挤掉)。"""
    from app.backtest.matrix import MatrixPipelineConfig, MatrixStrategyPipeline
    from app.backtest.strategy import (StrategyBacktestConfig, StrategyDependencyResolver, _basic_filter_for_asset,
                                       build_matrix_cache_profile)
    from app.config import settings
    from app.strategy.scoring import effective_scoring, effective_scoring_directions

    s = engine.get(sid)
    cfg = StrategyBacktestConfig(strategy_id=sid, symbols=None, start=start, end=end, mode="full", holding_days=7)
    overrides = engine._override_loader(sid) if engine._override_loader else {}
    overrides = overrides or {}
    # 用户在策略页保存的参数 (overrides.params) 必须进入评估, 与生产选股同一套参数 (2026-10-05 修正: 旧版传 {} 只用默认值)
    params = svc._normalize_params(dict(overrides.get("params") or {}), s)
    basic_filter = _basic_filter_for_asset(svc._effective_basic_filter(s, overrides), "stock")
    entry_signals = svc._effective_signals(overrides, "entry_signals", s.entry_signals)
    exit_signals = svc._effective_signals(overrides, "exit_signals", s.exit_signals)
    plan = StrategyDependencyResolver().resolve(s, params=params, basic_filter=basic_filter, entry_signals=entry_signals,
                                                exit_signals=exit_signals, overrides=overrides, minute_fill=False,
                                                asset_type="stock")
    profile = build_matrix_cache_profile(engine, "stock", requested_plan=plan, requested_forward_bars=12,
                                         max_disk_bytes=settings.backtest_matrix_cache_max_mb * 1024 * 1024)
    warm = max(120, int(max(plan.warmup_bars, 1) * 1.6)) if warm_days is None else int(warm_days)
    load_start = start - timedelta(days=warm)
    load_end = end + timedelta(days=40)
    cov_start = min(load_start, start - timedelta(days=max(120, int(max(profile.warmup_bars, 1) * 1.6))))
    extra = {} if disk_cache is None else {"disk_cache": disk_cache}      # 默认调用与 0.25 逐字相同
    md = svc.engine.load_market_data_matrix_for_backtest(symbols, load_start, load_end, plan, asset_type="stock",
                                                         cache_profile=profile,
                                                         coverage_start=cov_start,
                                                         coverage_end=end + timedelta(days=(profile.forward_bars + 5) * 2),
                                                         **extra)
    pc = MatrixPipelineConfig(basic_filter=basic_filter, scoring=effective_scoring(s.meta.get("scoring"), overrides),
                              scoring_directions=effective_scoring_directions(overrides), order_by=s.meta.get("order_by"),
                              descending=bool(s.meta.get("descending", True)))
    timing: dict = {}
    sig = MatrixStrategyPipeline().run(s.matrix_strategy, md, params, pc, timing)
    stop_loss = svc._override_value(overrides, "stop_loss", s.stop_loss)
    return md, sig, stop_loss


def _daily_frames(md, idx: int, t: int, lookback: int = 70) -> pl.DataFrame:
    lo = max(0, t - lookback + 1)
    labels = md.timestamp_labels[lo:t + 1]
    return pl.DataFrame({"date": [date.fromisoformat(x[:10]) for x in labels], "open": md.open[lo:t + 1, idx],
                         "high": md.high[lo:t + 1, idx], "low": md.low[lo:t + 1, idx], "close": md.close[lo:t + 1, idx],
                         "volume": md.volume[lo:t + 1, idx]}).drop_nulls().filter(pl.col("close").is_not_nan())


def evaluate_strategy(sid, svc, engine, start, end, watchlist: set[str]) -> tuple[dict, list[dict]]:
    from app.services import decision_desk as dd
    from app.services import strategy_evaluation as se

    t0 = time.perf_counter()
    md, entry, score, stop_loss = _signals_for(sid, svc, engine, start, end)
    labels = md.timestamp_labels
    days = np.array([date.fromisoformat(x[:10]) for x in labels])
    dv = np.where(np.isfinite(md.close) & np.isfinite(md.volume), md.close * md.volume, np.nan)
    ranks = se.prior_dollar_volume_rank(dv, window=60)
    formal = np.flatnonzero((days >= start) & (days <= end))
    trades, statuses = [], defaultdict(int)
    for t in formal:
        hit = np.flatnonzero(entry[t] & (ranks[t] > 0) & (ranks[t] <= se.TOP_N))
        if hit.size == 0:
            continue
        order = hit[np.argsort(-np.nan_to_num(score[t, hit], nan=-np.inf), kind="stable")][: se.PICKS_PER_DAY]
        for i in order:
            sym = md.symbols[i]
            b = _daily_frames(md, i, t)
            card = dd.card_from_pick(sid, sym, b, as_of=days[t], stop_loss=stop_loss)
            if card is None:
                statuses["no_card"] += 1
                continue
            fut = slice(t + 1, t + 1 + se.ENTRY_WINDOW + se.MAX_HOLD + 2)
            res = se.simulate_7d(md.open[fut, i], md.high[fut, i], md.low[fut, i], md.close[fut, i],
                                 entry_zone=card.entry_zone, invalidation=card.invalidation, target=card.target_zone,
                                 risk=card.risk_per_share)
            statuses[res["status"]] += 1
            if res["status"] != "closed":
                continue
            trades.append({"symbol": sym, "signal_day": str(days[t]), "entry_day": str(days[t]), "r": res["r"],
                           "exit_reason": res["exit_reason"], "days": res["days"],
                           "bucket": "top100" if ranks[t, i] <= se.TOP_SMALL else "top1000",
                           "watchlist": sym in watchlist, "rr": card.reward_risk})
    summary = se.summarize(trades)
    summary.update({"source": sid, "statuses": dict(statuses), "elapsed_s": round(time.perf_counter() - t0, 1),
                    "method": "daily_bar_unified_7d"})
    return summary, trades


def strategy_fingerprint(engine, sid: str, contract: str | None = None) -> dict:
    """策略版本指纹: 源码 + 有效覆盖配置 (参数/评分/止损/信号) + 评估口径。证据只对同一指纹有效。"""
    import hashlib
    from app.services import strategy_evaluation as se
    contract = contract or se.CONTRACT_V2
    s = engine.get(sid)
    code = b""
    try:
        if s.file_path:
            code = Path(s.file_path).read_bytes()
    except OSError:
        pass
    overrides = (engine._override_loader(sid) if getattr(engine, "_override_loader", None) else {}) or {}
    relevant = {k: overrides.get(k) for k in sorted(overrides) if k not in ("display_limit", "notes", "updated_at")}
    h = hashlib.sha256()
    h.update(code)
    h.update(json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str).encode())
    h.update(contract.encode())
    return {"hash": h.hexdigest()[:16], "code_sha": hashlib.sha256(code).hexdigest()[:12] if code else None,
            "overrides": relevant, "contract": contract}


def evaluate_strategy_v2(sid, svc, engine, start, end, watchlist: set[str]) -> tuple[dict, list[dict]]:
    """v2: 生产同口径参数 + 原生半仓止盈日线近似 + 信号/成交/退出日分列; 聚类按真实成交日。"""
    from app.services import decision_desk as dd
    from app.services import strategy_evaluation as se

    t0 = time.perf_counter()
    md, entry, score, stop_loss = _signals_for(sid, svc, engine, start, end)
    labels = md.timestamp_labels
    days = np.array([date.fromisoformat(x[:10]) for x in labels])
    dv = np.where(np.isfinite(md.close) & np.isfinite(md.volume), md.close * md.volume, np.nan)
    ranks = se.prior_dollar_volume_rank(dv, window=60)
    formal = np.flatnonzero((days >= start) & (days <= end))
    desc = bool(engine.get(sid).meta.get("descending", True))
    trades, statuses = [], defaultdict(int)
    for t in formal:
        hit = np.flatnonzero(entry[t] & (ranks[t] > 0) & (ranks[t] <= se.TOP_N))
        if hit.size == 0:
            continue
        key = np.nan_to_num(score[t, hit], nan=-np.inf if desc else np.inf)
        order = hit[np.argsort(-key if desc else key, kind="stable")][: se.PICKS_PER_DAY]
        for i in order:
            sym = md.symbols[i]
            b = _daily_frames(md, i, t)
            card = dd.card_from_pick(sid, sym, b, as_of=days[t], stop_loss=stop_loss)
            if card is None:
                statuses["no_card"] += 1
                continue
            fut = slice(t + 1, t + 1 + se.ENTRY_WINDOW + se.MAX_HOLD + 2)
            res = se.simulate_native_daily(md.open[fut, i], md.high[fut, i], md.low[fut, i], md.close[fut, i],
                                           entry_zone=card.entry_zone, invalidation=card.invalidation,
                                           target=card.target_zone, risk=card.risk_per_share)
            statuses[res["status"]] += 1
            if res["status"] != "closed":
                continue
            fill_day = days[t + 1 + res["fill_index"]]
            exit_day = days[min(t + 1 + res["exit_index"], len(days) - 1)]
            trades.append({"symbol": sym, "signal_day": str(days[t]), "entry_day": str(fill_day), "exit_day": str(exit_day),
                           "r": res["r"], "exit_reason": res["exit_reason"], "days": res["days"], "partial": res["partial"],
                           "bucket": "top100" if ranks[t, i] <= se.TOP_SMALL else "top1000",
                           "watchlist": sym in watchlist, "rr": card.reward_risk})
    summary = se.summarize(trades)
    summary.update({"source": sid, "statuses": dict(statuses), "elapsed_s": round(time.perf_counter() - t0, 1),
                    "method": "daily_bar_native_partial_v2", "fingerprint": strategy_fingerprint(engine, sid),
                    "censored": int(statuses.get("open_at_end", 0))})
    return summary, trades


def compare_versions(v1: dict | None, v2: dict | None) -> dict:
    """新旧证据并排: 每个来源的 n/均值/下界, 以及按现行门槛的许可是否改变。"""
    from app.services import decision_desk as dd
    out = {}
    s1, s2 = (v1 or {}).get("sources") or {}, (v2 or {}).get("sources") or {}
    for sid in sorted(set(s1) | set(s2)):
        a, b = s1.get(sid) or {}, s2.get(sid) or {}
        wa, wb = dd.source_weight(a) if a else None, dd.source_weight(b) if b else None
        out[sid] = {"v1": {k: a.get(k) for k in ("n", "mean", "ci_low", "ci_high", "method")} if a else None,
                    "v2": {k: b.get(k) for k in ("n", "mean", "ci_low", "ci_high", "method", "censored")} if b else None,
                    "tier_v1": (wa or {}).get("tier"), "tier_v2": (wb or {}).get("tier"),
                    "weight_v1": (wa or {}).get("weight"), "weight_v2": (wb or {}).get("weight"),
                    "changes_permission": bool(wa and wb and wa.get("tier") != wb.get("tier")),
                    "fingerprint": b.get("fingerprint")}
    return out


def run_all_v2(data_dir: Path, start: date, end: date, *, repo=None, engine=None, only: list[str] | None = None,
               out_path: Path | None = None, cancel=None) -> dict:
    """v2 评估写 evaluation_v2.json (并行文件, 不改现行 evaluation.json 与任何许可)。"""
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import strategy_evaluation as se
    from app.storage.repository import DataStore, KlineRepository

    data_dir = Path(data_dir)
    repo = repo or KlineRepository(DataStore(data_dir))
    engine = engine or _strategy_engine(data_dir)
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    try:
        watchlist = set(pl.read_parquet(data_dir / "user_data" / "watchlist.parquet")["symbol"].to_list())
    except Exception:  # noqa: BLE001
        watchlist = set()
    ids = [m["id"] for m in engine.list_strategies()
           if not m.get("research_only") and m.get("execution_backend") == "matrix_native"]
    if only:
        ids = [i for i in ids if i in set(only)]
    if not ids:
        raise RuntimeError("没有可评估的策略 (策略目录未加载)")
    out_path = Path(out_path) if out_path else se.evaluation_v2_path(data_dir)
    payload = {"contract": se.CONTRACT_V2, "window": [str(start), str(end)],
               "universe": f"prior-60d median dollar volume top {se.TOP_N}", "picks_per_day": se.PICKS_PER_DAY,
               "rules": "same entry/invalidation/target as v1; near target sells HALF (stop→BE), far target exits rest; "
                        "gap-down fill day ignores pre-fill high; cluster by actual fill day; saved strategy params used",
               "sources": {}, "trades": {}, "evaluated": []}
    t0 = time.perf_counter()

    def _write():
        from app.services.fs_utils import atomic_write_text
        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out_path, json.dumps(payload, ensure_ascii=False))
    for sid in ids:
        if cancel is not None and cancel():
            payload["cancelled"] = True
            break
        try:
            summary, trades = evaluate_strategy_v2(sid, svc, engine, start, end, watchlist)
        except Exception as e:  # noqa: BLE001
            logger.exception("strategy evaluation v2 %s failed", sid)
            payload["sources"][sid] = {"source": sid, "error": f"{type(e).__name__}: {e}"[:300]}
            continue
        payload["sources"][sid] = summary
        payload["trades"][sid] = trades[-400:]
        payload["evaluated"].append(sid)
        logger.info("evaluation v2 %s: n=%s mean=%s ci=[%s,%s] %.0fs", sid, summary["n"], summary["mean"],
                    summary["ci_low"], summary["ci_high"], summary["elapsed_s"])
        _write()
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["elapsed_s"] = round(time.perf_counter() - t0, 1)
    payload["comparison"] = compare_versions(se.load_evaluation_file(data_dir, "v1"), payload)
    _write()
    return payload


def replay_summary(replay_trades: Path, watchlist: set[str], ranks_by_day: dict | None = None) -> dict:
    """S1/S2: 真实规则重放 (分钟级, 生产 SwingTrader) 的逐笔结果, 同一 summarize 口径。"""
    from app.services import strategy_evaluation as se
    rows = json.loads(replay_trades.read_text())
    out = {}
    for setup in ("breakout", "pullback"):
        tr = [{"r": r["r_multiple"], "entry_day": r["entry_date"], "exit_reason": (r["exit_reasons"] or ["?"])[-1],
               "bucket": "top1000", "watchlist": r["symbol"] in watchlist}
              for r in rows if r["setup"] == setup and r["r_multiple"] is not None]
        s = se.summarize(tr)
        s.update({"source": setup, "method": "minute_replay_production_trader", "statuses": {"closed": len(tr)}})
        out[setup] = s
    return out



def run_all(data_dir: Path, start: date, end: date, *, repo=None, engine=None, only: list[str] | None = None,
            replay_trades: Path | None = None, out_path: Path | None = None, cancel=None) -> dict:
    """评估全部可运行策略并写 evaluation.json; 返回 payload。"""
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import strategy_evaluation as se
    from app.storage.repository import DataStore, KlineRepository

    data_dir = Path(data_dir)
    repo = repo or KlineRepository(DataStore(data_dir))
    engine = engine or _strategy_engine(data_dir)
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    try:
        watchlist = set(pl.read_parquet(data_dir / "user_data" / "watchlist.parquet")["symbol"].to_list())
    except Exception:  # noqa: BLE001
        watchlist = set()
    ids = [m["id"] for m in engine.list_strategies()
           if not m.get("research_only") and m.get("execution_backend") == "matrix_native"]
    if only:
        ids = [i for i in ids if i in set(only)]
    out_path = Path(out_path) if out_path else se.evaluation_path(data_dir)
    # 审计 2026-10-26 M-9: 增量基底必须是 v1 文件本身。load_evaluation 按「当前证据模式」解释
    # (v4 下返回许可视图: 每条规则 {v4, enabled, tradable, forward}; v3 带 "v3": true; v2 带 carried_from),
    # 拿它当旧证据会把视图行写进 v1 文件、删掉未重评的来源 (breakout / pullback 回放) 和逐笔记录。
    # 另把历史已混进来的视图形状行剔掉。
    previous = se.load_evaluation_file(data_dir, "v1") or {}
    prev_sources = {k: v for k, v in (previous.get("sources") or {}).items() if not se.is_mode_view_row(v)}
    payload = {"sources": prev_sources, "trades": dict(previous.get("trades") or {})}
    payload.update({"window": [str(start), str(end)], "universe": f"prior-60d median dollar volume top {se.TOP_N}",
                    "picks_per_day": se.PICKS_PER_DAY,
                    "rules": "entry 0~+1% next 5 days, gap>2% skip, inval=max(strategy stop,2ATR), target>=2R (60d high), "
                             "target-low exit, close<stop→next open, 1R→BE, 2R→+1R, day-7 close; 5bps+$0.005/sh"})
    if replay_trades and Path(replay_trades).exists():
        payload["sources"].update(replay_summary(Path(replay_trades), watchlist))
    t0 = time.perf_counter()
    payload["evaluated"] = []
    if not ids:
        raise RuntimeError("没有可评估的策略 (策略目录未加载)")
    for sid in ids:
        if cancel is not None and cancel():
            payload["cancelled"] = True
            break
        try:
            summary, trades = evaluate_strategy(sid, svc, engine, start, end, watchlist)
        except Exception as e:  # noqa: BLE001
            logger.exception("strategy evaluation %s failed", sid)
            payload["sources"][sid] = {"source": sid, "error": f"{type(e).__name__}: {e}"[:300]}
            continue
        payload["sources"][sid] = summary
        payload["trades"][sid] = trades[-400:]
        payload["evaluated"].append(sid)
        logger.info("evaluation %s: n=%s mean=%s ci=[%s,%s] %.0fs", sid, summary["n"], summary["mean"],
                    summary["ci_low"], summary["ci_high"], summary["elapsed_s"])
        se.save_evaluation(data_dir, payload) if out_path == se.evaluation_path(data_dir) else out_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["elapsed_s"] = round(time.perf_counter() - t0, 1)
    if out_path == se.evaluation_path(data_dir):
        se.save_evaluation(data_dir, payload)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload
