"""盘前跳空预警 (2026-10-02 产品决策)。

- 时点: 美东 09:00 (北京夏令时 21:00 / 冬令时 22:00, 随美东自动切换)
- 范围: HL 有合约的 持仓 + 活跃/观察机会卡 + 自选股
- 阈值: HL 中间价相对上一交易日收盘 ±3%
- 推送: 合并为一条; 没有触发就不推送; 只提醒, 不触发任何交易动作
- 实测 (24 只 × 29 日): 09:00 ET 价与开盘跳空相关 0.966, 偏差中位 33 bps
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from app.market_clock import us_clock
from app.services import notify_opportunity as notify

logger = logging.getLogger(__name__)

THRESHOLD_PCT = 3.0
SCHEDULE_ET = (9, 0)
ROLE_ORDER = {"持仓": 0, "机会卡": 1, "自选": 2}


def scan(*, scope: dict[str, list[str]], prev_close: dict[str, float], mids: dict[str, float],
         threshold_pct: float = THRESHOLD_PCT) -> dict:
    alerts, not_on_hl, checked, gaps = [], [], 0, {}
    for sym, roles in scope.items():
        if sym not in mids:
            if "持仓" in roles:
                not_on_hl.append(sym)
            continue
        pc = prev_close.get(sym)
        if not pc:
            continue
        checked += 1
        gap = (mids[sym] / pc - 1) * 100
        gaps[sym] = round(gap, 3)
        if abs(gap) >= threshold_pct:
            alerts.append({"symbol": sym, "roles": sorted(roles, key=lambda r: ROLE_ORDER.get(r, 9)),
                           "hl_price": mids[sym], "prev_close": pc, "gap_pct": round(gap, 3),
                           "direction": "up" if gap > 0 else "down"})
    alerts.sort(key=lambda a: (min(ROLE_ORDER.get(r, 9) for r in a["roles"]), -abs(a["gap_pct"])))
    return {"alerts": alerts, "checked": checked, "not_on_hl": sorted(not_on_hl), "threshold_pct": threshold_pct,
            "gaps": gaps, "mids": {s: mids[s] for s in gaps}}


def message(res: dict, day: date, at: datetime) -> dict:
    lines = [f"**{day.isoformat()}** · 美东 {at:%H:%M} · Hyperliquid 永续价 vs 昨收 · 阈值 ±{res.get('threshold_pct', THRESHOLD_PCT):g}%\n"]
    for a in res["alerts"]:
        arrow = "▲" if a["direction"] == "up" else "▼"
        lines.append(f"- {arrow} **{a['symbol']}**（{'/'.join(a['roles'])}）{a['gap_pct']:+.2f}% · "
                     f"HL ${a['hl_price']:,.2f} vs 昨收 ${a['prev_close']:,.2f}")
    lines.append(f"\nHL 有合约标的 {res['checked']} 只")
    if res.get("not_on_hl"):
        lines.append(f"无 HL 合约、缺盘前价的持仓：{'、'.join(res['not_on_hl'])}")
    lines.append("观察，不触发交易；永续价含基差，以开盘成交为准")
    return {"title": "ZenTrade · 盘前跳空预警", "subtitle": day.isoformat(), "body": "\n".join(lines)}


def _inputs(data_dir: Path, loop) -> tuple[dict[str, list[str]], dict[str, float], dict[str, float]]:
    scope: dict[str, list[str]] = {}
    if loop is not None:
        for p in loop.ledger.open_positions:
            scope.setdefault(p.symbol, []).append("持仓")
        for s in loop.execution_symbols():
            if "持仓" not in scope.get(s, []):
                scope.setdefault(s, []).append("机会卡")
    try:
        from app.services import watchlist
        for r in watchlist.list_symbols():
            if r.get("symbol"):
                roles = scope.setdefault(r["symbol"], [])
                if "自选" not in roles:
                    roles.append("自选")
    except Exception as e:  # noqa: BLE001
        logger.warning("premarket gap: watchlist unavailable: %s", type(e).__name__)
    from app.data_providers import custom as custom_sources
    hl = custom_sources.get_provider("hyperliquid")
    mids = hl.mids(sorted(scope))
    prev_close: dict[str, float] = {}
    repo = getattr(loop, "repo", None)
    if repo is not None:
        today = us_clock.today()
        for s in mids:
            try:
                df = repo.get_daily(s, today - timedelta(days=10), today - timedelta(days=1))
                if not df.is_empty():
                    prev_close[s] = float(df["close"][-1])
            except Exception:  # noqa: BLE001
                continue
    return scope, prev_close, mids


def run(data_dir: Path, loop, *, now: datetime | None = None) -> dict:
    now = now or us_clock.now()
    day = now.date()
    scope, prev_close, mids = _inputs(data_dir, loop)
    res = scan(scope=scope, prev_close=prev_close, mids=mids)
    res.update(day=day.isoformat(), at=now.isoformat())
    out = Path(data_dir) / "premarket_gap"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{day.isoformat()}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    from app.services import strategy_evaluation
    from app.services import swing_desk as sd
    if strategy_evaluation.evidence_mode(data_dir) == "v4" and sd.swing_config_path(data_dir).exists():
        try:   # 机会板模式: 盘前推送 = 机会板上的卡 (= 纸面下单簿), 与页面同一份
            board = sd.build_live(data_dir, repo=getattr(loop, "repo", None), loop=loop, today=day)
            notify._enqueue(data_dir, f"preopen:{day.isoformat()}", swing_preopen_message(board, res, day, now))
        except Exception as e:
            logger.warning("preopen swing push failed (soft): %s", type(e).__name__)
    elif strategy_evaluation.evidence_mode(data_dir) == "v4":
        try:
            from app.services import watchlist_table
            table = watchlist_table.build(data_dir=data_dir, repo=getattr(loop, "repo", None), loop=loop)
            notify._enqueue(data_dir, f"preopen:{day.isoformat()}", preopen_message(table, res, day, now))
        except Exception as e:  # noqa: BLE001
            logger.warning("preopen table push failed (soft): %s", type(e).__name__)
    elif res["alerts"]:
        notify._enqueue(data_dir, f"premarket_gap:{day.isoformat()}", message(res, day, now))
    logger.info("premarket gap %s: checked=%d alerts=%d", day, res["checked"], len(res["alerts"]))
    return res


BLOCK_LABEL = {"holding_other": "同票持仓中", "cooldown": "平仓冷却", "earnings": "财报前一日",
               "direction_conflict": "多空冲突", "paper_watch": "准入阻断",
               "premarket_beyond_stop": "盘前越止损", "entry_closed": "入场已截止"}


def _odds_text(o) -> str:
    """ · 回区 73% 期望 +$87 (期望没有美元时写 R); 没有数字 -> 空。"""
    if not isinstance(o, dict) or not isinstance(o.get("fill"), (int, float)):
        return ""
    if isinstance(o.get("ev_usd"), (int, float)):
        ev = f"{'+' if o['ev_usd'] > 0 else '-' if o['ev_usd'] < 0 else ''}${abs(o['ev_usd']):,.0f}"
    elif isinstance(o.get("ev_r"), (int, float)):
        ev = f"{o['ev_r']:+.2f}R"
    else:
        return ""
    return f" · 回区 {o['fill'] * 100:.0f}% 期望 {ev}"


def swing_preopen_message(board: dict, res: dict, day: date, at: datetime) -> dict:
    """盘前一条 (机会板模式): 每张卡一行 = 方向 挂单区 止损 目标 盘前涨跌; 持仓行; 自选大幅跳空。只有代码和数字。"""
    gaps = res.get("gaps") or {}
    lines = [f"**{day.isoformat()} {at:%H:%M} ET · 机会板**"]
    shown = set()
    for c in (board.get("long") or []) + (board.get("short") or []):
        g = gaps.get(c["symbol"])
        gtxt = f" 盘前 {g:+.1f}%" if isinstance(g, (int, float)) else ""
        tag = "多" if c["side"] == "long" else "空"
        shown.add(c["symbol"])
        if c.get("state") == "holding":
            r = f" {c['r_now']:+.1f}R" if isinstance(c.get("r_now"), (int, float)) else ""
            lines.append(f"{c['symbol']} {tag} 持仓 @{c.get('entry_price') or 0:.2f}{r} 止损 {c['stop']:.2f}{gtxt}")
            continue
        lo, hi = c["entry_zone"]
        # 0.27.0: 每行先写系统裁决 (下单 / 不下单 + 原因), 再写价位和两个数字 (回区率 / 期望), 与机会页卡面同一份
        why = c.get("blocked") or c.get("verdict_reason")
        verdict = "不下单" + (f" {BLOCK_LABEL[why]}" if why in BLOCK_LABEL else "") \
            if (c.get("blocked") or c.get("verdict") == "skip") else "下单"
        lines.append(f"{c['symbol']} {tag} {verdict} 挂单 {lo:.2f}-{hi:.2f} 止损 {c['stop']:.2f} 目标 {c['target']:.2f}"
                     f"{gtxt}{_odds_text(c.get('odds'))}")
    movers = [(s, g) for s, g in gaps.items() if s not in shown and abs(g) >= res.get("threshold_pct", THRESHOLD_PCT)]
    if movers:
        lines.append("跳空 " + "  ".join(f"{s} {g:+.1f}%" for s, g in sorted(movers, key=lambda x: -abs(x[1]))))
    if len(lines) == 1:
        lines.append("无")
    return {"title": "ZenTrade · 盘前", "subtitle": day.isoformat(), "body": "\n".join(lines)}


def preopen_message(table: dict, res: dict, day: date, at: datetime) -> dict:
    """盘前一条 (v4): 有信号的自选行 + 持仓行 + 自选里 |跳空| ≥ 阈值且无信号的票。只有代码和数字。"""
    gaps = res.get("gaps") or {}
    lines = [f"**{day.isoformat()} {at:%H:%M} ET · R{(table.get('ruleset') or {}).get('version')}**"]
    sig_rows = [r for r in table.get("rows") or [] if r.get("signal") or r.get("position")]
    for r in sig_rows:
        g = gaps.get(r["symbol"])
        gtxt = f" 盘前 {g:+.1f}%" if isinstance(g, (int, float)) else ""
        if r.get("position"):
            p = r["position"]
            lines.append(f"{r['symbol']} 持 {p.get('day')}/{p.get('max_days')} 止损 {p.get('stop'):.2f}{gtxt}"
                         if isinstance(p.get("stop"), (int, float)) else f"{r['symbol']} 持 {p.get('day')}/{p.get('max_days')}{gtxt}")
            continue
        s = r["signal"]
        void = " 越过" if r.get("premarket_void") else ""
        trade = "" if r.get("tradable") else " 记账"
        lines.append(f"{r['symbol']} {s['name']} 触发 {r['trigger']:.2f} 失效 {r['invalidation']:.2f}{gtxt}{void}{trade}")
    in_table = {r["symbol"] for r in sig_rows}
    movers = [(s, g) for s, g in gaps.items() if s not in in_table and abs(g) >= res.get("threshold_pct", THRESHOLD_PCT)
              and s in {r["symbol"] for r in table.get("rows") or []}]
    if movers:
        lines.append("跳空 " + "  ".join(f"{s} {g:+.1f}%" for s, g in sorted(movers, key=lambda x: -abs(x[1]))))
    if len(lines) == 1:
        lines.append("无")
    return {"title": "ZenTrade · 盘前", "subtitle": day.isoformat(), "body": "\n".join(lines)}


def latest(data_dir: Path) -> dict | None:
    d = Path(data_dir) / "premarket_gap"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
