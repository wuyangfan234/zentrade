"""主题压力 (机会页 B 档, 2026-11-15; 只显示, 不拦单, 不进交易环 / 准入 / 下单簿)。

用用户自己的自选分组当主题: 一个主题里的持仓一起再跌 (空头: 一起再涨) stress_pct, 账户亏多少。
- 名义 = |股数| x 现价 (估值与持仓页同源: 热报价优先, 否则最近收盘; 都没有用成本)。
- 计划止损风险 = 多头 股数 x max(成本 - 止损, 0); 空头 |股数| x max(止损 - 成本, 0)。止损移到成本价的仓计 0。
- 压力损失 = 名义 x stress_pct。计划止损风险与压力损失是两回事: 前者假设止损按价成交, 后者是整组同向跳空。
- 一票多组: 各组都计入 (主题行相加可以大于合计)。
- 覆盖过半自选的分组 (例如“核心”) 是分层不是主题, 不算一行, 悬停里写明。
- 今日卡全部成交后: 当前机会板上会挂单 (结论 = 下单) 的卡按计划股数 x 入场区中值折名义, 一起算进该主题。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "theme-exposure-v1"
DEFAULT_STRESS_PCT = 20
STRESS_LEVELS = (10, 15, 20, 25, 30)
MAX_ROWS = 5
TIER_SHARE = 0.5               # 覆盖 >= 一半自选的分组 = 分层, 不当主题


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "theme_stress.json"


def stress_pct(data_dir: Path) -> int:
    """齿轮里设的压力跌幅 (%); 没设或读坏 = 20。"""
    try:
        v = json.loads(settings_path(data_dir).read_text(encoding="utf-8")).get("pct")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_STRESS_PCT
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v in STRESS_LEVELS else DEFAULT_STRESS_PCT


def save_stress_pct(data_dir: Path, pct: int) -> int:
    from datetime import datetime

    from app.services.fs_utils import atomic_write_text
    if pct not in STRESS_LEVELS:
        raise ValueError(f"stress pct must be one of {STRESS_LEVELS}")
    p = settings_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"pct": int(pct), "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                    ensure_ascii=False))
    return int(pct)


def _r2(v: float) -> float:
    return round(float(v), 2)


def compute(positions: list[dict], groups: list[dict], members: dict[str, list[str]], *, nav: float | None,
            pct: int, pending: list[dict] | None = None, watch_total: int | None = None) -> dict:
    """纯函数。positions = [{symbol, qty(空头为负), avg_cost, stop, price}]; groups = [{id, name}];
    members = {票: [组 id]}; pending = [{symbol, side, qty, mid}] 今日会挂单的卡。"""
    f = pct / 100.0
    total_watch = watch_total if watch_total else len(members)
    counts: dict[str, int] = {}
    for gids in members.values():
        for g in set(gids or []):
            counts[g] = counts.get(g, 0) + 1
    names = {str(g.get("id")): str(g.get("name") or "") for g in groups if g.get("id")}
    tiers = [{"name": names[g], "n": counts.get(g, 0)} for g in names
             if total_watch and counts.get(g, 0) / total_watch >= TIER_SHARE]
    tier_ids = {g for g in names if total_watch and counts.get(g, 0) / total_watch >= TIER_SHARE}

    def risk_of(p: dict) -> float:
        q, cost, stop = float(p["qty"]), float(p["avg_cost"]), p.get("stop")
        if not isinstance(stop, (int, float)):
            return 0.0
        return q * max(cost - float(stop), 0.0) if q > 0 else -q * max(float(stop) - cost, 0.0)

    rows_by: dict[str, dict] = {}
    tot = {"notional": 0.0, "stop_risk": 0.0, "stress_loss": 0.0}
    for p in positions:
        mark = p.get("price")
        px = float(mark) if isinstance(mark, (int, float)) and mark > 0 else float(p["avg_cost"])
        notional = abs(float(p["qty"])) * px
        rk = risk_of(p)
        tot["notional"] += notional
        tot["stop_risk"] += rk
        tot["stress_loss"] += notional * f
        for g in sorted(set(members.get(p["symbol"]) or [])):
            if g not in names or g in tier_ids:
                continue
            r = rows_by.setdefault(g, {"theme": names[g], "symbols": [], "notional": 0.0, "stop_risk": 0.0,
                                       "stress_loss": 0.0, "pending_loss": 0.0, "pending": []})
            r["symbols"].append(p["symbol"])
            r["notional"] += notional
            r["stop_risk"] += rk
            r["stress_loss"] += notional * f
    for c in pending or []:
        notional = abs(float(c["qty"])) * float(c["mid"])
        for g in sorted(set(members.get(c["symbol"]) or [])):
            if g not in names or g in tier_ids:
                continue
            r = rows_by.setdefault(g, {"theme": names[g], "symbols": [], "notional": 0.0, "stop_risk": 0.0,
                                       "stress_loss": 0.0, "pending_loss": 0.0, "pending": []})
            r["pending"].append(c["symbol"])
            r["pending_loss"] += notional * f

    def pct_nav(v: float) -> float | None:
        return round(v / nav * 100, 2) if nav else None

    rows = []
    for r in rows_by.values():
        after = r["stress_loss"] + r["pending_loss"]
        rows.append({"theme": r["theme"], "symbols": sorted(r["symbols"]), "count": len(r["symbols"]),
                     "notional": _r2(r["notional"]), "notional_pct": pct_nav(r["notional"]),
                     "stop_risk": _r2(r["stop_risk"]), "stress_loss": _r2(r["stress_loss"]),
                     "stress_pct_nav": pct_nav(r["stress_loss"]),
                     "pending": sorted(set(r["pending"])), "after_fill_loss": _r2(after),
                     "after_fill_pct_nav": pct_nav(after)})
    rows.sort(key=lambda r: (-r["after_fill_loss"], -r["stress_loss"], r["theme"]))
    shown = rows[:MAX_ROWS]
    return {"contract": CONTRACT, "stress_pct": pct, "levels": list(STRESS_LEVELS), "nav": _r2(nav) if nav else None,
            "positions": len(positions), "rows": shown, "hidden": len(rows) - len(shown), "tiers": tiers,
            "total": {"notional": _r2(tot["notional"]), "notional_pct": pct_nav(tot["notional"]),
                      "stop_risk": _r2(tot["stop_risk"]), "stop_risk_pct": pct_nav(tot["stop_risk"]),
                      "stress_loss": _r2(tot["stress_loss"]), "stress_pct_nav": pct_nav(tot["stress_loss"])}}


def _pending_cards(swing: dict | None) -> list[dict]:
    out = []
    for c in [*((swing or {}).get("long") or []), *((swing or {}).get("short") or [])]:
        if c.get("state") == "holding" or c.get("blocked") or c.get("verdict") != "order":
            continue
        qty = (c.get("odds") or {}).get("qty") if isinstance(c.get("odds"), dict) else None
        qty = qty if isinstance(qty, (int, float)) else c.get("qty")
        try:
            lo, hi = c["entry_zone"]
            mid = (float(lo) + float(hi)) / 2
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(qty, (int, float)) and qty > 0 and mid > 0:
            out.append({"symbol": c["symbol"], "side": c.get("side"), "qty": qty, "mid": mid})
    return out


def view(data_dir: Path, *, repo=None, loop=None, swing: dict | None = None) -> dict:
    """页面段 (只读)。分组读不到 = 抛错 (opp_page 只空这一段)。没有持仓也返回 (行为空)。"""
    from app.services import desk_overview as ov
    from app.services import ls_rank, watchlist
    data_dir = Path(data_dir)
    groups = watchlist.list_groups()
    entries = watchlist.list_symbols()
    members = {str(e.get("symbol")).upper(): [str(g) for g in (e.get("group_ids") or [])] for e in entries if e.get("symbol")}
    lg, _acct = ov._account(loop, data_dir)
    positions = []
    if lg is not None:
        opens = list(lg.open_positions)
        marks = {}
        if opens and repo is not None:
            try:
                marks, _src = ov._marks(repo, lg)
            except Exception as e:
                logger.warning("theme exposure: marks unavailable: %s", type(e).__name__)
        for p in opens:
            positions.append({"symbol": str(p.symbol).upper(), "qty": float(p.qty), "avg_cost": float(p.avg_cost),
                              "stop": p.stop_price, "price": marks.get(p.symbol)})
    nav, _risk = ls_rank.risk_usd(data_dir)
    return compute(positions, groups, members, nav=nav, pct=stress_pct(data_dir), pending=_pending_cards(swing),
                   watch_total=len(members))
