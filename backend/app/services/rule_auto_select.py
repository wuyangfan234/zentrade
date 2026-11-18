"""自动选策略: 每周最后一个交易日收盘后, 按规则前向成绩 (rule_forward) 评一次机会板入选策略, 每侧最多换 1 条。

换入要同时满足 (全部读 rule_forward 的摘要, 这里不重算):
  - 候补在“全部格子”范围 verdict = above (有成交信号日 >= 20、成交 >= 15、按同时比较条数收紧后的区间整体高于 0);
  - 候补在“4h 同向”范围成交 >= GATE_MIN_FILLED 时, 那里的超额不能为负 (上板卡只来自 4h 同向);
  - 被换出的 = 现入选里超额最低的一条, 且它不是 above、超额低于候补; 记分满 20 个信号日仍 0 命中的入选策略算最低;
    样本还不够的入选策略不换出 (先攒够再说)。
  - 该侧上次自动换入后不足 COOLDOWN_DAYS 个交易日不动; 被撤回过的换入不再自动换回。
开关关着时照常评定并记下“本来会换什么”, 不写入选策略。

写入点只有三处: rules/auto_select.json (开关)、research/swing_settle/auto_select.json (评定与换入记录)、
经 swing_desk.save_strategies 写 rules/swing.json (与齿轮保存同一个函数)。不碰下单簿、台账、交易环、单笔风险。
成交率表 (config/swing_state_odds.json) 是离线回放产物, 换入后与新入选策略对不上, 卡上的成交率显示为空, 下单裁决不受影响。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "rule-auto-select-v1"
COOLDOWN_DAYS = 20
GATE_MIN_FILLED = 5
KEEP_EVALS = 26
BAN = frozenset({"evo", "phantom_4h", "phantom_4h_short", "phantom_4h_pine"})
SIDES = ("long", "short")


def switch_path(data_dir: Path) -> Path:
    return Path(data_dir) / "rules" / "auto_select.json"


def log_path(data_dir: Path) -> Path:
    from app.services import swing_settlement as ss
    return ss.settle_dir(data_dir) / "auto_select.json"


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write(p: Path, d: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=1))


def enabled(data_dir: Path) -> bool:
    """没有文件 = 开 (2026-11-18 产品决策: 直接全自动, 可手动关)。"""
    v = _read(switch_path(data_dir)).get("enabled")
    return True if v is None else v is True


def set_enabled(data_dir: Path, on: bool) -> dict:
    d = {"enabled": bool(on), "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    _write(switch_path(data_dir), d)
    return d


def read_log(data_dir: Path) -> dict:
    d = _read(log_path(data_dir))
    return {"contract": CONTRACT, "evals": [x for x in d.get("evals") or [] if isinstance(x, dict)],
            "swaps": [x for x in d.get("swaps") or [] if isinstance(x, dict)]}


def is_eval_day(day: date) -> bool:
    """这一周的最后一个交易日 (通常周五; 周五休市则周四)。"""
    from app.market_clock import us_clock
    return us_clock.next_trading_day(day).isocalendar()[:2] != day.isocalendar()[:2]


def next_eval_day(after: date) -> date:
    from app.market_clock import us_clock
    d = us_clock.next_trading_day(after)
    for _ in range(10):
        if is_eval_day(d):
            return d
        d = us_clock.next_trading_day(d)
    return d


def eligible(data_dir: Path, engine) -> dict[str, set[str] | None]:
    """能进入选策略的规则 (与齿轮保存口同一判断)。策略目录读不到 -> 做多 None (本次不评做多)。"""
    from app.services import watchlist_table as wt
    from app.services import watchlist_v4 as wv
    known = {r["id"] for r in wt.rule_catalog(engine)}
    long_ok: set[str] | None = None
    if engine is not None and len(known) > 4:
        long_ok = set(known)
        try:
            from app.services import rank_lab
            long_ok |= set(rank_lab.read_rankers(data_dir))
        except Exception as e:
            logger.warning("auto select: rankers unavailable: %s", type(e).__name__)
        long_ok -= BAN
    return {"long": long_ok, "short": {r for r in known if wv.short_capable(r)} - BAN}


def _row(rows: dict[str, dict], rule: str, side: str) -> dict | None:
    return rows.get(rule if side == "long" else f"{rule}:short")


def propose(table: dict, current: dict[str, list[str]], ok: dict[str, set[str] | None], swaps: list[dict],
            day: date, days_since) -> dict[str, dict]:
    """纯判定 (不写任何东西)。-> {side: {"action": "swap", "out", "in", ...} | {"action": "none", "reason"}}。
    days_since(换入日) = 那天之后到 day 的交易日数。"""
    from app.services import rule_forward as rf
    all_rows, gate_rows = rf.by_rule(table, "all"), rf.by_rule(table, "gate")
    n_days = int(table.get("days") or 0)
    out: dict[str, dict] = {}
    for side in SIDES:
        cur = [x for x in current.get(side) or [] if x not in BAN]
        allow = ok.get(side)
        if allow is None:
            out[side] = {"action": "none", "reason": "catalog_unavailable"}
            continue
        mine = [s for s in swaps if s.get("side") == side]
        live = [s for s in mine if not s.get("undone_at")]
        if live and days_since(date.fromisoformat(live[-1]["day"])) < COOLDOWN_DAYS:
            out[side] = {"action": "none", "reason": "cooldown", "since": live[-1]["day"]}
            continue
        undone = {s.get("in") for s in mine if s.get("undone_at")}
        cands = []
        for key, x in all_rows.items():
            rule = key[:-6] if key.endswith(":short") else key
            if x.get("side") != side or rule in cur or rule not in allow or rule in undone or x.get("verdict") != "above":
                continue
            g = _row(gate_rows, rule, side)
            if g and (g.get("filled") or 0) >= GATE_MIN_FILLED and isinstance(g.get("excess"), (int, float)) and g["excess"] < 0:
                continue
            cands.append((float(x["excess"]), rule, x))
        if not cands:
            out[side] = {"action": "none", "reason": "no_candidate"}
            continue
        cands.sort(key=lambda c: (-c[0], c[1]))
        best_ex, best, best_row = cands[0]
        scored = []
        for rule in cur:
            x = _row(all_rows, rule, side)
            if x is None or not x.get("hits"):
                if n_days >= rf.VERDICT_DAYS:
                    scored.append((float("-inf"), rule, x))
                continue
            if x.get("verdict") in ("thin", "above") or not isinstance(x.get("excess"), (int, float)):
                continue
            scored.append((float(x["excess"]), rule, x))
        scored = [s for s in scored if s[0] < best_ex]
        if not scored:
            out[side] = {"action": "none", "reason": "incumbents_hold", "best": best}
            continue
        scored.sort(key=lambda s: (s[0], s[1]))
        ex_out, rule_out, row_out = scored[0]
        out[side] = {"action": "swap", "out": rule_out, "in": best,
                     "out_excess": None if ex_out == float("-inf") else round(ex_out, 3), "in_excess": round(best_ex, 3),
                     "in_ci": best_row.get("ci"), "in_days": best_row.get("days"), "in_filled": best_row.get("filled"),
                     "out_hits": (row_out or {}).get("hits") or 0}
    return out


def _names(data_dir: Path) -> dict[str, str]:
    try:
        from app.services import rank_lab
        return rank_lab.ranker_names(data_dir)
    except Exception:
        return {}


def name_of(data_dir: Path, rule: str, side: str, rk: dict[str, str] | None = None) -> str:
    from app.services import swing_desk as sd
    rk = _names(data_dir) if rk is None else rk
    return rk.get(rule) or sd.strategy_name(rule, side)


def run_weekly(data_dir: Path, engine, *, day: date, force: bool = False) -> dict | None:
    """评定日收盘后调用 (在 rule_forward.run_daily 之后)。不是评定日 -> None。已评过同一天 -> 原记录。"""
    from app.market_clock import us_clock
    from app.services import rule_forward as rf
    from app.services import swing_desk as sd
    data_dir = Path(data_dir)
    if not force and not is_eval_day(day):
        return None
    log = read_log(data_dir)
    for e in log["evals"]:
        if e.get("day") == day.isoformat():
            return e
    table = rf.read_summary(data_dir)
    on = enabled(data_dir)
    rec: dict = {"day": day.isoformat(), "enabled": on, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not table or table.get("as_of") != day.isoformat():
        rec["sides"] = {s: {"action": "none", "reason": "no_scores"} for s in SIDES}
    else:
        lg, sh = sd.strategies_from_ruleset(data_dir)
        cur = {"long": list(lg), "short": list(sh)}
        rec["sides"] = propose(table, cur, eligible(data_dir, engine), log["swaps"], day,
                               lambda d0: len(us_clock.trading_days_between(us_clock.next_trading_day(d0), day)))
        rk = _names(data_dir)
        new = {s: list(cur[s]) for s in SIDES}
        done = []
        for side in SIDES:
            p = rec["sides"][side]
            if p["action"] != "swap":
                continue
            p["out_name"], p["in_name"] = name_of(data_dir, p["out"], side, rk), name_of(data_dir, p["in"], side, rk)
            if not on:
                p["applied"] = False
                continue
            new[side] = [p["in"] if x == p["out"] else x for x in new[side]]
            done.append({"id": f"{day.isoformat()}-{side}", "day": day.isoformat(), "side": side,
                         **{k: p.get(k) for k in ("out", "in", "out_name", "in_name", "out_excess", "in_excess", "in_ci",
                                                  "in_days", "in_filled")}, "undone_at": None})
        if done:
            sd.save_strategies(data_dir, new["long"], new["short"])
            for side in SIDES:
                if rec["sides"][side]["action"] == "swap":
                    rec["sides"][side]["applied"] = True
            log["swaps"] += done
            _notify(data_dir, done)
    log["evals"] = (log["evals"] + [rec])[-KEEP_EVALS:]
    _write(log_path(data_dir), log)
    return rec


def _fmt(v) -> str:
    return "—" if not isinstance(v, (int, float)) else f"{v:+.2f}".replace("-", "−")  # noqa: RUF001


def _notify(data_dir: Path, done: list[dict]) -> None:
    try:
        from app.services.notify_opportunity import enqueue_global
        lines = []
        for s in done:
            lines.append(f"- **{'做多' if s['side'] == 'long' else '做空'}**：{s['out_name']} → {s['in_name']}"  # noqa: RUF001
                         f"（超额 {_fmt(s['out_excess'])} → {_fmt(s['in_excess'])} R，{s['in_days']} 个信号日 {s['in_filled']} 笔）")  # noqa: RUF001
        lines.append("下一个信号日起生效；机会页齿轮可撤回或关闭自动选策略。")  # noqa: RUF001
        enqueue_global(data_dir, "auto-select:" + ",".join(s["id"] for s in done),
                       {"title": "ZenTrade · 入选策略自动调整", "subtitle": done[0]["day"], "body": "\n".join(lines)},
                       event_type="auto_select", data={"swaps": done})
    except Exception as e:
        logger.warning("auto select notify failed (soft): %s", type(e).__name__)


def undo(data_dir: Path, swap_id: str) -> dict:
    """撤回一次换入: 把换入的那条换回被换出的那条。换入的已不在入选里 / 被换出的已在 -> ValueError("changed_since");
    被换出的是排序因子且已被移除 (公式挖掘退席 / 手动移除) -> ValueError("out_removed"): 写回去信号表不会再给它算值。"""
    from app.services import swing_desk as sd
    data_dir = Path(data_dir)
    log = read_log(data_dir)
    s = next((x for x in log["swaps"] if x.get("id") == swap_id), None)
    if s is None:
        raise KeyError(swap_id)
    if s.get("undone_at"):
        raise ValueError("already_undone")
    lg, sh = sd.strategies_from_ruleset(data_dir)
    cur = {"long": list(lg), "short": list(sh)}
    side = s["side"]
    if s["in"] not in cur[side] or s["out"] in cur[side]:
        raise ValueError("changed_since")
    from app.services import rank_lab
    if rank_lab.is_ranker(s["out"]) and s["out"] not in rank_lab.read_rankers(data_dir):
        raise ValueError("out_removed")
    cur[side] = [s["out"] if x == s["in"] else x for x in cur[side]]
    sd.save_strategies(data_dir, cur["long"], cur["short"])
    s["undone_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write(log_path(data_dir), log)
    return s


def view(data_dir: Path, *, today: date | None = None) -> dict:
    """页面与齿轮共用的只读视图: 开关、入选策略各自的前向成绩、上次 / 下次评定、换入记录、候补计数。"""
    from app.market_clock import us_clock
    from app.services import rule_forward as rf
    from app.services import swing_desk as sd
    data_dir = Path(data_dir)
    today = today or us_clock.today()
    table = rf.read_summary(data_dir) or {}
    rows = rf.by_rule(table, "all")
    lg, sh = sd.strategies_from_ruleset(data_dir)
    rk = _names(data_dir)
    keep = ("hits", "filled", "days", "fill", "mean_r", "excess", "ci", "verdict")

    def item(rule: str, side: str) -> dict:
        x = _row(rows, rule, side) or {}
        return {"id": rule, "side": side, "name": name_of(data_dir, rule, side, rk), **{k: x.get(k) for k in keep},
                "verdict": x.get("verdict") or "thin"}
    picks = {"long": [item(r, "long") for r in lg], "short": [item(r, "short") for r in sh]}
    bench = []
    for key, x in rows.items():
        rule = key[:-6] if key.endswith(":short") else key
        if rule in BAN or rule in (lg if x["side"] == "long" else sh):
            continue
        bench.append({"id": rule, "side": x["side"], "name": name_of(data_dir, rule, x["side"], rk), **{k: x.get(k) for k in keep}})
    bench.sort(key=lambda r: (r["verdict"] == "thin", -(r["excess"] if isinstance(r.get("excess"), (int, float)) else -9), r["name"]))
    log = read_log(data_dir)
    last = log["evals"][-1] if log["evals"] else None
    last_day = date.fromisoformat(last["day"]) if last else None
    nxt = today if (is_eval_day(today) and us_clock.is_trading_day(today) and last_day != today) else next_eval_day(today)
    return {"contract": CONTRACT, "enabled": enabled(data_dir), "picks": picks, "bench": bench,
            "scored": len(rows), "days": table.get("days") or 0, "cells": table.get("cells") or 0, "window": table.get("window"),
            "as_of": table.get("as_of"), "need_days": rf.VERDICT_DAYS, "need_filled": rf.MIN_FILLED, "cooldown_days": COOLDOWN_DAYS,
            "last_eval": last, "next_eval": nxt.isoformat(), "swaps": list(reversed(log["swaps"]))[:20]}
