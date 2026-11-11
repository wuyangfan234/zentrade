"""策略页面板 (只读聚合): 一个策略一行, 把四处现成数据拼在一起。

- 入选: 机会页规则设置里的 3 多 + 3 空 (data/rules/swing.json), 这里只读, 不提供修改口。
- 回放: 机会板同一张卡的历史 (config/swing_rule_history.json), 多空各一份。
- 自选命中: 自选信号表 (opportunities/watchlist/<信号日>.json), 当日名单 + 近 20 个信号日的命中次数。
- 机会板现状: 现在板上的卡 (挂单 / 入场区内 / 持仓 / 不下单), 按卡上的入选策略挂到对应策略名下。
- 模拟成绩: 已结算的机会板卡 (research/swing_settle), 按卡上的 rules 归到策略名下, 与回放并排。

只读文件与进程内状态: 不算新指标、不出网、不写任何文件。某一段读失败只空那一段 (errors), 不拖垮整页。
面板数字只展示, 不参与下单、许可、排序。

归因口径: 一张卡同时命中两条入选策略时, 两条策略各记一次, 所以各策略相加不等于按卡合计。
区间与背离沿用机会页页头那一行的算法 (swing_settlement.live_vs_replay: 出卡日聚类自助法 95%,
出卡日不足 8 个不给区间, 也不判背离)。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTRACT = "strategy-panel-v1"
HIST_DAYS = 20          # 自选命中迷你柱: 最近 20 个信号日
TRADES_MAX = 400        # 每个策略每个方向最多带回这么多笔已平仓成交 (新的在后)
SIDES = ("long", "short")

_SIG_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_SIG_CACHE_MAX = 120
_SIG_LOCK = threading.Lock()


def _key(rule: str, side: str) -> str:
    return f"{rule}|{side}"


def _side(x: dict) -> str:
    return "short" if x.get("side") == "short" else "long"


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _iso(v) -> str | None:
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v)[:10] if isinstance(v, str) and v else None


def _section(name: str, fn, errors: dict, default: Any = None) -> Any:
    try:
        return fn()
    except Exception as e:  # 一块失败不拖垮整页
        logger.warning("strategy panel: %s failed: %s", name, type(e).__name__)
        errors[name] = type(e).__name__
        return default


# ── 自选信号表 ────────────────────────────────────────────────

def _day_summary(path: Path) -> dict | None:
    """一份自选信号表 -> {hits: {"<规则>|<方向>": [代码]}, universe}。按文件签名缓存 (已落盘的信号日不再变)。"""
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    ck = str(path)
    with _SIG_LOCK:
        hit = _SIG_CACHE.get(ck)
        if hit and hit[0] == sig:
            return hit[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    hits: dict[str, set[str]] = {}
    for x in raw.get("signals") or []:
        if not isinstance(x, dict):
            continue
        rule, sym = x.get("rule"), x.get("symbol")
        if isinstance(rule, str) and isinstance(sym, str) and rule and sym:
            hits.setdefault(_key(rule, _side(x)), set()).add(sym)
    wl = raw.get("watchlist")
    out = {"hits": {k: sorted(v) for k, v in hits.items()}, "universe": len(wl) if isinstance(wl, list) else None}
    with _SIG_LOCK:
        if len(_SIG_CACHE) >= _SIG_CACHE_MAX:
            _SIG_CACHE.clear()
        _SIG_CACHE[ck] = (sig, out)
    return out


def _signals(data_dir: Path, day: date | None) -> dict:
    """当日命中名单 + 近 HIST_DAYS 个信号日的命中次数。day=None 取最新信号日; 指定日没有信号表 = 名单为空 (day=None)。"""
    from app.services import watchlist_v4 as wv
    files = wv.signal_files(data_dir)
    empty = {"day": None, "requested": _iso(day), "days": [], "universe": None, "hits": {}, "hist": {}}
    if not files:
        return empty
    have = {d for d, _ in files}
    target = files[-1][0] if day is None else (day if day in have else None)
    upto = target or day
    window = [(d, p) for d, p in files if upto is None or d <= upto][-HIST_DAYS:]
    days, per_day = [], []
    for d, p in window:
        s = _day_summary(p)
        days.append(d.isoformat())
        per_day.append((d, s))
    hist: dict[str, list[int]] = {}
    for i, (_d, s) in enumerate(per_day):
        for k, syms in ((s or {}).get("hits") or {}).items():
            hist.setdefault(k, [0] * len(per_day))[i] = len(syms)
    cur = next((s for d, s in per_day if d == target), None) if target else None
    return {"day": target.isoformat() if target else None, "requested": _iso(day), "days": days,
            "universe": (cur or {}).get("universe"), "hits": dict((cur or {}).get("hits") or {}), "hist": hist}


# ── 机会板现状 ────────────────────────────────────────────────

def _board(data_dir: Path, repo, loop) -> dict[str, list[dict]]:
    from app.services import swing_desk as sd
    live = sd.build_live(data_dir, repo=repo, loop=loop)
    out: dict[str, list[dict]] = {}
    for side in SIDES:
        for c in live.get(side) or []:
            if not isinstance(c, dict):
                continue
            row = {"symbol": c.get("symbol"), "state": c.get("state"),
                   "skip": bool(c.get("blocked")) or c.get("verdict") == "skip",
                   "signal_day": _iso(c.get("signal_day")), "r_now": _num(c.get("r_now")), "card_id": c.get("card_id")}
            for s in c.get("strategies") or []:
                sid = s.get("id") if isinstance(s, dict) else s
                if isinstance(sid, str) and sid:
                    out.setdefault(_key(sid, side), []).append(row)
    return out


# ── 模拟成绩 (按规则归因) ────────────────────────────────────

def _replay(rule: str, side: str, hist: dict) -> dict | None:
    """这条策略这个方向的回放行。做空只认「卖出信号当做空」的那批策略 (键 <id>:short), 与规则设置里的口径一致。"""
    from app.services import watchlist_v4 as wv
    if side == "short":
        h = hist.get(f"{rule}:short") if rule in wv.SELL_IS_SHORT else None
    else:
        h = hist.get(rule)
    return dict(h) if isinstance(h, dict) else None


def _replay_mean(rp: dict | None) -> float | None:
    return None if not rp or rp.get("thin") else _num(rp.get("mean_r"))


def _paper(data_dir: Path, hist: dict) -> tuple[dict[str, dict], str | None]:
    from app.services import swing_settlement as ss
    rows = [r for r in ss.read_outcomes(data_dir)
            if isinstance(r, dict) and r.get("status") in ("no_fill", "closed")]
    by: dict[str, list[dict]] = {}
    for r in rows:
        for rule in r.get("rules") or []:
            if isinstance(rule, str) and rule:
                by.setdefault(_key(rule, _side(r)), []).append(r)
    out: dict[str, dict] = {}
    for k, xs in by.items():
        rule, side = k.rsplit("|", 1)
        rp = _replay(rule, side, hist)
        v = ss.live_vs_replay(ss._by_day(xs), _replay_mean(rp))
        closed = sorted((r for r in xs if r.get("status") == "closed" and _num(r.get("r")) is not None),
                        key=lambda r: (str(r.get("exit_date") or r.get("settled_on") or ""), str(r.get("symbol") or "")))
        rs = [float(r["r"]) for r in closed]
        out[k] = {
            "cards": len(xs), "fills": len(rs), "days": v["days"],
            "fill_rate": round(len(rs) / len(xs), 3) if xs else None,
            "sum_r": round(sum(rs), 3) if rs else 0.0, "mean_r": v["mean_r"],
            "win": sum(1 for x in rs if x > 0), "loss": sum(1 for x in rs if x <= 0),
            "ci": v["ci"], "diverged": v["diverged"], "replay_mean_r": v["replay_mean_r"],
            "replay_fill": None if not rp or rp.get("thin") else _num(rp.get("fill")),
            "trades": [{"symbol": r.get("symbol"), "signal_day": _iso(r.get("signal_day")),
                        "fill_date": _iso(r.get("fill_date")),
                        "exit_date": _iso(r.get("exit_date") or r.get("settled_on")),
                        "why": r.get("why"), "r": round(float(r["r"]), 3)} for r in closed[-TRADES_MAX:]],
        }
    days = [str(r.get("signal_day")) for r in rows if r.get("signal_day")]
    return out, (min(days)[:10] if days else None)


# ── 组装 ──────────────────────────────────────────────────────

def build(data_dir: Path, *, strategy_ids: list[str], repo=None, loop=None, day: date | None = None,
          now: datetime | None = None) -> dict:
    """strategy_ids = 策略页列出的策略 (引擎里可公开运行的那些); 返回按策略 id 索引的面板数据。"""
    from app.market_clock import us_clock
    from app.services import swing_desk as sd
    from app.services import watchlist_v4 as wv
    data_dir = Path(data_dir)
    errors: dict[str, str] = {}
    now = us_clock.market_datetime(now or us_clock.now())

    sel = _section("selected", lambda: sd.strategies_from_ruleset(data_dir), errors, ((), ()))
    hist_file = _section("replay", lambda: sd.rule_history(), errors, {}) or {}
    rules_hist = hist_file.get("rules")
    hist: dict = rules_hist if isinstance(rules_hist, dict) else {}
    sig = _section("signals", lambda: _signals(data_dir, day), errors) or {
        "day": None, "requested": _iso(day), "days": [], "universe": None, "hits": {}, "hist": {}}
    board = _section("board", lambda: _board(data_dir, repo, loop), errors, {}) or {}
    paper, since = _section("paper", lambda: _paper(data_dir, hist), errors, ({}, None)) or ({}, None)

    n_days = len(sig["days"])
    sel_long, sel_short = set(sel[0]), set(sel[1])

    def side_view(sid: str, side: str) -> dict:
        k = _key(sid, side)
        return {"selected": sid in (sel_short if side == "short" else sel_long),
                "replay": _replay(sid, side, hist),
                "hits": list(sig["hits"].get(k) or []),
                "hist": list(sig["hist"].get(k) or [0] * n_days),
                "board": list(board.get(k) or []),
                "paper": paper.get(k)}

    strategies: dict[str, dict] = {}
    for sid in strategy_ids:
        short_ok = bool(_section(f"short:{sid}", lambda sid=sid: wv.short_capable(sid), errors, False))
        sides = {"long": side_view(sid, "long")}
        if short_ok:
            sides["short"] = side_view(sid, "short")
        strategies[sid] = {"short_capable": short_ok,
                           "name_short": sd.strategy_name(sid, "short") if short_ok else None, "sides": sides}

    win = hist_file.get("window") or []
    return {"contract": CONTRACT, "generated_at": now.isoformat(timespec="seconds"),
            "selected": {"long": list(sel[0]), "short": list(sel[1])},
            "replay_meta": {"window": [str(x) for x in win][:2] or None, "universe": hist_file.get("universe"),
                            "data_end": hist_file.get("data_end"), "generated": hist_file.get("generated")},
            "signals": {"day": sig["day"], "requested": sig["requested"], "days": sig["days"],
                        "universe": sig["universe"]},
            "paper_since": since, "strategies": strategies, "errors": errors}
