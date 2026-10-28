"""决策台每日扫描 (纯组装, 无 I/O): 策略选股 + S1/S2 → 统一成卡 → 证据权重/共振/探索额度 → 组合约束 → 当日卡。

输入都由调用方准备 (生产: decision_scan_job.run; 测试: 直接构造), 本模块不读文件、不联网。
输出 ScanResult 与 S1/S2 原格式完全一致, 走同一个 OpportunityService.save_scan / 生命周期 / 纸面交易员。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

import polars as pl

from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import decision_desk as dd


@dataclass
class DeskOutput:
    result: ScanResult
    ranked: list[dict] = field(default_factory=list)
    weights: dict[str, dict] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    card_failures: dict[str, list[str]] = field(default_factory=dict)  # symbol → [rr_or_zone:source …] (诊断用)


def weights_from_evaluation(evaluation: dict | None, registry: dd.SourceRegistry, sources: set[str],
                            previous: dict[str, float] | None = None, *, due: bool = True) -> dict[str, dict]:
    ev = (evaluation or {}).get("sources", {})
    out = {}
    for s in sorted(sources):
        w = dd.source_weight(ev.get(s))
        if previous is not None:
            w["target_weight"] = w["weight"]
            w["weight"] = dd.weekly_weight(previous=previous.get(s), target=w["weight"], due=due)
        e = ev.get(s) or {}
        w["evidence"] = {k: e.get(k) for k in ("n", "mean", "ci_low", "ci_high", "win_rate", "method")} if e else None
        out[s] = registry.effective(s, w)
    return out


def build(*, as_of: date, strategy_picks: dict[str, list[str]], s1s2: ScanResult, frames: dict[str, pl.DataFrame],
          evaluation: dict | None, registry: dd.SourceRegistry, watchlist: set[str], stop_losses: dict[str, float | None],
          sector_map: dict[str, str], portfolio_risk_used_pct: float, active_symbols: set[str], params: dict,
          previous_weights: dict[str, float] | None = None, weights_due: bool = True,
          stop_losses_by_symbol: dict[str, dict[str, float | None]] | None = None,
          shortable=None, watchlist_lane: dict[str, set[str]] | None = None,
          source_hits: dict[str, list[dict]] | None = None, industry_map: dict[str, str] | None = None) -> DeskOutput:
    # 1) 每个来源的候选卡 (S1/S2 用原生卡, 其他策略用统一成卡)
    cards_by_symbol: dict[str, dict[str, OpportunityCard]] = {}
    for c in list(s1s2.cards) + list(s1s2.watch):
        cards_by_symbol.setdefault(c.symbol, {})[c.setup_type] = c
    rejected = list(s1s2.rejected)
    failures: dict[str, list[str]] = {}
    lane = watchlist_lane or {}
    hit_index = {sid: {h["symbol"]: h for h in rows} for sid, rows in (source_hits or {}).items()}
    for source, symbols in strategy_picks.items():
        for sym in symbols:
            b = frames.get(sym)
            if b is None:
                rejected.append((sym, f"no_bars:{source}"))
                failures.setdefault(sym, []).append(f"no_bars:{source}")
                continue
            per_symbol = (stop_losses_by_symbol or {}).get(source)
            stop = per_symbol.get(sym) if per_symbol is not None else stop_losses.get(source)
            h = hit_index.get(source, {}).get(sym)
            extra = {"pick": {"via": "watchlist" if sym in lane.get(source, ()) else "pool_top",
                              **({"score": h.get("score"), "rank": h.get("rank"),
                                  "total": len(hit_index.get(source, {}))} if h else {})}}
            card = dd.card_from_pick(source, sym, b, as_of=as_of, stop_loss=stop, extra=extra,
                                     revision=params.get("revision", 1), side=dd.side_of(source))
            if card is None:
                rejected.append((sym, f"rr_or_zone:{source}"))
                failures.setdefault(sym, []).append(f"rr_or_zone:{source}")
                continue
            cards_by_symbol.setdefault(sym, {})[source] = card
    sources = {dd.source_of(s) for per in cards_by_symbol.values() for s in per}
    # 权重覆盖全部已评估来源 + 上周有权重的来源 (不只当日有候选的): 周更落盘时当天没选股的来源也要
    # 保留其周权重, 否则下周会从目标值直接起跳, 绕过 ±50% 限幅 (2026-10-05)。
    all_sources = (sources | {"breakout", "pullback"} | set((evaluation or {}).get("sources", {}) or {})
                   | set(previous_weights or {}))
    weights = weights_from_evaluation(evaluation, registry, all_sources, previous_weights, due=weights_due)

    # 2) 综合排序 (共振 = 同日被几个来源选中)
    picks = {sym: [dd.source_of(s) for s in per] for sym, per in cards_by_symbol.items()}
    ranked = dd.rank(picks, weights, watchlist=watchlist)
    kept, held = dd.apply_explore_quota(ranked)
    held_syms = {r["symbol"] for r in held}

    # 3) 每只股票选一张代表卡: 优先最高权重的可交易来源; 否则首个来源 (只观察)
    result = ScanResult(as_of=as_of, gate=s1s2.gate, params_revision=s1s2.params_revision or params.get("revision", 0),
                        scanned=s1s2.scanned, rejected=rejected)
    pf, gate = params["portfolio"], params["market_gate"]
    daily_cap = pf["daily_new_cards_max"] if result.gate == "green" else gate["caution_card_max"]
    risk_cap = pf["max_portfolio_risk_pct"] if result.gate == "green" else gate["caution_risk_pct"]
    risk_used = portfolio_risk_used_pct
    active = set(active_symbols)
    active_sectors: dict[str, int] = {}
    for s in active:
        active_sectors[sector_map.get(s, "?")] = active_sectors.get(sector_map.get(s, "?"), 0) + 1
    by_source: dict[str, dict[str, int]] = {}

    for r in kept + held:
        sym = r["symbol"]
        per = cards_by_symbol[sym]
        def _w(st):
            return weights.get(dd.source_of(st), {}).get("weight", 0.0)
        best_key = max(per, key=lambda st: (dd.source_of(st) in r["tradable_sources"], _w(st)))
        card = per[best_key]
        src = dd.source_of(card.setup_type)
        sides = {c.side for c in per.values()}
        hard_blockers = []
        if len(sides) > 1:
            hard_blockers.append("direction_conflict")
        if not r["tradable"]:
            hard_blockers.append("source_not_tradable")
        if r["risk_pct"] <= 0:
            hard_blockers.append("risk_zero")
        decision = {"sources": r["sources"], "tradable_sources": r["tradable_sources"], "resonance": r["resonance"],
                    "direction_conflict": len(sides) > 1, "sides": sorted(sides), "hard_blockers": hard_blockers,
                    "score": r["score"], "tier": r["tier"], "risk_pct": r["risk_pct"], "tab": r["tab"],
                    "primary_source": src, "weights": {s: {k: weights.get(s, {}).get(k) for k in ("weight", "tier", "status", "reason")}
                                                       for s in r["sources"]},
                    # 诊断字段 (只展示, 不参与排序): 各来源在自身评分下的原始分/名次, 是否经自选通道选入
                    "source_scores": {dd.source_of(st): (c.evidence or {}).get("pick") for st, c in per.items()
                                      if (c.evidence or {}).get("pick")},
                    "via_watchlist_lane": any(sym in lane.get(dd.source_of(st), ()) for st in per),
                    # 只因自选通道才成为可交易候选: 自选移出后不再新开 (entry_guard 每帧核对当前自选名单)
                    "lane_only": bool(r["tradable_sources"]) and all(sym in lane.get(t, ()) for t in r["tradable_sources"])}
        if decision["lane_only"] and (evaluation or {}).get("mode") != "v4":
            # 来源证据只覆盖每日全池前 10; 自选通道的票排在第 10 名之后, 不在证据总体内 (10-05 研究: 长下影
            # 第 11-30 名 −0.206R vs 第 4-10 名 +0.291R)。开仓照做 (无限拍板), 单笔风险按探索档 0.25%。
            decision["risk_pct"] = min(decision["risk_pct"], dd.EXPLORE_RISK_PCT)
            decision["lane_risk_capped"] = True
        card = replace(card, evidence={**card.evidence, "decision": decision, "sector": sector_map.get(sym),
                                       "industry": (industry_map or {}).get(sym)})
        stat = by_source.setdefault(src, {"opened": 0, "watched": 0})

        def _watch(reason, card=card, stat=stat):   # 默认参数绑定当轮 card/stat (当轮立即调用)
            result.watch.append(card.with_state(CardState.WATCH, reason))
            stat["watched"] += 1

        if (evaluation or {}).get("mode") == "v4" and sym not in watchlist:
            _watch("非自选：只记账")
            continue
        if not r["tradable"]:
            _watch("；".join(weights.get(s, {}).get("reason", "") for s in r["sources"] if weights.get(s)))
            continue
        if len(sides) > 1:
            _watch("多空来源冲突（同日既有做多来源又有做空来源）：只观察")
            continue
        if sym in held_syms:
            _watch("探索额度已用完（每天最多 1 张探索单）")
            continue
        if result.gate == "unavailable":
            _watch("SPY基准数据不足")
            continue
        sec = sector_map.get(sym)
        if not sec:
            _watch("行业尚未核实")
            continue
        if len(result.cards) >= daily_cap:
            _watch("超出每日新卡上限")
            continue
        if sym in active:
            _watch("同 symbol 已有活跃卡")
            continue
        if card.is_short and (shortable is None or shortable(sym) is not True):
            _watch("不可借券（Alpaca 标记不可卖空或非易借）")
            continue
        if active_sectors.get(sec, 0) >= pf["sector_max_active"]:
            _watch(f"行业 {sec} 已达上限")
            continue
        est = decision["risk_pct"] * 100
        if risk_used + est > risk_cap:
            _watch("组合风险超限")
            continue
        result.cards.append(card.with_state(CardState.OPEN, f"决策台通过（{r['tier']}，综合分 {r['score']:.3f}）"))
        stat["opened"] += 1
        active.add(sym)
        active_sectors[sec] = active_sectors.get(sec, 0) + 1
        risk_used += est

    # 同主题扎堆 (只提示): 可交易候选按细分行业计数, 同一行业 ≥3 只即提示 (10-01 长下影 10 只里 7 只加密矿企)
    crowd: dict[str, list[str]] = {}
    for r in ranked:
        ind = (industry_map or {}).get(r["symbol"])
        if r.get("tradable") and ind:
            crowd.setdefault(ind, []).append(r["symbol"])
    summary = {"as_of": as_of.isoformat(), "candidates": len(ranked), "opened": len(result.cards),
               "watched": len(result.watch), "by_source": by_source,
               "crowding": {k: v for k, v in sorted(crowd.items(), key=lambda kv: -len(kv[1])) if len(v) >= 3},
               "weights": {s: {k: w.get(k) for k in ("weight", "target_weight", "tier", "status", "risk_pct", "reason")}
                           for s, w in weights.items()}}
    return DeskOutput(result=result, ranked=ranked, weights=weights, summary=summary, card_failures=failures)
