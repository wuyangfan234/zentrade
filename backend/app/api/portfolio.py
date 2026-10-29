"""持仓/台账 API — /api/portfolio (P3)。

端点:
  GET /api/portfolio/overview   净值/现金/持仓/风险/事件/净值曲线
复用运行台账的一致快照；未启动时只读重放，不在 GET 路径写物化文件。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Request

from app.ledger.core import CONTRACT_MARGIN_RATE, Account, Ledger, position_value
from app.market_clock import us_clock
from app.review.review import RoundTripState, build_round_states

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


MARK_LOOKBACK_DAYS = 10  # 覆盖长周末/休市; 更久无收盘则按成本价


def _ledger_dir(request: Request) -> Path:
    return Path(request.app.state.repo.store.data_dir) / "ledger"


@router.get("/overview")
def overview(request: Request) -> dict:
    """持仓总览。台账未初始化 → 空仓骨架 (前端 EmptyState)。"""
    ledger_dir = _ledger_dir(request)
    if not (ledger_dir / "account.json").exists():
        return {"cash": 0, "nav": 0, "positions": [], "open_risk": 0,
                "events": [], "nav_daily": [], "note": "台账未初始化"}

    loop = getattr(request.app.state, "trading_loop", None)
    if loop is not None and getattr(loop, "_boot", False):
        lg = loop.ledger
    else:
        lg = Ledger(data_dir=ledger_dir, read_only=True)
        lg.replay(Account.from_file(ledger_dir / "account.json"), persist=False)
    snap = lg.snapshot()

    marks: dict[str, float] = {}
    today = us_clock.today()
    # 估值取最近一个已有收盘: 美东零点后到当日首根日K之间, 只查当天会落空并
    # 回落到成本价, 让净值卡与净值曲线 (按收盘定版) 对不上。
    since = today - timedelta(days=MARK_LOOKBACK_DAYS)
    for pos in snap["positions"]:
        df = request.app.state.repo.get_daily(pos["symbol"], since, today)
        if not df.is_empty():
            marks[pos["symbol"]] = float(df["close"][-1])
    nav = snap["cash"] + sum(
        position_value(p["qty"], marks.get(p["symbol"], p["avg_cost"]), p["avg_cost"], p.get("contract", False))
        for p in snap["positions"]
    )

    return {
        "cash": round(snap["cash"], 2),
        "nav": round(nav, 2),
        "positions": [
            {**p, "avg_cost": round(p["avg_cost"], 4)} for p in snap["positions"]
        ],
        "open_risk": round(snap["open_risk"], 2),
        "events": _ledger_feed(snap["events"]),
        "nav_daily": snap["nav_daily"][-60:],
    }


# 持仓页"台账事件"区只看交易事实; 机会卡状态流水 (kind=opportunity) 每天上百条, 会把成交/平仓挤出窗口
_FEED_EXCLUDED_KINDS = frozenset({"opportunity"})
_FEED_LIMIT = 200


def _ledger_feed(events: list[dict]) -> list[dict]:
    """台账事件区数据: 去掉机会卡状态流水后取最近 _FEED_LIMIT 条 (成交/平仓/止损调整/离场意向等)

    stop_move 补 position_side (该回合开仓方向), 供前端区分多头"止损上移"与空头"止损下移"
    只读展示: 返回副本, 不改台账事件
    """
    side: dict[str, str] = {}
    out: list[dict] = []
    for e in events:
        kind, sym = e.get("kind"), e.get("symbol")
        if kind in _FEED_EXCLUDED_KINDS:
            continue
        if kind == "fill" and sym:
            side[sym] = "short" if e.get("side") == "short" else "long"
        if kind == "stop_move" and sym in side:
            e = {**e, "position_side": side[sym]}
        out.append(e)
    return out[-_FEED_LIMIT:]


# ── /live: 持仓直观数据 + 已平仓 + 汇总 (5 秒刷新; 2026-10-02 产品需求) ─────────

LIVE_REFRESH_SECONDS = 5
EXIT_REASON_LABEL = {
    "target_zone_upper": "到达目标上沿", "target_zone_lower": "到达目标下沿(减半)",
    "invalidation": "跌破失效位", "trailing_stop": "移动止损", "time_stop": "持有到期时间止损",
    "earnings_reduce": "财报前减半", "corp_action": "公司行动", "manual": "人工",
    "watchlist_removed": "移出自选次日开盘平仓",
}


def _live_marks(symbols: list[str]) -> dict[str, tuple[float, int, str]]:
    """最新报价: 优先 5 秒热报价缓冲 (TickFlow); 没有则返回空, 由调用方回落到最近收盘。"""
    from app.services import hot_quotes
    provider = hot_quotes.provider()
    out: dict[str, tuple[float, int, str]] = {}
    if provider is None:
        return out
    ticks = getattr(provider, "_ticks", {})
    lock = getattr(provider, "_tick_lock", None)
    if lock is None:
        return out
    with lock:
        for s in symbols:
            dq = ticks.get(s)
            if dq:
                ts, price, _ = dq[-1]
                out[s] = (price, ts, getattr(provider, "name", "realtime"))
    return out


def _round_trips(events: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """复用 review 的 FIFO/归零/现金流计算，API 只负责原有展示字段。"""
    closed, opens = build_round_states(events)
    return ([_close_trip(state) for state in closed],
            {symbol: _round_plan(state) for symbol, state in opens.items()})


def _stop_history(events: list[dict], plan: dict) -> dict:
    """0.18.0: 初始止损/目标 + 本回合 stop_move 记录, 只从台账事件推算 (不读写任何文件, 不改台账格式)。"""
    seq0 = plan.get("entry_seq")
    current = plan.get("stop_initial")
    moves = []
    for e in events:
        if e.get("kind") != "stop_move" or e.get("symbol") != plan.get("symbol"):
            continue
        if seq0 is None or (e.get("seq") or 0) <= seq0:
            continue
        moves.append({"seq": e.get("seq"), "at": e.get("at"), "trade_date": e.get("trade_date"),
                      "from": current, "stop_price": e.get("stop_price"), "reason": e.get("reason")})
        current = e.get("stop_price")
    return {"stop_initial": plan.get("stop_initial"), "target_initial": plan.get("target_zone"), "stop_moves": moves}


def _round_plan(state: RoundTripState) -> dict:
    """价格与总开仓股数统一到本回合最新拆股单位；原始事件不变。"""
    first = state.first
    factor = state.split_factor
    qty = state.qty_opened * factor
    plan = {key: first.get(key) for key in (
        "symbol", "card_id", "setup_type", "invalidation", "target_zone",
        "risk_per_share", "max_holding_days", "quote_feed", "risk_pct", "entry_tier", "accounting",
    )}
    for key in ("invalidation", "risk_per_share"):
        value = plan[key]
        if isinstance(value, (int, float)):
            plan[key] = value / factor
    if plan["target_zone"]:
        plan["target_zone"] = [price / factor for price in plan["target_zone"]]
    plan.update({
        "side": "short" if state.short else "long", "entry_seq": first.get("seq"),
        "entry_date": first.get("trade_date") or str(first.get("at") or "")[:10] or None,
        "entry_at": first.get("at"), "qty": qty, "cost": state.entry_notional,
        "entry_price": state.entry_notional / qty,
        "fees": state.fees, "exits": state.exits, "stop_price": state.stop_price,
        "stop_initial": first.get("stop_price"),
    })
    return plan


def _close_trip(state: RoundTripState) -> dict:
    plan = _round_plan(state)
    exits = state.exits
    last = exits[-1]
    trip = state.closed(last)
    # Partial exits on both sides of a split must use the same share unit as
    # entry_price/qty; dividend cash never changes either execution price.
    sold_qty = state.exit_qty_basis * state.split_factor
    proceeds = sum(float(x["qty"]) * float(x["price"]) for x in exits)
    exit_price = proceeds / sold_qty if sold_qty else None
    invested = state.entry_notional + state.entry_fees
    reason = last.get("reason") or last.get("exit_reason") or ""
    return {
        "symbol": plan["symbol"], "side": plan["side"], "card_id": plan.get("card_id"),
        "setup_type": plan.get("setup_type"),
        "entry_date": plan.get("entry_date"), "entry_price": round(plan["entry_price"], 4),
        "exit_date": trip.exit_date.isoformat(), "exit_price": round(exit_price, 4) if exit_price else None,
        "qty": plan["qty"], "partial_exits": len(exits) - 1,
        "exit_reason": reason, "exit_reason_label": EXIT_REASON_LABEL.get(reason, reason),
        "days_held": us_clock.days_held(trip.entry_date, trip.exit_date),
        "fees": round(trip.total_fees, 2), "pnl": trip.pnl,
        "return_pct": round(state.pnl / invested * 100, 3) if invested else None,
        "r_multiple": trip.r_multiple,
    }


def _setup_label(setup_type: str | None, side: str, names: dict) -> str | None:
    """持仓来源的展示名: 机会卡同款名称 (引擎 META 名; 看空用 card_replay.SHORT_NAMES) + （多/空）。
    方向取持仓方向; 映射不到 → None (前端回退显示原 setup_type)。只读展示, 不改台账字段。"""
    if not setup_type:
        return None
    from app.services import decision_desk as dd
    from app.services.card_replay import rule_name as _rname
    rule = dd.source_of(setup_type)
    short = side == "short"
    if rule == "swing":                        # 机会板综合卡 (strategy:swing) 没有引擎规则名
        return f"机会板（{'空' if short else '多'}）"  # noqa: RUF001
    name = _rname(names, rule, short)
    if not name or name == rule:
        return None
    return f"{name}（{'空' if short else '多'}）"  # noqa: RUF001


def _scope_of(card_id: str | None) -> str:
    """回合口径: 机会板卡 (卡 id 含 -book-swing-) / 其他一律旧规则 (0.24 前的试用档、决策台、回补成交)。"""
    return "swing" if "-book-swing-" in str(card_id or "") else "legacy"


def _setup_names(request: Request) -> dict:
    from app.services import watchlist_table as wt
    try:
        return wt._names(getattr(request.app.state, "strategy_engine", None))
    except Exception:  # noqa: BLE001 - 名称只用于展示, 失败回退原 id
        return {}


@router.get("/live")
def live(request: Request) -> dict:
    """持仓页: 入场价/现价/浮盈/距止损/距目标/持有天数/来源 + 已平仓 + 汇总。前端每 5 秒刷新。"""
    ledger_dir = _ledger_dir(request)
    if not (ledger_dir / "account.json").exists():
        return {"positions": [], "closed": [], "summary": None, "note": "台账未初始化"}
    loop = getattr(request.app.state, "trading_loop", None)
    if loop is not None and getattr(loop, "_boot", False):
        lg, account = loop.ledger, loop.account
    else:
        account = Account.from_file(ledger_dir / "account.json")
        lg = Ledger(data_dir=ledger_dir, read_only=True)
        lg.replay(account, persist=False)
    snap = lg.snapshot()
    closed, open_plans = _round_trips(snap["events"])
    symbols = [p["symbol"] for p in snap["positions"]]
    marks = _live_marks(symbols)
    now = us_clock.now()
    today = us_clock.today()
    since = today - timedelta(days=MARK_LOOKBACK_DAYS)
    names = _setup_names(request)
    for c in closed:
        c["setup_label"] = _setup_label(c.get("setup_type"), c.get("side") or "long", names)
    positions = []
    for p in snap["positions"]:
        sym = p["symbol"]
        plan = open_plans.get(sym, {})
        if sym in marks:
            price, ts, source = marks[sym]
            age = round(now.timestamp() - ts / 1000, 1)
            if not us_clock.in_session(now):
                # off-session the buffer still holds the last session tick: price = last trade, label it as close
                source, age = "last_close", None
        else:
            df = request.app.state.repo.get_daily(sym, since, today)
            price = float(df["close"][-1]) if not df.is_empty() else p["avg_cost"]
            age, source = None, "last_close" if not df.is_empty() else "cost"
        avg = p["avg_cost"]
        stop = p.get("stop_price") or plan.get("stop_price")
        target = plan.get("target_zone")
        risk = plan.get("risk_per_share")
        entry_day = date.fromisoformat(p["entry_date"]) if p.get("entry_date") else None
        short = p["qty"] < 0
        size = abs(p["qty"])
        move = (avg - price) if short else (price - avg)
        positions.append({
            "symbol": sym, "side": "short" if short else "long", "qty": size,
            "card_id": p.get("card_id") or plan.get("card_id"),
            "setup_type": plan.get("setup_type"), "entry_date": p.get("entry_date"),
            "setup_label": _setup_label(plan.get("setup_type"), "short" if short else "long", names),
            "entry_price": round(plan.get("entry_price", avg), 4), "avg_cost": round(avg, 4),
            "last_price": round(price, 4), "price_source": source, "price_age_s": age,
            # 股票式: 带符号市值 (空头为负 = 回补负债); 合约式: 浮动盈亏 (不按名义进出现金)
            "market_value": round(position_value(p["qty"], price, avg, p.get("contract", False)), 2),
            "notional": round(size * price, 2), "accounting": "contract" if p.get("contract") else "stock",
            "risk_pct": plan.get("risk_pct"), "entry_tier": plan.get("entry_tier"),
            "unrealized_pnl": round(size * move, 2),
            "unrealized_pct": round(move / avg * 100, 3) if avg else None,
            "r_now": round(move / risk, 2) if risk else None,
            "stop_price": stop, "stop_distance_pct": round((stop / price - 1) * 100, 3) if stop and price else None,
            "target_zone": target,
            # 先触及的那一端: 多头 = 目标下沿; 空头 = 目标上沿
            "target_distance_pct": round(((target[1] if short else target[0]) / price - 1) * 100, 3) if target and price else None,
            "days_held": us_clock.days_held(entry_day, today) if entry_day else None,
            "max_holding_days": plan.get("max_holding_days") or account.max_holding_days,
            "partial_taken": bool(plan.get("exits")),
            "entry_quote_feed": plan.get("quote_feed"),
            **(_stop_history(snap["events"], plan) if plan else
               {"stop_initial": None, "target_initial": None, "stop_moves": []}),
        })
    mv = sum(x["market_value"] for x in positions)
    nav = snap["cash"] + mv
    wins = [c for c in closed if (c["pnl"] or 0) > 0]
    rs = [c["r_multiple"] for c in closed if isinstance(c.get("r_multiple"), (int, float))]
    # 0.26.0: 胜率按口径分开 (机会板卡 / 旧规则卡)。旧规则 12 回合 66.7% 不能当成机会板的成绩。
    by_scope = []
    for key, label in (("swing", "机会板"), ("legacy", "旧规则")):
        grp = [c for c in closed if _scope_of(c.get("card_id")) == key]
        if not grp:
            continue
        gr = [c["r_multiple"] for c in grp if isinstance(c.get("r_multiple"), (int, float))]
        by_scope.append({"key": key, "label": label, "closed_trades": len(grp),
                         "win_rate": round(sum(1 for c in grp if (c["pnl"] or 0) > 0) / len(grp), 4),
                         "avg_r": round(sum(gr) / len(gr), 3) if gr else None})
    summary = {
        "nav": round(nav, 2), "cash": round(snap["cash"], 2), "market_value": round(mv, 2),
        "initial_equity": account.initial_equity,
        "total_return_pct": round((nav / account.initial_equity - 1) * 100, 3) if account.initial_equity else None,
        # 已实现 = 净值 - 初始权益 - 未实现: 含已闭合回合 + 仍持有回合里已减仓 (目标下沿/财报前减半) 的盈亏与分红
        # (bughunt-1015: 旧口径只加闭合回合, 与「未实现」相加对不上总收益)。
        # 合约式持仓不动现金 → 现金 - 本金; 股票式持仓的名义成本已出现金 → 加回 qty x 均价 (空头 qty 为负同式)。
        "realized_pnl": round(snap["cash"] - account.initial_equity
                              + sum(p["qty"] * p["avg_cost"] for p in snap["positions"] if not p.get("contract")), 2),
        "unrealized_pnl": round(sum(x["unrealized_pnl"] for x in positions), 2),
        "closed_trades": len(closed), "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "by_scope": by_scope,
        "open_risk": round(snap["open_risk"], 2), "refresh_seconds": LIVE_REFRESH_SECONDS,
        # 合约式回合占用的保证金 (名义 × 保证金率, 多空对称) 与剩余可开仓资金
        "margin_used": round(sum(x["notional"] for x in positions if x["accounting"] == "contract") * CONTRACT_MARGIN_RATE, 2),
        "buying_power": round(lg.buying_power({x["symbol"]: x["last_price"] for x in positions}), 2),
        "as_of": now.isoformat(),
    }
    return {"positions": positions, "closed": list(reversed(closed)), "summary": summary}
