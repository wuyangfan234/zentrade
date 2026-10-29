"""机会页 v4 = 自选表 (2026-10-06)。只读: 读当日/近日自选信号表 + 当前规则集 + 前向确认 + 台账 + 热报价, 不写任何文件。

一行一只当前自选股。列: 代码 现价 信号 触发 失效 目标 股数 同票前向 盘前 持仓。
- 信号: 最近一次盘后扫描起仍在入场窗内 (默认 5 个交易日) 的、当前规则集勾选的规则 +1; 同票多条取同票前向最好的一条给几何,
  其余规则名附在后面 (最多 2 个名字)。
- 股数: 按现行仓位算式 (风险额 / 每股风险, 名义额 ≤25% 净值) —— 只是计划股数, 是否下单由前向确认决定 (tradable 字段)。
- 同票前向: 该规则在该票上前向已结算影子卡的笔数与均 R。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)


def _names(engine) -> dict:
    names = {"breakout": "S1 突破", "pullback": "S2 回踩", "phantom_4h": "Phantom 4h", "phantom_4h_pine": "Phantom 4h Pine",
             "phantom_4h_short": "Phantom 4h 空"}
    if engine is not None:
        try:
            names.update({m["id"]: m.get("name", m["id"]) for m in engine.list_strategies()})
        except Exception:  # noqa: BLE001
            pass
    return names


def rule_catalog(engine) -> list[dict]:
    """可勾选的规则: 引擎全部可运行的日线股票策略 (含自定义/挖掘发布, 含 10 个 TradingView 移植) + S1/S2 +
    Phantom 4h 多 (简化扫描版与 Pine 原版)。"""
    out = [{"id": "breakout", "name": "S1 突破", "kind": "builtin"}, {"id": "pullback", "name": "S2 回踩", "kind": "builtin"}]
    from app import optional
    if optional.available("app.services.phantom_4h_source"):   # 可选来源: 开源核心版不含
        out += [{"id": "phantom_4h", "name": "Phantom 4h", "kind": "ported"},
                {"id": "phantom_4h_pine", "name": "Phantom 4h Pine", "kind": "ported"}]
    if engine is not None:
        try:
            for m in engine.list_strategies():
                if m.get("research_only") or "stock" not in m.get("asset_types", ["stock"]) or "1d" not in m.get("timeframes", ["1d"]):
                    continue
                out.append({"id": m["id"], "name": m.get("name", m["id"]), "kind": m.get("source") or "strategy"})
        except Exception:  # noqa: BLE001
            pass
    return out


def _signal_days(data_dir: Path, today: date, window: int) -> list[dict]:
    """最近 window 个交易日 (含今天之前最近一次扫描) 的自选信号表, 新的在前。"""
    from app.services import watchlist_v4 as wv
    out = []
    for day, f in reversed(wv.signal_files(data_dir)):
        if day > today:
            continue
        elapsed = len(us_clock.trading_days_between(day + timedelta(days=1), today))
        if elapsed > window:
            break
        payload = wv._read(f)
        if isinstance(payload, dict) and payload.get("backfill"):
            continue        # 机会卡补算的信号只给机会页卡片用, 不进自选表/盘前推送 (保持原行为)
        if isinstance(payload, dict):
            payload["_elapsed"] = elapsed
            out.append(payload)
    return out


def build(*, data_dir: Path, repo, loop=None, engine=None) -> dict:
    from app.services import desk_overview as ov
    from app.services import watchlist_v4 as wv
    from app.services import decision_scan_job as job
    from app.trader.sizing import size_position

    data_dir = Path(data_dir)
    today = us_clock.today()
    names = _names(engine)
    from app.services import strategy_evaluation
    perm = wv.permission(data_dir)
    rs, st = perm["ruleset"], perm["status"]
    risk_pct = wv.risk_pct_of(rs)          # 计划股数用 (展示); None = 规则集非法 → 不算股数
    from app.opportunity.engine import load_params
    window = int(load_params()["portfolio"]["watchlist_promotion_days"])
    v4_on = strategy_evaluation.evidence_mode(data_dir) == "v4"
    tradable_rules = perm["tradable"] if v4_on else set()
    enabled = set(x for x in rs.get("enabled") or [] if isinstance(x, str))
    watch = sorted(job._watchlist())
    days = _signal_days(data_dir, today, window)
    latest = days[0] if days else None

    # 账户 / 持仓 / 现价
    lg, account = ov._account(loop, data_dir)
    marks, origin = ov._marks(repo, lg) if lg is not None else ({}, {})
    positions = {p.symbol: p for p in (lg.open_positions if lg is not None else [])}
    plans = {}
    if lg is not None:
        for e in lg.read_events():
            if e.get("kind") == "fill" and e.get("side") in ("buy", "short"):
                plans[e["symbol"]] = e
    nav = lg.nav(marks) if lg is not None else None
    live = {}
    try:
        from app.api.portfolio import _live_marks
        live = {s: v[0] for s, v in _live_marks(watch).items()}
    except Exception:  # noqa: BLE001
        pass
    closes = {k: v for k, v in ((latest or {}).get("closes") or {}).items() if v}
    missing = [s for s in watch if s not in live and s not in closes]
    if missing and repo is not None:   # 热报价暂停 (同步期间) 且尚无当日信号表: 用日K最近收盘兜底
        try:
            import polars as pl
            df = repo.get_daily_batch(missing, today - timedelta(days=10), today, columns=["symbol", "date", "close"])
            if df is not None and not df.is_empty():
                last = df.sort("date").group_by("symbol").agg(pl.col("close").last())
                closes.update({r["symbol"]: float(r["close"]) for r in last.iter_rows(named=True) if r["close"]})
        except Exception:  # noqa: BLE001
            pass

    # 盘前 (当天 09:00 ET 之后才有)
    pm = {}
    try:
        from app.services import premarket_gap
        g = premarket_gap.latest(data_dir) or {}
        if str(g.get("day")) == today.isoformat():
            pm = {k: v for k, v in (g.get("gaps") or {}).items()}
            for a in g.get("alerts") or []:
                pm.setdefault(a["symbol"], a.get("gap_pct"))
    except Exception:  # noqa: BLE001
        pass
    # 财报 (7 个自然日内)
    earn, earn_block = {}, set()
    try:
        from app.services import earnings_guard
        from app.services.earnings_calendar import EarningsCalendar
        cal = EarningsCalendar(data_dir)
        for s in watch:
            ev = cal.event_for(s)
            if ev:
                d = date.fromisoformat(str(ev.get("date"))[:10])
                if today <= d <= today + timedelta(days=7):
                    earn[s] = {"date": d.isoformat(), "timing": ev.get("timing")}
                if earnings_guard.entry_blocked(ev, today):
                    earn_block.add(s)
    except Exception:  # noqa: BLE001
        pass

    per_sym_rule = st.get("per_symbol_rule") or {}
    per_rule = st.get("per_rule") or {}
    rows = []
    for sym in watch:
        price = live.get(sym) or marks.get(sym) or closes.get(sym)
        cands = []
        for payload in days:
            for x in payload.get("signals") or []:
                if x["symbol"] != sym or x["rule"] not in enabled or x.get("side") == "short":
                    continue
                cands.append({**x, "as_of": payload["as_of"], "elapsed": payload["_elapsed"]})
        row = {"symbol": sym, "price": round(float(price), 4) if price else None, "price_source": "live" if sym in live else "close",
               "signal": None, "rules": [], "trigger": None, "invalidation": None, "target": None, "qty": None,
               "dist_pct": None, "forward": None, "tradable": False, "premarket_pct": pm.get(sym),
               "earnings": earn.get(sym), "position": None}
        if cands:
            from app.services import decision_desk as dd

            def fwd_key(c):
                # 与交易员选卡同序 (decision_scan: 可下单优先, 再按规则权重 = 前向超额 × 收缩), 再取最新的一天
                f = per_rule.get(c["rule"]) or {}
                ex, n = f.get("excess") or 0.0, f.get("n") or 0
                w = max(ex, 0.0001) * n / (n + dd.SHRINK_K) if c["rule"] in tradable_rules else 0.0
                ok = wv.rule_permission(perm, c["rule"], "long")[0] if v4_on else False
                return (not ok, c["rule"] not in tradable_rules, -w, c["elapsed"], c["rule"])
            cands.sort(key=fwd_key)
            best = cands[0]
            seen = []
            for c in cands:
                if c["rule"] not in seen:
                    seen.append(c["rule"])
            row["rules"] = [{"id": r, "name": names.get(r, r)} for r in seen]
            row["signal"] = {"rule": best["rule"], "name": names.get(best["rule"], best["rule"]), "as_of": best["as_of"],
                             "day": best["elapsed"] + 1, "window": window}
            row["trigger"] = best["entry_zone"][1]
            row["entry_zone"] = best["entry_zone"]
            row["invalidation"] = best["invalidation"]
            row["target"] = best["target_zone"][0]
            if price:
                row["dist_pct"] = round((row["trigger"] / float(price) - 1) * 100, 2)
            if nav and account is not None and best["risk"] > 0 and risk_pct:
                from dataclasses import replace
                from app.opportunity.card import OpportunityCard
                card = OpportunityCard(id="x", symbol=sym, setup_type="x", created_date=today, entry_zone=tuple(best["entry_zone"]),
                                       invalidation=best["invalidation"], target_zone=tuple(best["target_zone"]),
                                       risk_per_share=best["risk"], reward_risk=best["rr"])
                acc = replace(account, risk_per_trade_pct=risk_pct / 100)
                row["qty"] = size_position(card, nav, acc)
            f = per_sym_rule.get(f"{sym}|{best['rule']}")
            row["forward"] = {"n": f["n"], "mean_r": f["mean_r"]} if f else None
            ok, rule_risk, tier = wv.rule_permission(perm, best["rule"], "long") if v4_on else (False, 0.0, "none")
            row["tradable"] = ok and sym not in earn_block
            row["tier"] = tier if ok else None
            if ok and nav and account is not None and best["risk"] > 0:
                from dataclasses import replace as _rp
                from app.opportunity.card import OpportunityCard as _OC
                row["qty"] = size_position(_OC(id="x", symbol=sym, setup_type="x", created_date=today,
                                               entry_zone=tuple(best["entry_zone"]), invalidation=best["invalidation"],
                                               target_zone=tuple(best["target_zone"]), risk_per_share=best["risk"],
                                               reward_risk=best["rr"]), nav, _rp(account, risk_per_trade_pct=rule_risk))
            gap = pm.get(sym)
            if isinstance(gap, (int, float)) and row["trigger"] and closes.get(sym):
                row["premarket_void"] = bool(closes[sym] * (1 + gap / 100) > row["trigger"] * 1.02)
        p = positions.get(sym)
        if p is not None:
            plan = plans.get(sym) or {}
            entry_day = date.fromisoformat(p.entry_date) if p.entry_date else None
            rps = plan.get("risk_per_share")
            mv = ((float(price) - p.avg_cost) * (1 if p.qty > 0 else -1)) if price else None
            row["position"] = {"qty": abs(p.qty), "avg_cost": round(p.avg_cost, 4), "stop": p.stop_price,
                               "target": (plan.get("target_zone") or [None])[0],
                               "day": us_clock.days_held(entry_day, today) if entry_day else None,
                               "max_days": plan.get("max_holding_days") or (account.max_holding_days if account else 7),
                               "r_now": round(mv / rps, 2) if (mv is not None and rps) else None}
        rows.append(row)
    rows.sort(key=lambda r: (r["position"] is None, r["signal"] is None,
                             abs(r["dist_pct"]) if r["dist_pct"] is not None else 99, r["symbol"]))
    others = sorted(set(positions) - set(watch))
    for sym in others:
        p = positions[sym]
        plan = plans.get(sym) or {}
        price = marks.get(sym)
        entry_day = date.fromisoformat(p.entry_date) if p.entry_date else None
        rps = plan.get("risk_per_share")
        rows.insert(0, {"symbol": sym, "price": round(float(price), 4) if price else None, "price_source": (origin.get(sym) or {}).get("source"),
                        "signal": None, "rules": [], "trigger": None, "invalidation": None, "target": None, "qty": None,
                        "dist_pct": None, "forward": None, "tradable": False, "premarket_pct": pm.get(sym), "earnings": None,
                        "outside_watchlist": True,
                        "position": {"qty": abs(p.qty), "avg_cost": round(p.avg_cost, 4), "stop": p.stop_price,
                                     "target": (plan.get("target_zone") or [None])[0],
                                     "day": us_clock.days_held(entry_day, today) if entry_day else None,
                                     "max_days": plan.get("max_holding_days") or 7,
                                     "r_now": round((float(price) - p.avg_cost) * (1 if p.qty > 0 else -1) / rps, 2)
                                     if (price and rps) else None}})
    return {"as_of": (latest or {}).get("as_of"), "generated_at": (latest or {}).get("generated_at"),
            "mode": "v4" if v4_on else "off",
            "ruleset": {"version": rs.get("version"), "enabled": sorted(enabled), "invalid": bool(rs.get("invalid"))},
            "forward": {k: st.get(k) for k in ("n", "months", "excess", "p", "threshold", "promoted")},
            "rows": rows}
