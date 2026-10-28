"""机会页总览 (批次① 2026-10-05): 只读地回答“现在能不能出手、卡在哪”。

不写任何文件、不触发扫描/评估/收费/下单。所有判定复用交易链同一套规则:
- 来源许可: decision_scan_job.source_permission (每次重读登记与证据)
- 卡生命周期: OpportunityService.all_cards (冻结卡 + 台账事件)
- 入场窗: 与 advance_lifecycle 同口径 (创建日次日起 watchlist_promotion_days 个交易日)
- 账户容量: Ledger 快照 + sizing.size_position / downsize_for_cash (与交易员同一算式)

“可执行”只表示按现行规则此刻没有被许可挡住, 不等于此刻一定成交 (还要报价进区、开盘未越界、
财报/公司行动/熔断等交易环保护)。页面据此展示, 不据此下单。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard

logger = logging.getLogger(__name__)

LIVE_STATES = (CardState.OPEN, CardState.ACTIVE, CardState.WATCH)
RR_SUSPECT = 10.0  # RR 超过这个值先怀疑价位退化 (分拆/并购钉价/复权), 不当成好机会

STATE_LABEL = {"open": "待入场", "active": "监控中", "watch": "观察", "filled": "已入场", "closed": "已平仓",
               "expired": "已失效", "rejected": "已否决"}


def _window(card: OpportunityCard, today: date, days: int) -> dict:
    """入场窗 (与 advance_lifecycle 同口径): 创建日次日起第 1..days 个交易日, 第 days 个交易日收盘后结束。"""
    start = card.created_date + timedelta(days=1)
    elapsed = len(us_clock.trading_days_between(start, today))
    ahead = us_clock.trading_days_between(start, card.created_date + timedelta(days=days * 2 + 10))
    last = ahead[days - 1] if len(ahead) >= days else None
    is_open = elapsed < days or (elapsed == days and not _after_close(today))
    today_counts = elapsed >= 1 and us_clock.is_trading_day(today) and not _after_close(today)
    remaining = (days if elapsed == 0 else days - elapsed + (1 if today_counts else 0)) if is_open else 0
    return {"elapsed": elapsed, "remaining": max(remaining, 0), "last_day": last.isoformat() if last else None,
            "open": is_open}


def _after_close(today: date) -> bool:
    now = us_clock.now()
    return now.date() > today or (now.date() == today and now.time() >= us_clock.sessions[-1].close)


def _account(loop, data_dir: Path):
    """运行中复用唯一 Ledger 的快照; 未启动时只读重放。返回 (ledger, account) 或 (None, None)。"""
    from app.ledger.core import Account, Ledger
    ledger_dir = Path(data_dir) / "ledger"
    if not (ledger_dir / "account.json").exists():
        return None, None
    if loop is not None and getattr(loop, "_boot", False):
        return loop.ledger, loop.account
    account = Account.from_file(ledger_dir / "account.json")
    lg = Ledger(data_dir=ledger_dir, read_only=True)
    lg.replay(account, persist=False)
    return lg, account


def _marks(repo, ledger) -> tuple[dict, dict]:
    """估值: 优先 5 秒热报价 (TickFlow), 否则最近收盘。返回 (marks, 每只的出处)。"""
    marks, origin = {}, {}
    symbols = [p.symbol for p in ledger.open_positions]
    try:
        from app.api.portfolio import _live_marks
        for s, (price, ts, src) in _live_marks(symbols).items():
            marks[s], origin[s] = float(price), {"source": src, "ts": ts}
    except Exception:  # noqa: BLE001
        pass
    today = us_clock.today()
    for s in symbols:
        if s in marks:
            continue
        try:
            df = repo.get_daily(s, today - timedelta(days=10), today)
            if not df.is_empty():
                marks[s], origin[s] = float(df["close"][-1]), {"source": "close", "date": str(df["date"][-1])}
        except Exception:  # noqa: BLE001
            continue
    return marks, origin


def account_capacity(repo, loop, data_dir: Path) -> dict | None:
    lg, account = _account(loop, data_dir)
    if lg is None:
        return None
    marks, origin = _marks(repo, lg)
    with lg._lock:
        positions = list(lg.open_positions)
        nav = lg.nav(marks)
        bp = lg.buying_power(marks)
        risk = lg.open_risk()
        cash = lg.cash
    return {"nav": round(nav, 2), "cash": round(cash, 2), "buying_power": round(bp, 2),
            "positions": len(positions), "max_positions": account.max_positions,
            "open_risk": round(risk, 2), "open_risk_pct": round(risk / nav * 100, 2) if nav > 0 else None,
            "max_portfolio_risk_pct": round(account.max_portfolio_risk_pct * 100, 2),
            "risk_per_trade_pct": round(account.risk_per_trade_pct * 100, 2),
            "marks_origin": sorted({o["source"] for o in origin.values()}) or None,
            "unmarked": sorted(p.symbol for p in positions if p.symbol not in marks),
            "_account": account, "_nav": nav, "_bp": bp, "_risk": risk, "_positions": len(positions)}


def card_capacity(card: OpportunityCard, cap: dict | None, risk_pct: float) -> dict | None:
    """这张卡按现行仓位算式需要多少钱、现金是否够、不够时能缩到多少 (与交易员同一算式)。"""
    if not cap or card.risk_per_share <= 0:
        return None
    from dataclasses import replace
    from app.trader.sizing import MIN_CASH_DOWNSIZE_RATIO, downsize_for_cash, fee_for, size_position
    account = replace(cap["_account"], risk_per_trade_pct=risk_pct)
    qty = size_position(card, cap["_nav"], account)
    mid = sum(card.entry_zone) / 2
    need = qty * mid + fee_for(qty, account) if qty > 0 else 0.0
    out = {"planned_qty": qty, "need_cash": round(need, 2), "risk_pct": round(risk_pct * 100, 2)}
    if qty <= 0:
        out["verdict"] = "zero_size"
    elif cap["_positions"] >= account.max_positions:
        out["verdict"] = "max_positions"
    elif need <= cap["_bp"]:
        out["verdict"] = "ok"
    else:
        n = downsize_for_cash(qty, mid, cap["_bp"], account)
        out["downsized_qty"] = n
        out["verdict"] = "downsized" if n > 0 and n >= qty * MIN_CASH_DOWNSIZE_RATIO else "cash"
    final_qty = out.get("downsized_qty", qty) if out["verdict"] == "downsized" else qty
    new_risk = final_qty * card.risk_per_share
    if out["verdict"] in ("ok", "downsized") and cap["_risk"] + new_risk > cap["_nav"] * account.max_portfolio_risk_pct:
        out["verdict"] = "portfolio_risk"
    return out


def _blocker_text(code: str, names: dict) -> str:
    if code.startswith("source_not_tradable:"):
        sid = code.split(":", 1)[1]
        return f"来源「{names.get(sid, sid)}」当前不可交易"
    return {"direction_conflict": "多空来源冲突", "risk_zero": "单笔风险为零", "explore_daily_quota": "今日探索额度已用",
            "watchlist_removed": "已移出自选（自选通道入选的卡不再新开）", "not_in_watchlist": "非自选", "borrow_unavailable": "不可借券",
            "daily_open_cap": "今日开仓已达上限", "sector_side_cap": "同板块同方向已达上限",
            "sector_unknown": "板块未知, 不开",
            "circuit_breaker_unavailable": "熔断评估出错, 暂停新开仓"}.get(code, code)


VERDICT_TEXT = {"ok": "现金够", "downsized": "现金不足，将缩量成交", "cash": "现金不足（缩量后不到计划 25%，不成交）",
                "max_positions": "持仓数已满", "portfolio_risk": "组合风险超限", "zero_size": "仓位为零"}


def explain(card: dict, names: dict) -> str:
    """确定性的一句话 (不调用 AI): 谁选中 → 证据 → 卡在哪 → 入场窗。"""
    d = (card.get("evidence") or {}).get("decision") or {}
    srcs = d.get("sources") or [card.get("setup_type", "")]
    who = "、".join(names.get(s, s) for s in srcs[:3]) + (f" 等 {len(srcs)} 个来源" if len(srcs) > 3 else "")
    perm = card.get("permission") or {}
    ev = {"proven": "证据为正", "explore": "探索额度", "negative": "证据为负"}.get(perm.get("tier"), "证据未知")
    parts = [f"{who}选中", ev if perm.get("tradable") else f"{ev}，当前不可交易"]
    if card.get("blockers"):
        parts.append("卡在：" + "、".join(card["blockers"]))
    capacity = card.get("capacity") or {}
    if capacity.get("verdict") == "downsized":
        parts.append(f"现金不足，入区时将缩量成交（约 {capacity.get('downsized_qty')} 股，计划 {capacity.get('planned_qty')} 股）")
    win = card.get("window") or {}
    if win.get("open") and win.get("remaining") is not None:
        parts.append(f"入场窗剩 {win['remaining']} 个交易日")
    return "；".join(parts)


def _names(engine) -> dict:
    names = {"breakout": "S1 突破", "pullback": "S2 回踩"}
    try:
        from app.services import phantom_4h_source as p4
        names[p4.SOURCE_ID], names[p4.SHORT_SOURCE_ID] = p4.SOURCE_NAME, p4.SHORT_SOURCE_NAME
        names[p4.PINE_SOURCE_ID] = p4.PINE_SOURCE_NAME
    except Exception:  # noqa: BLE001
        pass
    if engine is not None:
        try:
            names.update({m["id"]: m.get("name", m["id"]) for m in engine.list_strategies()})
        except Exception:  # noqa: BLE001
            pass
    return names


def build(*, svc, repo, loop=None, engine=None, data_dir: Path) -> dict:
    """GET /api/opportunities/desk/overview 的全部内容。"""
    from app.opportunity.engine import load_params
    from app.services import decision_desk as dd
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation

    today = us_clock.today()
    params = load_params()
    window_days = params["portfolio"]["watchlist_promotion_days"]
    names = _names(engine)
    cap = account_capacity(repo, loop, data_dir)
    perms: dict[str, dict] = {}

    def perm(src: str) -> dict:
        if src not in perms:
            p = job.source_permission(data_dir, src)
            perms[src] = {k: p.get(k) for k in ("status", "tier", "weight", "risk_pct", "tradable", "reason")}
        return perms[src]

    try:
        from app.services import watchlist as _wl
        watch_now = {r["symbol"] for r in _wl.list_symbols() if r.get("symbol")}
        watch_ok = True
    except Exception:  # noqa: BLE001
        watch_now, watch_ok = set(), False

    cards = svc.all_cards(today)
    v4_mode = strategy_evaluation.evidence_mode(data_dir) == "v4"
    lg, _ = _account(loop, data_dir)
    held = {p.symbol for p in (lg.open_positions if lg is not None else [])}
    # 行业占用 / 冷却: 与 advance_lifecycle 同口径。生命周期按“非观察卡先处理”, 当前不可交易的
    # OPEN/ACTIVE 卡会先被准入守卫打回观察并释放行业名额, 所以这里只计 已成交 + 当前可交易的 OPEN/ACTIVE。
    sector_map = {}
    try:
        sector_map = svc.sector_map()
    except Exception:  # noqa: BLE001
        pass
    sector_max = params["portfolio"]["sector_max_active"]
    occupied: dict[str, int] = {}
    active_syms: set[str] = set()
    for c in cards:
        live_trade = c.state == CardState.FILLED or (c.state in (CardState.OPEN, CardState.ACTIVE)
                                                     and perm(dd.source_of(c.setup_type)).get("tradable"))
        if live_trade:
            active_syms.add(c.symbol)
            sec = sector_map.get(c.symbol) or (c.evidence or {}).get("sector")
            if sec:
                occupied[sec] = occupied.get(sec, 0) + 1
    try:
        cooldown = svc.cooldown_symbols(today)
    except Exception:  # noqa: BLE001
        cooldown = set()
    executable, live_other = [], []
    for c in cards:
        if c.state not in LIVE_STATES:
            continue
        win = _window(c, today, window_days)
        if not win["open"]:
            continue
        src = dd.source_of(c.setup_type)
        p = perm(src)
        dec = (c.evidence or {}).get("decision") or {}
        blockers = []
        for code in dec.get("hard_blockers") or []:
            blockers.append(_blocker_text(code if code != "source_not_tradable" else f"source_not_tradable:{src}", names))
        if not p.get("tradable"):
            blockers.append(_blocker_text(f"source_not_tradable:{src}", names))
        if v4_mode and (not watch_ok or c.symbol not in watch_now or dec.get("tab") != "watchlist"):
            blockers.append(_blocker_text("not_in_watchlist", names))   # 与 entry_guard v4 一致
        elif dec.get("lane_only") and (not watch_ok or c.symbol not in watch_now):
            blockers.append(_blocker_text("watchlist_removed", names))  # 读不到名单与交易环一致: 禁入
        risk_pct = min(x for x in (dec.get("risk_pct"), p.get("risk_pct")) if isinstance(x, (int, float)) and x > 0) \
            if any(isinstance(x, (int, float)) and x > 0 for x in (dec.get("risk_pct"), p.get("risk_pct"))) else 0.0
        if p.get("tier") == "explore" or dec.get("lane_only"):
            risk_pct = min(risk_pct or dd.EXPLORE_RISK_PCT, dd.EXPLORE_RISK_PCT)
        if c.state == CardState.WATCH and p.get("tradable"):
            sec = sector_map.get(c.symbol) or (c.evidence or {}).get("sector")
            if c.symbol in held:
                blockers.append("已持有该股")
            elif c.symbol in active_syms:
                blockers.append("同一股票已有进行中的卡")
            elif c.symbol in cooldown:
                blockers.append("平仓冷却期内")
            elif not sec:
                blockers.append("行业尚未核实")
            elif occupied.get(sec, 0) >= sector_max:
                blockers.append(f"行业 {sec} 已有 {occupied[sec]} 张进行中（上限 {sector_max}）")
        capacity = card_capacity(c, cap, risk_pct) if not blockers and risk_pct > 0 else None
        if capacity and capacity["verdict"] not in ("ok", "downsized"):
            blockers.append(VERDICT_TEXT[capacity["verdict"]])
        row = {**c.to_dict(), "window": win, "permission": p, "blockers": blockers, "capacity": capacity,
               "in_watchlist": c.symbol in watch_now, "held": c.symbol in held,
               "rr_suspect": (c.reward_risk or 0) > RR_SUSPECT}
        row["explain"] = explain(row, names)
        (executable if p.get("tradable") else live_other).append(row)
    executable.sort(key=lambda r: (bool(r["blockers"]), r["state"] == "watch", -(r["permission"].get("weight") or 0),
                                   r["created_date"], r["symbol"]))

    # 状态条
    ev = strategy_evaluation.load_evaluation(data_dir) or {}
    tradable_sources = sorted(s for s in set(ev.get("sources", {})) | set(dd.BUILTIN_SETUPS) if perm(s).get("tradable"))
    latest = job.latest_run(data_dir) or {}
    summary = latest.get("summary") or {}
    st = loop.status() if loop is not None else {}
    cb = st.get("circuit_breaker")
    days = svc.list_card_days()
    gate = (svc.load_cards(date.fromisoformat(days[-1])) or {}).get("gate") if days else None
    status = {
        "today": today.isoformat(), "in_session": us_clock.in_session(), "latest_scan": summary.get("as_of") or (days[-1] if days else None),
        "scan_generated_at": summary.get("generated_at"), "gate": gate,
        "tradable_sources": [{"id": s, "name": names.get(s, s), "weight": perm(s).get("weight"), "tier": perm(s).get("tier")}
                             for s in tradable_sources],
        "evaluated_sources": len(ev.get("sources", {})),
        "executable": len(executable), "executable_clear": sum(1 for r in executable if not r["blockers"]),
        "circuit_breaker": cb if cb else None,  # None = 本次进程尚未评估 (周末/重启后), 不是“正常”
        "last_quote_at": st.get("last_quote_at"), "last_run_at": st.get("last_run_at"),
        "entry_blockers": (st.get("entry_blockers") or [])[-20:],
        "crowding": summary.get("crowding") or {},
        "account": {k: v for k, v in (cap or {}).items() if not k.startswith("_")} or None,
    }
    return {"status": status, "executable": executable, "live_other_count": len(live_other),
            "watchlist": watchlist_table(svc, data_dir, today, watch_now, watch_ok, cards, latest, names, perm, held),
            "positions": positions_actions(repo, loop, data_dir, today, names),
            "events": event_radar(data_dir, today, cards, held, watch_now),
            "evidence": evidence_brief(data_dir),
            "names": names}


def positions_actions(repo, loop, data_dir: Path, today: date, names: dict) -> list[dict]:
    """持仓 + 下一步动作 (白话): 待执行离场 / 第几天到期 / 距止损与目标 / 来源当前许可。只读。"""
    from app.services import decision_desk as dd
    lg, account = _account(loop, data_dir)
    if lg is None:
        return []
    marks, origin = _marks(repo, lg)
    events = lg.read_events()
    plans: dict[str, dict] = {}
    pending_exit: dict[str, dict] = {}
    for e in events:
        sym = e.get("symbol")
        if e.get("kind") == "fill" and e.get("side") in ("buy", "short"):
            plans[sym] = e
            pending_exit.pop(sym, None)
        elif e.get("kind") == "exit_intent":
            pending_exit[sym] = e
        elif e.get("kind") in ("close",) and sym in pending_exit:
            pending_exit.pop(sym, None)
    out = []
    for p in lg.open_positions:
        sym = p.symbol
        plan = plans.get(sym) or {}   # fill 事件顶层即冻结的计划字段 (setup_type/target_zone/risk_per_share/…)
        setup = plan.get("setup_type") or ""
        price = marks.get(sym)
        short = p.qty < 0
        entry_day = date.fromisoformat(p.entry_date) if getattr(p, "entry_date", None) else None
        held_days = us_clock.days_held(entry_day, today) if entry_day else None
        max_days = plan.get("max_holding_days") or account.max_holding_days
        stop = p.stop_price
        target = plan.get("target_zone")
        rps = plan.get("risk_per_share")
        move = None if price is None else ((p.avg_cost - price) if short else (price - p.avg_cost))
        if sym in pending_exit:
            e = pending_exit[sym]
            action = f"待离场：{e.get('trade_date')} 收盘触发{_EXIT_LABEL.get(e.get('reason'), e.get('reason') or '')}，下一交易时段按报价卖出"
            urgency = "high"
        elif held_days is not None and max_days and held_days >= max_days:
            action, urgency = "持有已到期：今天收盘前时间止损离场", "high"
        elif held_days is not None and max_days and held_days == max_days - 1:
            action, urgency = "明天是最后持有日", "mid"
        else:
            action, urgency = "持有中：按止损/目标自动管理", "low"
        src = dd.source_of(setup) if setup else None
        out.append({"symbol": sym, "side": "short" if short else "long", "qty": abs(p.qty), "avg_cost": round(p.avg_cost, 4),
                    "last": round(price, 4) if price is not None else None, "mark_source": (origin.get(sym) or {}).get("source"),
                    "r_now": round(move / rps, 2) if (move is not None and rps) else None,
                    "stop": stop, "stop_dist_pct": round((stop / price - 1) * 100, 2) if (stop and price) else None,
                    "target": target, "target_dist_pct": round(((target[1] if short else target[0]) / price - 1) * 100, 2)
                    if (target and price) else None,
                    "held_days": held_days, "max_days": max_days, "source": src,
                    "source_name": names.get(src, src) if src else None, "action": action, "urgency": urgency})
    out.sort(key=lambda r: ({"high": 0, "mid": 1}.get(r["urgency"], 2), r["symbol"]))
    return out


_EXIT_LABEL = {"trailing_stop": "移动止损", "invalidation": "失效止损", "time_stop": "时间止损",
               "breakeven_after_partial": "保本止损", "watchlist_removed": "已移出自选"}


def event_radar(data_dir: Path, today: date, cards: list, held: set, watch_now: set) -> dict:
    """事件雷达: 盘前跳空 (HL) + 未来 7 天财报 (持仓/在窗卡/自选)。只读本地缓存, 不触发任何收费调用。"""
    out: dict = {"premarket": None, "earnings": [], "earnings_source": None}
    try:
        from app.services import premarket_gap
        pm = premarket_gap.latest(data_dir)
        if pm:
            out["premarket"] = {"date": pm.get("date") or pm.get("as_of"), "alerts": (pm.get("alerts") or [])[:20],
                                "generated_at": pm.get("generated_at")}
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.earnings_calendar import EarningsCalendar
        cal = EarningsCalendar(data_dir)
        live = {c.symbol for c in cards if c.state.value in ("watch", "active", "open")} if cards else set()
        watch_syms = set(held) | live | set(watch_now)
        horizon = today + timedelta(days=7)
        for sym in sorted(watch_syms):
            ev = cal.event_for(sym)
            if not ev:
                continue
            day = ev.get("date")
            try:
                d = date.fromisoformat(str(day))
            except ValueError:
                continue
            if today <= d <= horizon:
                roles = [r for r, ok in (("持仓", sym in held), ("在窗卡", sym in live), ("自选", sym in watch_now)) if ok]
                out["earnings"].append({"symbol": sym, "date": d.isoformat(), "timing": ev.get("timing"), "roles": roles})
        out["earnings"].sort(key=lambda r: (r["date"], r["symbol"]))
        out["earnings_source"] = "nasdaq 日历缓存"
    except Exception:  # noqa: BLE001
        pass
    return out


def evidence_brief(data_dir: Path) -> dict:
    """状态条用的证据摘要: 现行口径、v3 生成时间、前向确认进度 (候选来源)。"""
    from app.services import strategy_evaluation
    out = {"mode": strategy_evaluation.evidence_mode(data_dir), "v3_generated_at": None, "forward": []}
    try:
        v3 = strategy_evaluation.load_evaluation_file(data_dir, "v3")
        if v3:
            from app.services import forward_confirmation
            out["v3_generated_at"] = v3.get("generated_at")
            fwd = forward_confirmation.status(data_dir, v3)
            out["forward"] = [{"source": k, **{x: v.get(x) for x in ("n", "months", "p", "threshold", "promoted",
                                                                       "progress_text")}}
                              for k, v in fwd.items() if v.get("candidate")]
            out["forward_min_n"] = forward_confirmation.MIN_N
    except Exception:  # noqa: BLE001
        pass
    return out


def watchlist_table(svc, data_dir, today, members, ok, cards, latest, names, perm, held) -> dict:
    """自选总表: 当前名单一票一行 (名单变化即时反映); 原因来自最近一次扫描的逐票漏斗。"""
    funnel = latest.get("funnel") or {}
    snapshot = set((latest.get("summary") or {}).get("watchlist_snapshot") or [])
    scan_day = (latest.get("summary") or {}).get("as_of")
    by_symbol: dict[str, list[OpportunityCard]] = {}
    for c in cards:
        by_symbol.setdefault(c.symbol, []).append(c)
    rows = []
    for sym in sorted(members):
        own = sorted(by_symbol.get(sym, []), key=lambda c: (c.created_date, c.id), reverse=True)
        live = next((c for c in own if c.state in (CardState.OPEN, CardState.ACTIVE, CardState.WATCH, CardState.FILLED)), None)
        f = funnel.get(sym)
        row = {"symbol": sym, "held": sym in held, "scan_day": scan_day,
               "added_after_scan": bool(snapshot) and sym not in snapshot,
               "card": None, "stage": None, "reason": None, "hits": []}
        if live is not None:
            dec = (live.evidence or {}).get("decision") or {}
            row["card"] = {"id": live.id, "state": str(live.state), "state_label": STATE_LABEL.get(str(live.state)),
                           "created_date": live.created_date.isoformat(), "setup_type": live.setup_type,
                           "entry_zone": list(live.entry_zone), "state_reason": live.state_reason,
                           "tradable": perm(_src(live.setup_type)).get("tradable"), "sources": dec.get("sources")}
        if f:
            row.update(stage=f.get("stage"), reason=f.get("reason"), hits=f.get("hits") or [],
                       card_failures=f.get("card_failures"))
        elif row["added_after_scan"]:
            row.update(stage="pending", reason="最近一次扫描之后才加入自选，下次盘后扫描纳入")
        else:
            row.update(stage="unrecorded", reason="该次扫描未记录逐票原因（诊断上线前的扫描）")
        rows.append(row)
    removed = sorted(snapshot - set(members))
    return {"ok": ok, "count": len(rows), "rows": rows, "scan_day": scan_day, "removed_since_scan": removed}


def _src(setup_type: str) -> str:
    from app.services import decision_desk as dd
    return dd.source_of(setup_type)


def to_json_safe(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(v) for v in obj]
    return obj


__all__ = ["build", "account_capacity", "card_capacity", "explain", "to_json_safe", "json"]
