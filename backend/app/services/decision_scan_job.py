"""决策台生产编排: 当日运行全部已启用/观察中的策略 → decision_scan.build → 当日卡 + 摘要落盘。

- 策略选股: 生产 StrategyEngine.run_all (与策略页同一入口), 每个策略取评分前 10 只, 只看流动性池内的股票
- 证据: data/decision_desk/evaluation.json (scripts/evaluate_strategies.py 统一口径生成)
- 上周权重: data/decision_desk/weights.json (每周最多 ±50%)
- 摘要: data/decision_desk/runs/<day>.json (机会页“为什么选它/为什么只观察”的出处)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import polars as pl

from app.services import decision_desk as dd
from app.services import decision_scan, strategy_evaluation

logger = logging.getLogger(__name__)

PICKS_PER_STRATEGY = strategy_evaluation.PICKS_PER_DAY
WEIGHT_REFRESH_DAYS = 7


def _app_state():
    try:
        from app.jobs.daily_pipeline import _get_app_state
        return _get_app_state()
    except Exception:  # noqa: BLE001
        return None


def _desk_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk"


_WL_CACHE: dict = {}


def _watchlist() -> set[str]:
    """自选名单, 按 parquet 文件 (mtime, size) 缓存 (盘中每张卡每帧都会问一次)。读失败 = 空集 (fail-closed)。"""
    try:
        from app.services import watchlist
        try:
            st = watchlist._path().stat()
            sig = (str(watchlist._path()), st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        hit = _WL_CACHE.get("v")
        if sig is not None and hit and hit[0] == sig:
            return set(hit[1])
        out = {r["symbol"] for r in watchlist.list_symbols() if r.get("symbol")}
        if sig is not None:
            _WL_CACHE["v"] = (sig, frozenset(out))
        return out
    except Exception:  # noqa: BLE001
        return set()


# 决策台取候选时不受策略页“显示条数”截断 (2026-10-05): 引擎默认先按 META.limit=100 截断再交给这里过
# 流动性池, 池内强信号排在全市场第 100 名之后就会被丢掉。显式 display_limit=0 = 不截断。
DESK_DISPLAY_LIMIT = 0


def strategy_picks(repo, as_of: date, universe: set[str]) -> tuple[dict[str, list[str]], dict[str, float | None], dict]:
    """返回 ({strategy_id: [symbols…]}, {strategy_id: stop_loss}, meta)。

    meta["hits"][sid] = 池内全部命中 [{symbol, score, rank}] (按策略自身评分排序, rank 从 1 起) —
    供自选独立选股与逐票诊断使用; picks 仍是每策略池内前 PICKS_PER_STRATEGY 名。
    """
    state = _app_state()
    engine = getattr(state, "strategy_engine", None) if state is not None else None
    if engine is None:
        return {}, {}, {"error": "strategy engine unavailable"}
    from app.services.screener import ScreenerService
    from app.strategy import config as strategy_config
    svc = ScreenerService(repo, asset_type="stock")
    ids = [m["id"] for m in engine.list_strategies()
           if not m.get("research_only") and "stock" in m.get("asset_types", ["stock"]) and "1d" in m.get("timeframes", ["1d"])]
    data_dir = repo.store.data_dir
    overrides_map = {sid: strategy_config.load_override(data_dir, sid) or {} for sid in ids}
    run_overrides = {sid: {**ov, "display_limit": DESK_DISPLAY_LIMIT} for sid, ov in overrides_map.items()}
    t0 = time.perf_counter()
    context = svc.build_strategy_context(engine, as_of, ids, timeframe="1d", overrides_map=run_overrides)
    results = engine.run_all(context, overrides_map=run_overrides, strategy_ids=ids)
    _rerun_long_memory(svc, engine, as_of, context, results, run_overrides, ids)
    picks, stops, hits, sell_hits = {}, {}, {}, {}
    from app.services import watchlist_v4 as wv
    for sid, res in results.items():
        rows = [r for r in (res.rows or []) if r.get("symbol") in universe]
        picks[sid] = [r["symbol"] for r in rows[:PICKS_PER_STRATEGY]]
        hits[sid] = [{"symbol": r["symbol"], "score": _num(r.get("score")), "rank": i + 1} for i, r in enumerate(rows)]
        s = engine.get(sid)
        stops[sid] = (overrides_map.get(sid) or {}).get("stop_loss", s.stop_loss)
        # 卖出信号 = 源脚本的反向信号 (TV 翻空/Sell): 只给机会卡出看空卡 (只观察), 不进决策台/交易
        if sid in wv.SELL_IS_SHORT:
            sell_hits[sid] = [{"symbol": h["symbol"], "rank": None} for h in (res.exit_signal_hits or [])
                              if h.get("symbol") in universe]
    return picks, stops, {"strategies": len(ids), "elapsed_s": round(time.perf_counter() - t0, 1), "hits": hits,
                          "sell_hits": sell_hits}


# 结构状态从序列开头累积的策略: 共享窗口 (~212 根) 截断后漏报/误报 (SMC CHoCH 召回 61%, 400 根 97%,
# oppfit-1016 合成面板实测)。只对自选重跑一次长窗口 (自选 ~60 只, 矩阵小), 结果替换共享窗口的那份。
LONG_MEMORY_BARS = {"tv_smc_structure": 420}


def _rerun_long_memory(svc, engine, as_of: date, context, results: dict, run_overrides: dict, ids: list[str]) -> None:
    from dataclasses import replace

    import polars as pl
    todo = [sid for sid in LONG_MEMORY_BARS if sid in ids and sid in results]
    if not todo:
        return
    wl = _watchlist()
    if not wl:
        return
    try:
        hist = svc._load_enriched_history(as_of, max(LONG_MEMORY_BARS[s] for s in todo))
        if hist is None or hist.is_empty():
            return
        hist = hist.filter(pl.col("symbol").is_in(sorted(wl)))
        n_bars = int(hist["date"].n_unique()) if "date" in hist.columns else 0
        cur = context.current.filter(pl.col("symbol").is_in(sorted(wl))) if context.current is not None else None
        ctx = replace(context, current=cur, history=hist, market=None, cache_key=None)
        for sid in todo:
            need = LONG_MEMORY_BARS[sid]
            if n_bars < need:
                # short window would be worse than the shared window; keep the shared result
                logger.warning("long-memory rerun %s skipped: got %d bars, need %d", sid, n_bars, need)
                continue
            long_res = engine.run(sid, ctx, overrides=run_overrides.get(sid))
            base = results[sid]
            # 自选之外的命中保留共享窗口结果; 自选内一律以长窗口为准
            rows = [r for r in (base.rows or []) if r.get("symbol") not in wl] + list(long_res.rows or [])
            exits = [h for h in (base.exit_signal_hits or []) if h.get("symbol") not in wl] + list(long_res.exit_signal_hits or [])
            # 0.25.1: re-sort like engine.run (score desc), otherwise watchlist hits sit after every
            # non-watchlist hit and get cut from picks / pushed down in rank
            rows.sort(key=lambda r: (_num(r.get("score")) is None, -(_num(r.get("score")) or 0.0), str(r.get("symbol"))))
            results[sid] = replace(base, rows=rows, total=len(rows), exit_signal_hits=exits)
            logger.info("long-memory rerun %s: %d bars, watchlist hits long=%d short=%d", sid,
                        hist["date"].n_unique(), len(long_res.rows or []), len(long_res.exit_signal_hits or []))
    except Exception as e:
        logger.warning("long-memory rerun failed (keeps shared-window result): %s", type(e).__name__)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 6) if f == f and abs(f) != float("inf") else None


def _phantom_4h_picks(repo, as_of: date, frames: dict, picks: dict, meta: dict, include: set[str] | None = None) -> dict:
    """原版 4h Phantom 扫描 (吊灯止损) 作为独立候选来源 (2026-10-03)。失败只降级, 不影响其他来源。

    返回 {source: {symbol: stop_loss}} — 4h 来源的止损因股而异 (吊灯止损位), 不是策略级固定比例。
    """
    from app import optional
    p4 = optional.load("app.services.phantom_4h_source")
    if p4 is None:   # 开源核心版不含该来源: 跳过, 方向门回落到内置门
        meta["phantom_4h"] = {"unavailable": True}
        _refresh_m30(repo, as_of, include, meta)
        return {}
    try:
        provider = None
        try:
            from app.data_providers.custom import get_provider
            provider = get_provider("alpaca")
        except Exception as e:  # noqa: BLE001
            logger.warning("phantom_4h: alpaca provider unavailable: %s", type(e).__name__)
        res = p4.run(repo, provider, as_of, frames, include=include)
        picks[p4.SOURCE_ID] = [s for s in res["picks"] if s in frames]
        picks[p4.SHORT_SOURCE_ID] = [s for s in res.get("short_picks", []) if s in frames]
        hits = meta.setdefault("hits", {})
        hits[p4.SOURCE_ID] = [h for h in res.get("hits", []) if h["symbol"] in frames]
        hits[p4.SHORT_SOURCE_ID] = [h for h in res.get("short_hits", []) if h["symbol"] in frames]
        out = {p4.SOURCE_ID: res["stops"], p4.SHORT_SOURCE_ID: res.get("short_stops", {})}
        meta["phantom_4h_state"] = dict(res.get("state") or {})      # tight-v1: 自选 4h 方向状态 (落信号表)
        if "pine_picks" in res:     # Pine 原版 (2026-10-09): 独立来源, 只攒前向样本
            picks[p4.PINE_SOURCE_ID] = [s for s in res["pine_picks"] if s in frames]
            hits[p4.PINE_SOURCE_ID] = [h for h in res.get("pine_hits", []) if h["symbol"] in frames]
            out[p4.PINE_SOURCE_ID] = res.get("pine_stops", {})
        pine_meta = res["meta"].get("pine") or {}
        meta[p4.SOURCE_ID] = {k: v for k, v in res["meta"].items() if not k.startswith("detail") and k != "pine"} | {
            "picks": len(picks[p4.SOURCE_ID]), "short_picks": len(picks[p4.SHORT_SOURCE_ID])}
        meta[p4.PINE_SOURCE_ID] = {k: v for k, v in pine_meta.items() if k != "detail"} | {
            "picks": len(picks.get(p4.PINE_SOURCE_ID, []))}
        return out
    except Exception as e:  # noqa: BLE001
        logger.exception("phantom_4h source failed (跳过该来源)")
        meta[p4.SOURCE_ID] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        return {}


def _refresh_m30(repo, as_of: date, symbols: set[str] | None, meta: dict) -> None:
    """30 分钟K 数据层的盘后增量 (卡结算、盘前作废检查、盘前放量排序、数据新鲜度都读它)。
    完整版由 4h 来源扫描顺带刷新; 没有该来源时在这里单独刷新自选。失败只记 meta, 不影响扫描。"""
    syms = sorted(symbols or ())
    if not syms:
        return
    try:
        from app.data_providers.custom import get_provider
        from app.services import kline_4h
        provider = get_provider("alpaca")
        data_dir = Path(repo.store.data_dir)
        meta["m30"] = {"refresh": kline_4h.refresh_recent(data_dir, repo, provider, syms, as_of),
                       "history": kline_4h.ensure_history(data_dir, repo, provider, syms, as_of)}
    except Exception as e:  # noqa: BLE001
        meta["m30"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        logger.warning("m30 refresh failed: %s", meta["m30"]["error"])


def _direction_states(repo, data_dir: Path, as_of: date, frames: dict, watchlist, meta: dict) -> dict[str, str]:
    """机会板方向门: 自选股 → long/short (写进信号快照的 h4_state, 字段名沿用历史)。

    当前门由 direction_gate.active 决定 (插件门如 4h Phantom 优先, 缺失时用内置日线 MA50 门)。
    插件门在来源扫描里已经算好的方向放在 meta["phantom_4h_state"], 作为 precomputed 交给门复用。
    任何失败 = 空 (不放行), 只记日志。"""
    from app.opportunity import direction_gate
    pre = {"phantom_4h_state": meta.pop("phantom_4h_state", {}) or {}}
    gate = direction_gate.active(data_dir)
    try:
        ctx = direction_gate.GateContext(data_dir=Path(data_dir), repo=repo, as_of=as_of,
                                         symbols=tuple(sorted(watchlist or ())), frames=frames, precomputed=pre)
        states = gate.states(ctx) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("direction gate %s failed (不放行): %s", gate.id, type(e).__name__)
        states = {}
    meta["direction_gate"] = {"id": gate.id, "long": sum(1 for v in states.values() if v == "long"),
                              "short": sum(1 for v in states.values() if v == "short")}
    return {s: v for s, v in states.items() if v in ("long", "short")}


def _previous_weights(data_dir: Path, as_of: date) -> tuple[dict[str, float] | None, bool]:
    p = _desk_dir(data_dir) / "weights.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return None, True
        last = date.fromisoformat(d.get("as_of", ""))
    except (OSError, TypeError, ValueError):
        return None, True
    due = (as_of - last).days >= WEIGHT_REFRESH_DAYS
    return d.get("weights", {}), due


def _save_weights(data_dir: Path, as_of: date, weights: dict[str, dict]) -> None:
    p = _desk_dir(data_dir) / "weights.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"as_of": as_of.isoformat(), "weights": {s: w.get("weight", 0.0) for s, w in weights.items()}},
                            ensure_ascii=False, indent=1), encoding="utf-8")


def run(opp_svc, as_of: date, *, s1s2, frames: dict[str, pl.DataFrame], params: dict, excluded: set[str],
        risk_used_pct: float):
    repo = opp_svc.repo
    data_dir = Path(repo.store.data_dir)
    evaluation = strategy_evaluation.load_evaluation(data_dir)
    # 自选名单在扫描这一刻读取并落盘 (名单会动态增删; 卡上冻结的是扫描时的归属)。
    watchlist = _watchlist()
    picks, stops, meta = strategy_picks(repo, as_of, set(frames))
    stops_by_symbol = _phantom_4h_picks(repo, as_of, frames, picks, meta, include=watchlist)
    hits = meta.pop("hits", {}) or {}
    sell_hits = meta.pop("sell_hits", {}) or {}
    h4_state = _direction_states(repo, data_dir, as_of, frames, watchlist, meta)
    # 自选独立选股 (2026-10-05 产品决策): 每个来源在自选池内的全部命中都成为候选, 不受“全池前 10”限制;
    # 门槛 (流动性池 / 统一成卡 / 来源证据 / 组合约束) 与全池一致, 同账户同风控。
    lane = {sid: [h["symbol"] for h in rows if h["symbol"] in watchlist and h["symbol"] not in set(picks.get(sid, []))]
            for sid, rows in hits.items()}
    lane = {sid: syms for sid, syms in lane.items() if syms}
    merged = {sid: list(syms) + lane.get(sid, []) for sid, syms in picks.items()}
    for sid, syms in lane.items():
        merged.setdefault(sid, list(syms))
    registry = dd.SourceRegistry(data_dir, builtin=set(dd.BUILTIN_SETUPS))
    registry.seed(set(picks) | set(dd.BUILTIN_SETUPS))
    previous, due = _previous_weights(data_dir, as_of)
    from app.services import shortable as _shortable
    out = decision_scan.build(as_of=as_of, strategy_picks=merged, s1s2=s1s2, frames=frames, evaluation=evaluation,
                              shortable=_shortable.checker(data_dir),
                              registry=registry, watchlist=watchlist, stop_losses=stops,
                              stop_losses_by_symbol=stops_by_symbol,
                              sector_map=opp_svc.sector_map(), portfolio_risk_used_pct=risk_used_pct,
                              active_symbols=excluded, params=params,
                              previous_weights=previous, weights_due=due,
                              watchlist_lane={sid: set(v) for sid, v in lane.items()},
                              source_hits=hits, industry_map=_industry_map(opp_svc))
    if due:
        _save_weights(data_dir, as_of, out.weights)
    funnel = diagnose_watchlist(watchlist, frames=frames, hits=hits, picks=picks, lane=lane, s1s2=s1s2, out=out,
                                universe_diag=getattr(opp_svc, "universe_diag", None) or {})
    out.summary.update({"meta": meta, "evaluation_generated_at": (evaluation or {}).get("generated_at"),
                        "evaluation_window": (evaluation or {}).get("window"), "weights_refreshed": due,
                        "generated_at": _now_iso(), "watchlist_snapshot": sorted(watchlist),
                        "watchlist_lane": {sid: len(v) for sid, v in lane.items()},
                        "source_hits": {sid: len(v) for sid, v in hits.items()}})
    _news_shadow_snapshot(data_dir, as_of, out)
    _shadow_forward_snapshot(data_dir, as_of, frames=frames, hits=hits, stops=stops, stops_by_symbol=stops_by_symbol,
                             out=out, s1s2=s1s2)
    _watchlist_v4_snapshot(data_dir, as_of, watchlist=watchlist, frames=frames, hits=hits, stops=stops,
                           stops_by_symbol=stops_by_symbol, s1s2=s1s2, out=out, sell_hits=sell_hits, h4_state=h4_state)
    out.summary["sell_hits"] = {sid: len(v) for sid, v in sell_hits.items()}
    runs = _desk_dir(data_dir) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    from app.services.fs_utils import atomic_write_text
    atomic_write_text(runs / f"{as_of.isoformat()}.json",
                      json.dumps({"summary": out.summary, "ranked": out.ranked[:300], "funnel": funnel},
                                 ensure_ascii=False, indent=1))
    logger.info("decision desk %s: candidates=%d opened=%d watched=%d", as_of, len(out.ranked), len(out.result.cards),
                len(out.result.watch))
    return out


def watchlist_signals(as_of: date, *, watchlist: set[str], frames: dict, hits: dict, stops: dict,
                      stops_by_symbol: dict | None, s1s2) -> dict[str, list[dict]]:
    """自选 × 全部规则的当日 +1 (多头): {rule: [{symbol, card, stop, rank, total}]}。成卡规则与交易员一致:
    内置/自定义策略与 Phantom 4h 用 card_from_pick (Phantom 用逐股吊灯止损), S1/S2 用原生卡。"""
    out: dict[str, list[dict]] = {}
    for sid, rows in (hits or {}).items():
        if dd.side_of(sid) != "long":
            continue
        per = (stops_by_symbol or {}).get(sid)
        total = len(rows)
        for h in rows:
            sym = h["symbol"]
            if sym not in watchlist or sym not in frames:
                continue
            stop = per.get(sym) if per is not None else stops.get(sid)
            card = dd.card_from_pick(sid, sym, frames[sym], as_of=as_of, stop_loss=stop)
            if card is None:
                continue
            out.setdefault(sid, []).append({"symbol": sym, "card": card, "stop": stop, "rank": h.get("rank"),
                                            "total": total, "per_symbol_stop": per is not None})
    for c in list(getattr(s1s2, "cards", []) or []) + list(getattr(s1s2, "watch", []) or []):
        if c.symbol in watchlist and not c.is_short:
            out.setdefault(dd.source_of(c.setup_type), []).append({"symbol": c.symbol, "card": c, "stop": "native",
                                                                   "rank": None, "total": None})
    return out


def watchlist_short_signals(as_of: date, *, watchlist: set[str], frames: dict, hits: dict,
                            stops_by_symbol: dict | None, sell_hits: dict | None = None,
                            stops: dict | None = None) -> dict[str, list[dict]]:
    """自选上的当日看空卡 (只进自选信号表供机会卡展示, 只观察: 不进影子账本、规则集与下单许可)。来源两类:
    ① 空头规则 (decision_desk.SHORT_SOURCES, 如 Phantom 4h 空): 止损 = 逐股空头吊灯 (正比例);
    ② TradingView 移植策略的卖出信号 (watchlist_v4.SELL_IS_SHORT; 源脚本里就是翻空/Sell): 失效位 = 统一空头卡 2ATR
       (这些脚本的止损是指标线本身, 不给固定比例)。内置策略的卖出信号 (“持有理由没了”) 不当做空。"""
    from app.services import watchlist_v4 as wv
    out: dict[str, list[dict]] = {}
    sources = [(sid, rows, True) for sid, rows in (hits or {}).items() if dd.side_of(sid) == "short"]
    sources += [(sid, rows, False) for sid, rows in (sell_hits or {}).items() if sid in wv.SELL_IS_SHORT]
    for sid, rows, per_symbol in sources:
        per = (stops_by_symbol or {}).get(sid) or {}
        total = len(rows)
        for h in rows:
            sym = h["symbol"]
            if sym not in watchlist or sym not in frames:
                continue
            if per_symbol:
                stop = per.get(sym)
            else:
                st = (stops or {}).get(sid)
                stop = abs(st) if isinstance(st, (int, float)) and -0.5 < st < 0 else None
            card = dd.card_from_pick(sid, sym, frames[sym], as_of=as_of, stop_loss=stop, side="short")
            if card is None:
                continue
            out.setdefault(sid, []).append({"symbol": sym, "card": card, "stop": stop, "rank": h.get("rank"),
                                            "total": total, "per_symbol_stop": per_symbol})
    return out


def _watchlist_v4_snapshot(data_dir: Path, as_of: date, *, watchlist, frames, hits, stops, stops_by_symbol, s1s2,
                           out, sell_hits: dict | None = None, h4_state: dict | None = None) -> None:
    """自选影子账本 (v4, 不占资金): 全部规则在自选上的 +1 卡 + 同票基线卡; 同时落当日自选信号表 (另含只观察的看空卡)。
    失败只记日志。"""
    try:
        from app.services import watchlist_v4 as wv
        sig = watchlist_signals(as_of, watchlist=watchlist, frames=frames, hits=hits, stops=stops,
                                stops_by_symbol=stops_by_symbol, s1s2=s1s2)
        short = {}
        try:
            short = watchlist_short_signals(as_of, watchlist=watchlist, frames=frames, hits=hits,
                                            stops_by_symbol=stops_by_symbol, sell_hits=sell_hits, stops=stops)
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist short signals skipped: %s", type(e).__name__)
        base_stops = {v for v in (stops or {}).values() if v is None or isinstance(v, (int, float))}
        added = wv.record(data_dir, as_of, watchlist=watchlist, frames=frames, signals=sig, baseline_stops=base_stops,
                          short_signals=short)
        lineage, row_extra = None, None
        try:   # 方向 A 谱系 (只读许可, 只追加新键); 失败照常落表
            from app.services import card_lineage
            lineage, row_extra = card_lineage.stamp(data_dir, as_of, rules=sorted({**sig, **short}))
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist lineage stamp skipped: %s", type(e).__name__)
        wv.save_signals(data_dir, as_of, {**sig, **short}, watchlist=watchlist, frames=frames,
                        lineage=lineage, row_extra=row_extra, extra_rows=_evo_rows(data_dir, as_of, watchlist, out),
                        h4_state=h4_state, atr=_atr_map(frames, watchlist))
        out.summary["watchlist_v4"] = {"recorded": sum(added.values()), "rules_with_signal": sorted(sig),
                                       "short_signals": sum(len(v) for v in short.values())}
    except Exception as e:  # noqa: BLE001
        logger.warning("watchlist v4 snapshot skipped: %s", type(e).__name__)


def _atr_map(frames: dict, watchlist) -> dict[str, float]:
    """自选的日线 ATR14 (与 decision_desk.atr 同式, 截至扫描日收盘)。tight-v1 机会卡几何用。"""
    out = {}
    for s in watchlist:
        b = frames.get(s)
        if b is None or b.is_empty():
            continue
        try:
            a = dd.atr(b)
        except Exception:
            continue
        if a == a and a > 0:
            out[s] = round(float(a), 6)
    return out


def _evo_rows(data_dir: Path, as_of: date, watchlist: set[str], out) -> list[dict]:
    """已退役 (2026-10-15): 不再注入 rule=evo 信号行。"""
    out.summary["evo"] = {"retired": True}
    return []


def _shadow_forward_snapshot(data_dir: Path, as_of: date, *, frames, hits, stops, stops_by_symbol, out,
                             s1s2=None) -> None:
    """影子前向账本 (不占资金): 全部来源当日前 10 + S1/S2 原生卡 + 同日随机基线。失败只记日志, 不影响决策台。"""
    try:
        from app.services import shadow_forward
        native = [(c.setup_type, c) for c in (list(getattr(s1s2, "cards", []) or []) + list(getattr(s1s2, "watch", []) or []))
                  if not c.is_short]
        added = shadow_forward.record(data_dir, as_of, frames=frames, hits=hits, stops=stops,
                                      stops_by_symbol=stops_by_symbol, pool=sorted(frames), native_cards=native)
        out.summary["shadow_forward"] = {"recorded": sum(added.values()), "by_source": added}
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow forward snapshot skipped: %s", type(e).__name__)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _industry_map(opp_svc) -> dict[str, str]:
    fn = getattr(opp_svc, "industry_map", None)
    try:
        return fn() if callable(fn) else {}
    except Exception:  # noqa: BLE001
        return {}


def diagnose_watchlist(watchlist: set[str], *, frames: dict, hits: dict, picks: dict, lane: dict, s1s2, out,
                       universe_diag: dict) -> dict:
    """自选逐票漏斗 (批次② 2026-10-05, 只写诊断, 不改任何交易输出)。

    每只自选一行: 是否进流动性池 (未进的原因) → 哪些来源命中 (原始分/名次/分母, 是全池前 10 还是自选通道)
    → 成卡结果 (失败原因) → 决策台最终状态 (开卡 / 观察 + 原因)。没有命中任何来源就写“无来源命中”。
    """
    final = {c.symbol: ("open", c.state_reason, c) for c in out.result.cards}
    for c in out.result.watch:
        final.setdefault(c.symbol, ("watch", c.state_reason, c))
    s1s2_rej = {}
    for sym, why in (s1s2.rejected or []):
        s1s2_rej.setdefault(sym, []).append(why)
    s1s2_hits = {c.symbol: c.setup_type for c in list(s1s2.cards) + list(s1s2.watch)}
    failures = getattr(out, "card_failures", {}) or {}
    rows = {}
    for sym in sorted(watchlist):
        row: dict = {"in_pool": sym in frames}
        if sym not in frames:
            row["stage"] = "pool"
            row["reason"] = universe_diag.get(sym) or "未进流动性池（近 30 日均成交额 / 价格 / 历史天数不达标或无日K）"
            rows[sym] = row
            continue
        src_hits = []
        for sid, hs in hits.items():
            h = next((x for x in hs if x["symbol"] == sym), None)
            if h is None:
                continue
            via = "pool_top" if sym in set(picks.get(sid, [])) else ("watchlist" if sym in set(lane.get(sid, [])) else "none")
            src_hits.append({"source": sid, "score": h.get("score"), "rank": h.get("rank"), "total": len(hs), "via": via})
        if sym in s1s2_hits:
            src_hits.append({"source": s1s2_hits[sym], "score": None, "rank": None, "total": None, "via": "s1s2"})
        row["hits"] = src_hits
        if sym in s1s2_rej:
            row["s1s2_rejected"] = s1s2_rej[sym]
        if sym in failures:
            row["card_failures"] = failures[sym]
        if sym in final:
            st, why, card = final[sym]
            row.update(stage="card", state=st, reason=why, card_id=card.id, primary=card.setup_type)
        elif not src_hits:
            row.update(stage="no_signal", reason="当日无来源命中")
        else:
            row.update(stage="card_build", reason="有来源命中但统一成卡未通过（RR<2 / 风险过小 / 价位非法）")
        rows[sym] = row
    return rows


def _news_shadow_snapshot(data_dir: Path, as_of: date, out) -> None:
    """新闻催化影子记账 (只观察, 不改任何卡): 当日全部候选卡 (开 + 观察) 的新闻快照。失败只记日志。"""
    try:
        from app.services import news_shadow
        cands = []
        for card, opened in [(c, True) for c in out.result.cards] + [(c, False) for c in out.result.watch]:
            dec = (card.evidence or {}).get("decision") or {}
            cands.append({"symbol": card.symbol, "entry_zone": card.entry_zone, "invalidation": card.invalidation,
                          "target_zone": card.target_zone, "risk_per_share": card.risk_per_share,
                          "sources": dec.get("sources") or [card.setup_type], "side": card.side, "opened": opened})
        n = news_shadow.record_snapshot(data_dir, as_of, cands)
        out.summary["news_shadow"] = {"recorded": n, "candidates": len(cands)}
    except Exception as e:  # noqa: BLE001
        logger.warning("news shadow snapshot skipped: %s", type(e).__name__)


def latest_run(data_dir: Path) -> dict | None:
    d = _desk_dir(data_dir) / "runs"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def source_permission(data_dir: Path, source: str) -> dict:
    """Read current registry and current evidence on EVERY call; runs are not permissions.

    Missing/corrupt strategy evidence fails closed. Only pre-desk S1/S2 can retain
    their original defaults, before any registry/evaluation/run has ever appeared.
    """
    reg = dd.SourceRegistry(data_dir, builtin=set(dd.BUILTIN_SETUPS))
    evaluation = strategy_evaluation.load_evaluation(data_dir)
    sources = evaluation.get("sources") if isinstance(evaluation, dict) else None
    evidence = sources.get(source) if isinstance(sources, dict) else None
    desk = _desk_dir(data_dir)
    legacy = (source in dd.BUILTIN_SETUPS and strategy_evaluation.evidence_mode(data_dir) == "v1"
              and not (desk / "evaluation.json").exists()
              and not reg.path.exists() and not (desk / "weights.json").exists()
              and not any((desk / "runs").glob("*.json")))
    w = ({"weight": 1.0, "tier": "proven", "risk_pct": dd.PROVEN_RISK_PCT,
          "reason": "旧S1/S2首次无决策台证据兼容"} if legacy else dd.source_weight(evidence))
    return reg.effective(source, w)


def source_tradable(data_dir: Path, source: str) -> bool:
    """New-entry permission only; never gates management of existing positions."""
    return source_permission(data_dir, source).get("tradable") is True
