"""机会页历史日回看 (B 档, 2026-11-15): 选一个过去的交易日 P, 只读那天冻结的文件, 回答“那天页面知道什么”。

- 机会板 = P 之前最近 ENTRY_DAYS 个信号日的下单簿 (opportunities/book/<信号日>.json, 美东 08:00 冻结);
  P 当天页面上挂的就是这几天的卡。结局列取卡结算 (research/swing_settle/outcomes.<契约>.jsonl), 与当天列分开。
- 今日多空 = 写给 P 的名单 (opportunities/ls_rank/<P>.json) + 到期后的结算。
- 盘中强势 / 开盘放量 / 多信号共振 / 盘前判断 = 各自 <P>.json。
- 某块当天没有文件 → 该块 null, errors 写 not_archived; 不回退到别的日子 (不拿 P 之后的数据补)。
- 只读; 请求内不出网、不写文件、不推送。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

CONTRACT = "opp-asof-v1"
NOT_ARCHIVED = "not_archived"


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _days(d: Path) -> list[date]:
    out = []
    if d.exists():
        for f in d.glob("20*.json"):
            try:
                out.append(date.fromisoformat(f.stem))
            except ValueError:
                continue
    return sorted(out)


def _outcomes(data_dir: Path) -> dict[str, dict]:
    from app.services import swing_settlement as ss
    out: dict[str, dict] = {}
    for contract in (None, "swing-settle-v2"):          # 现行契约优先, 旧契约补缺
        try:
            rows = ss.read_outcomes(data_dir, contract)
        except Exception:
            rows = []
        for r in rows:
            cid = r.get("card_id")
            if isinstance(cid, str) and cid not in out:
                out[cid] = r
    return out


def _outcome_view(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"status": r.get("status"), "r": r.get("r"), "why": r.get("why"), "fill_date": r.get("fill_date"),
            "exit_date": r.get("exit_date"), "settled_on": r.get("settled_on")}


def _card_rules(ev: dict) -> list[dict]:
    """卡命中的规则 (下单簿 evidence.decision.rules) → [{id, name}], 名字与机会板同一套。"""
    from app.services import swing_desk as sd
    dec = ev.get("decision") if isinstance(ev.get("decision"), dict) else {}
    side = "short" if ev.get("side") == "short" else "long"
    return [{"id": str(r), "name": sd.strategy_name(str(r), side)} for r in dec.get("rules") or [] if r]


def board(data_dir: Path, day: date) -> dict | None:
    """P 当天机会板上的卡 (前 ENTRY_DAYS 个信号日的下单簿)。一份都没有 = None。"""
    from app.services import swing_desk as sd
    from app.services import watchlist_book as wb
    sig_days = [d for d in _days(Path(data_dir) / "opportunities" / "book") if d < day][-sd.ENTRY_DAYS:]
    books = [(d, wb.load_day(data_dir, d)) for d in sig_days]
    books = [(d, b) for d, b in books if b]
    if not books:
        return None
    oc = _outcomes(data_dir)
    cards, skipped = [], []
    for d, b in sorted(books, key=lambda x: x[0], reverse=True):
        for c in b.get("cards") or []:
            ev = c.get("evidence") or {}
            cards.append({"card_id": c.get("id"), "signal_day": d.isoformat(), "symbol": c.get("symbol"),
                          "side": c.get("side") or "long", "entry_zone": c.get("entry_zone"), "stop": c.get("invalidation"),
                          "target": (c.get("target_zone") or [None])[0], "reward_risk": c.get("reward_risk"),
                          "score": ev.get("score"), "rules": _card_rules(ev),
                          "outcome": _outcome_view(oc.get(c.get("id")))})
        for x in b.get("skipped") or []:
            if isinstance(x, dict):
                skipped.append({"signal_day": d.isoformat(), "symbol": x.get("symbol"), "side": x.get("side"),
                                "reason": x.get("reason")})
    return {"signal_days": [d.isoformat() for d, _ in books], "built_at": books[-1][1].get("built_at"),
            "rules": books[-1][1].get("swing_rules"), "cards": cards, "skipped": skipped}


def ls_list(data_dir: Path, day: date) -> dict | None:
    from app.services import ls_rank
    rec = ls_rank.load_day(data_dir, day)
    if not rec:
        return None
    rr = ((rec.get("settled") or {}).get("r")) or {}
    rows = {r.get("symbol"): r for r in rec.get("rows") or [] if isinstance(r, dict)}

    def side(s: str) -> list[dict]:
        out = []
        for sym in rec.get(s) or []:
            r = rows.get(sym) or {}
            res = rr.get(sym) or {}
            out.append({"symbol": sym, "rank": r.get("rank"), "close": r.get("close"), "atr": r.get("atr"),
                        "r": res.get(s), "exit": res.get(f"{s}_exit")})
        return out
    return {"day": rec.get("day"), "as_of": rec.get("as_of"), "made_at": rec.get("made_at"), "late": bool(rec.get("late")),
            "model": ls_rank.model_quarter(rec), "n": rec.get("n"), "long": side("long"), "short": side("short"),
            "settled_end": (rec.get("settled") or {}).get("end")}


def intraday(data_dir: Path, day: date) -> dict | None:
    d = _read(Path(data_dir) / "opportunities" / "intraday" / f"{day.isoformat()}.json")
    if d is None:
        return None
    keep = ("symbol", "side", "check", "fired_at", "entry", "stop", "target", "status", "exit_reason", "r")
    return {"cards": [{k: c.get(k) for k in keep} for c in d.get("cards") or [] if isinstance(c, dict)],
            "checks": len(d.get("checks") or [])}


def radar(data_dir: Path, day: date) -> dict | None:
    d = _read(Path(data_dir) / "opportunities" / "radar" / f"{day.isoformat()}.json")
    if d is None:
        return None
    keep = ("symbol", "rank", "rvol", "dir", "gap_pct", "or_chg_pct", "chg_close_pct")
    rows = [r for r in d.get("rows") or [] if isinstance(r, dict) and r.get("pick")]
    rows.sort(key=lambda r: (r.get("rank") or 999, str(r.get("symbol"))))
    return {"at": d.get("at"), "symbols": d.get("symbols"), "rows": [{k: r.get(k) for k in keep} for r in rows]}


def resonance(data_dir: Path, day: date) -> dict | None:
    d = _read(Path(data_dir) / "opportunities" / "resonance" / f"{day.isoformat()}.json")
    if d is None:
        return None
    keep = ("symbol", "side", "n", "engines", "at", "entry")
    return {"rows": [{k: r.get(k) for k in keep} for r in d.get("rows") or [] if isinstance(r, dict)]}


def mood(data_dir: Path, day: date) -> dict | None:
    d = _read(Path(data_dir) / "market_mood" / "judgments" / f"{day.isoformat()}.json")
    if d is None:
        return None
    return {k: d.get(k) for k in ("generated_at", "stance", "probs", "main", "stars")}


def available_days(data_dir: Path, today: date) -> list[str]:
    """有任一冻结文件的过去交易日 (日期选择器的可选范围)。"""
    root = Path(data_dir)
    days: set[date] = set()
    for sub in ("opportunities/ls_rank", "opportunities/intraday", "opportunities/radar", "opportunities/resonance",
                "market_mood/judgments", "opportunities/swing_live"):
        days |= set(_days(root / sub))
    book = _days(root / "opportunities" / "book")
    if book:
        from app.market_clock import us_clock
        for d in book:
            try:
                days.add(us_clock.next_trading_day(d))
            except ValueError:
                continue
    return [d.isoformat() for d in sorted(x for x in days if x < today)]


def build(data_dir: Path, day: date, *, today: date) -> dict[str, Any]:
    """P = day (必须早于今天)。每块各自容错; 没有文件 = null + errors[块] = not_archived。"""
    if day >= today:
        raise ValueError("asof must be before today")
    data_dir = Path(data_dir)
    errors: dict[str, str] = {}
    out: dict[str, Any] = {"contract": CONTRACT, "asof": day.isoformat(), "today": today.isoformat()}
    for name, fn in (("board", board), ("ls_rank", ls_list), ("intraday", intraday), ("radar", radar),
                     ("resonance", resonance), ("mood", mood)):
        try:
            v = fn(data_dir, day)
        except Exception as e:
            v = None
            errors[name] = type(e).__name__
        else:
            if v is None:
                errors[name] = NOT_ARCHIVED
        out[name] = v
    out["errors"] = errors
    return out
