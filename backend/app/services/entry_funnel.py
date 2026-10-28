# ruff: noqa: RUF001
"""准入漏斗 (2026-10-12): 激活 → 触价 → 被拦 → 成交, 按天汇总。只读/只追加, 不影响交易。

- 激活 / 触价 / 成交直接从台账 events.jsonl 读 (opportunity state=active / entry_intent / fill)。
- 被拦: 交易环每轮的 entry_blockers 只在内存里; 这里按 (日, 标的, 原因) 去重后追加到
  funnel/<日>.jsonl, 同一原因一天只记第一次 (每 15 秒一轮, 不去重会很快膨胀)。
- 回补 (backfill) 成交单独计数, 不混进实时成交。
- 0.26.0 (无限 10-18): 只统计机会板的卡 (卡 id 含 -book-swing-)。v4 下旧决策台每晚 2000+ 只的观察卡永远不下单,
  10-16 一天 3967 条拦截里排前四的原因全是它们, 把板上真正的 2 张卡淹没了。逐卡拦截落盘时带 card_id;
  0.26 前落盘的行没有 card_id, 无法判断归属, 不计入。全局拦截 (symbol = *) 照旧单列。
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

REASON_LABEL = {
    "regular_open_unavailable": "缺当日开盘价",
    "earnings_calendar_unavailable": "财报日历不可用",
    "corporate_actions_unavailable": "公司行动不可用",
    "valuation_incomplete": "持仓估值不全",
    "circuit_breaker_day_halt": "当日熔断",
    "circuit_breaker_unavailable": "熔断评估出错",
    "corporate_action_pending": "公司行动待处理",
    "corporate_actions_not_covered": "公司行动未覆盖",
    "earnings_window": "财报窗口",
    "premarket_beyond_stop": "盘前越过止损已取消",
    "premarket_check_pending": "盘前越止损检查未完成",
}
_LOCK = threading.Lock()
_SEEN: dict[tuple[str, str], set[str]] = {}


def label(reason: str) -> str:
    if reason.startswith("setup_degraded:"):
        return f"规则已降级（{reason.split(':', 1)[1]}）"
    return REASON_LABEL.get(reason, reason)


def _path(data_dir, day: date) -> Path:
    p = Path(data_dir) / "funnel"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{day.isoformat()}.jsonl"


def _read(data_dir, day: date) -> list[dict]:
    p = Path(data_dir) / "funnel" / f"{day.isoformat()}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def record_blockers(data_dir, now: datetime, blockers: list[dict]) -> int:
    """交易环每轮末尾调用。返回本次新写入的行数。异常由调用方吞掉。"""
    if not blockers:
        return 0
    day = now.date()
    k = (str(data_dir), day.isoformat())
    with _LOCK:
        seen = _SEEN.get(k)
        if seen is None:
            seen = {f"{r['symbol']}|{r['reason']}" for r in _read(data_dir, day)}
            # 只缓存当天, 跨日自动丢弃旧键
            for old in [x for x in _SEEN if x[0] == str(data_dir)]:
                _SEEN.pop(old, None)
            _SEEN[k] = seen
        rows = []
        for b in blockers:
            sym, reason = str(b.get("symbol") or "*"), str(b.get("reason") or "")
            if not reason:
                continue
            cid = b.get("card_id")
            if sym != "*" and not is_board_card(cid):
                continue                 # 0.26.0: 只记机会板的卡
            key = f"{sym}|{reason}"
            if key in seen:
                continue
            seen.add(key)
            row = {"symbol": sym, "reason": reason, "at": now.isoformat(timespec="seconds")}
            if cid:
                row["card_id"] = str(cid)
            rows.append(row)
        if rows:
            with _path(data_dir, day).open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(rows)


def is_board_card(card_id) -> bool:
    """机会板 (tight-v1) 的卡: 下单簿卡 id = opp-<信号日>-<票>-book-swing[-short]-<hash>。"""
    return "-book-swing" in str(card_id or "")


def _events(data_dir) -> list[dict]:
    p = Path(data_dir) / "ledger" / "events.jsonl"
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _day(e: dict) -> str:
    return str(e.get("trade_date") or e.get("at") or "")[:10]


def summarize(data_dir, *, days: int = 10, end: date | None = None) -> dict:
    """最近 N 个交易日的漏斗, 新的在前。"""
    end = end or us_clock.today()
    want = []
    d = end if us_clock.is_trading_day(end) else us_clock.previous_trading_day(end)
    while len(want) < days:
        want.append(d)
        d = us_clock.previous_trading_day(d)
    keys = {x.isoformat() for x in want}
    active, touched, blocked_intent, filled, backfill = (defaultdict(set) for _ in range(5))
    for e in _events(data_dir):
        dd = _day(e)
        if dd not in keys:
            continue
        kind, sym = e.get("kind"), e.get("symbol")
        if not is_board_card(e.get("card_id")):
            continue                     # 0.26.0: 只数机会板卡 (旧决策台/旧规则卡不进漏斗)
        if kind == "opportunity" and e.get("state") == "active":
            active[dd].add(e.get("card_id") or sym)
        elif kind == "entry_intent":
            touched[dd].add(sym)
            if e.get("status") == "blocked":
                blocked_intent[dd].add((sym, e.get("reason") or "blocked"))
        elif kind == "fill":
            (backfill if e.get("backfill") else filled)[dd].add(sym)
    out = []
    for day in want:
        dd = day.isoformat()
        rows = _read(data_dir, day)
        by_reason: Counter = Counter()
        global_reasons = []
        for r in rows:
            if r["symbol"] == "*":
                global_reasons.append({"reason": r["reason"], "label": label(r["reason"]), "since": r["at"][11:16]})
            elif is_board_card(r.get("card_id")):
                by_reason[r["reason"]] += 1
        for _sym, reason in blocked_intent[dd]:
            by_reason[reason] += 1
        out.append({
            "day": dd, "active": len(active[dd]), "touched": len(touched[dd]),
            "blocked": sum(by_reason.values()), "filled": len(filled[dd]), "backfill": len(backfill[dd]),
            "reasons": [{"reason": k, "label": label(k), "n": n} for k, n in by_reason.most_common()],
            "global": global_reasons, "recorded": bool(rows) or Path(_path(data_dir, day)).exists(),
            "scope": "swing",
        })
    return {"days": out, "as_of": us_clock.now().isoformat(timespec="minutes")}


def prune(data_dir, keep_days: int = 120) -> int:
    cut = (us_clock.today() - timedelta(days=keep_days)).isoformat()
    n = 0
    for p in (Path(data_dir) / "funnel").glob("*.jsonl"):
        if p.stem < cut:
            p.unlink(missing_ok=True)
            n += 1
    return n
