"""自选机会卡下单簿 (v4, 2026-10-10)。

无限 10-10: “纸面账户先按小风险开仓, 但是现在机会卡是多空两种方向的, 你按照合约期权的方式进行核算, 每笔 0.25%
(约 $250), 前向确认继续独立统计, 通过后再升到 1%, 系统的实用性是第一的”。

机会页的卡 = 自选信号表 (opportunities/watchlist/<day>.json) 回放出来的状态, 从来不进交易员的卡册; 交易员只认
opportunities/cards/<day>.json 里的 OPEN/ACTIVE 卡 (决策台在 v4 下全部只观察)。这里把“当前许可下单”的那部分
机会卡冻结成交易员能执行的卡, 存 opportunities/book/<信号日>.json, 由 opportunity_service.all_cards 一并读出:

- 看多: 规则集勾选且当前可下单的规则 (试用档 0.25% / 前向通过后 1%); 看空: 机会卡“看空”勾选、能出看空的规则 (试用档)。
- 一个信号日一只票一个方向一张卡; 同一信号日多空都有 → 方向冲突, 不下单。多条规则命中同一方向取一条
  (前向通过的规则优先, 其次按规则名), 几何 (入场区/失效/目标/每股风险) 原样取自信号表, 与机会页同一来源。
- 只收“截至最近收盘仍待触发”的信号: 已在过去某天触发/作废/到期的信号不再补开 (机会页那张卡已在持有或已消失)。
- 已落盘的簿不改写 (与决策台卡同为不可变提案); 是否真能开仓由 entry_guard 在执行时按当前许可再判。
- 试用开仓只进纸面台账, 不写影子账本 (前向确认的数据源), 前向统计不受影响。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import numpy as np

from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard

logger = logging.getLogger(__name__)

ENTRY_WINDOW = 5     # 与 strategy_evaluation.ENTRY_WINDOW / watchlist_promotion_days 一致 (测试锁定)


def book_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "book"


def book_path(data_dir: Path, day: date) -> Path:
    return book_dir(data_dir) / f"{day.isoformat()}.json"


_LOAD_CACHE: dict = {}
LIVE_DAYS = 40          # 入场窗 5 + 持有 7 个交易日 ≈ 17 个日历日; 40 天以前的簿卡不可能再成交或持有


def load_day(data_dir: Path, day: date) -> dict | None:
    """按文件签名缓存解析结果 (交易环每帧多次读卡)。返回深拷贝。"""
    import copy
    p = book_path(data_dir, day)
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _LOAD_CACHE.get(str(p))
    if hit and hit[0] == sig:
        return copy.deepcopy(hit[1])
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    d = d if isinstance(d, dict) and isinstance(d.get("cards"), list) else None
    _LOAD_CACHE[str(p)] = (sig, d)
    return copy.deepcopy(d)


def book_days(data_dir: Path) -> list[date]:
    out = []
    d = book_dir(data_dir)
    for f in d.glob("20*.json") if d.exists() else []:
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if f.stem == day.isoformat():
            out.append(day)
    return sorted(out)


def _side(x: dict) -> str:
    return "short" if x.get("side") == "short" else "long"


def _num(v) -> float | None:
    return float(v) if type(v) in (int, float) and np.isfinite(v) else None


def card_id(symbol: str, rule: str, side: str, day: date) -> str:
    return OpportunityCard.make_id(symbol, f"book-{rule}" + ("-short" if side == "short" else ""), day, 1)


def _replay_status(x: dict, bars, day: date, as_of_close: date) -> str:
    """信号日之后到 as_of_close 的已收盘日K 回放 (与机会卡/影子账本同一规则)。缺数据 → 'missing'。"""
    from app.services import card_replay as cr
    if bars is None or not bars.height:
        return "missing"
    upto = bars.filter(bars["date"] <= day)
    after = bars.filter((bars["date"] > day) & (bars["date"] <= as_of_close))
    if not upto.height or upto["date"][-1] != day:
        return "missing"
    expected = len(us_clock.trading_days_between(day, as_of_close)) - 1 if as_of_close > day else 0
    if after.height != max(expected, 0):
        return "missing"       # 单只票缺收盘: 不把“没数据”当“没触发”
    scale = 1.0
    ref = x.get("ref_close")
    now_close = float(upto["close"][-1])
    if isinstance(ref, (int, float)) and ref > 0 and now_close > 0 and abs(now_close / ref - 1) > 0.02:
        scale = ref / now_close
    o, h, lo, c = (after[k].to_numpy().astype(float) * scale for k in ("open", "high", "low", "close"))
    replay = cr.replay_state_short if _side(x) == "short" else cr.replay_state
    try:
        res = replay(o, h, lo, c, entry_zone=tuple(x["entry_zone"]), invalidation=x["invalidation"],
                     target=tuple(x["target_zone"]), risk=x["risk"], hold=cr.hold_days(x))
    except Exception:  # noqa: BLE001
        return "missing"
    return res["status"]


def _touched(geo_zone, bars, day: date, as_of_close: date) -> str:
    """tight-v1 卡: 信号日后到 as_of_close 的已收盘日K里入场区是否被触及。'missing' = 日K不全。"""
    if bars is None or not bars.height:
        return "missing"
    after = bars.filter((bars["date"] > day) & (bars["date"] <= as_of_close))
    expected = len(us_clock.trading_days_between(day, as_of_close)) - 1 if as_of_close > day else 0
    if after.height != max(expected, 0):
        return "missing"
    lo, hi = geo_zone
    for h, low in zip(after["high"].to_list(), after["low"].to_list(), strict=True):
        if h is not None and low is not None and h >= lo and low <= hi:
            return "touched"
    return "pending"


def build_swing_day(data_dir: Path, repo, day: date, *, as_of_close: date, watch: set[str], pm: dict) -> dict:
    """tight-v1 (0.24.0): 下单簿 = 机会板同一张卡 (swing_desk.board_for_day)。规则名 swing, 入场窗 2 日, 持有 5 日,
    限价回踩卡不适用“开盘越区 2% 作废”。只收截至最近收盘仍未触及入场区、且仍在 2 日入场窗内的卡。"""
    from app.services import swing_desk as sd
    from app.services import watchlist_v4 as wv
    out = {"as_of": day.isoformat(), "as_of_close": as_of_close.isoformat(), "cards": [], "skipped": [],
           "contract": sd.CONTRACT,
           "permission": {"ruleset": (pm.get("ruleset") or {}).get("version"),
                          "long": sorted(pm.get("tradable") or []), "short": sorted(pm.get("short_tradable") or [])},
           "built_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    n_after = len(us_clock.trading_days_between(day, as_of_close)) - 1 if as_of_close > day else 0
    if n_after >= sd.ENTRY_DAYS:
        out["skipped"].append({"reason": "entry_window_over"})
        return out
    board = sd.board_for_day(data_dir, day, repo)
    if board is None:
        out["skipped"].append({"reason": "no_signal_file"})
        return out
    out["swing_rules"] = {"long": board["long_rules"], "short": board["short_rules"]}   # 冻结本日 3+3 (板读这份)
    if board["meta"].get("short_gate") is None and board["meta"].get("raw_short_hits"):
        # SPY 数据缺: 空头未判 → 多空冲突也未知, 整天不落盘 (save_day 认这个原因), 下次重试
        out["incomplete"] = True
        out["skipped"].append({"reason": "short_gate_missing"})
        return out
    # 板上去掉的多空冲突也要进簿的 skipped, 让 save_day 给已落盘的同票卡打 conflict 标记
    out["skipped"] += [dict(x) for x in board["meta"].get("skipped") or [] if x.get("reason") == "direction_conflict"]
    cards = [c for c in board["long"] + board["short"] if c["symbol"] in watch]
    bars_by = {}
    if cards and as_of_close > day and repo is not None:
        try:
            from datetime import timedelta
            b = repo.get_daily_batch(sorted({c["symbol"] for c in cards}), day - timedelta(days=10), as_of_close,
                                     columns=["symbol", "date", "open", "high", "low", "close"])
            if b is not None and not b.is_empty():
                bars_by = {g["symbol"][0]: g.sort("date") for g in b.partition_by("symbol")}
        except Exception as e:
            logger.warning("swing book %s: daily bars unavailable: %s", day, type(e).__name__)
            out["skipped"].append({"reason": "bars_unavailable"})
            return out
    for c in cards:
        sym, side = c["symbol"], c["side"]
        ok, risk, tier = wv.rule_permission(pm, sd.RULE_ID, side)
        if not ok:
            out["skipped"].append({"symbol": sym, "side": side, "reason": "not_permitted"})
            continue
        if as_of_close > day:
            st = _touched(c["entry_zone"], bars_by.get(sym), day, as_of_close)
            if st == "missing":
                out["incomplete"] = True
                out["skipped"].append({"symbol": sym, "side": side, "reason": "bars_missing"})
                continue
            if st != "pending":
                out["skipped"].append({"symbol": sym, "side": side, "reason": "not_pending:touched"})
                continue
        decision = {"book": True, "tab": "watchlist", "tier": tier, "risk_pct": risk,
                    "sources": [sd.RULE_ID], "tradable_sources": [sd.RULE_ID], "sides": [side],
                    "hard_blockers": [], "rules": [x["id"] for x in c["strategies"]],
                    "accounting": "contract", "contract": sd.CONTRACT,
                    "entry_days": sd.ENTRY_DAYS, "limit_pullback": True, "gate_4h": c["gate"]}
        if side == "short" and board["meta"].get("spy"):
            decision["short_gate"] = {"day": day.isoformat(), **board["meta"]["spy"]}
        lo, hi = c["entry_zone"]
        card = OpportunityCard(
            id=card_id(sym, sd.RULE_ID, side, day), symbol=sym, setup_type=f"strategy:{sd.RULE_ID}",
            created_date=day, side=side, entry_zone=(float(lo), float(hi)), invalidation=float(c["stop"]),
            target_zone=(float(c["target"]), float(c["target"])), max_holding_days=sd.HOLD_DAYS,
            risk_per_share=float(c["risk"]), reward_risk=float(c["rr"]),
            evidence={"source": sd.RULE_ID, "side": side, "book": True, "signal_day": day.isoformat(),
                      "ref_close": c["close"], "atr14": c["atr"], "score": c["score"], "decision": decision},
            state=CardState.OPEN, state_reason="机会板" + ("做空" if side == "short" else "做多"))
        errs = card.validate(0.0)
        if errs:
            out["skipped"].append({"symbol": sym, "side": side, "reason": "invalid:" + errs[0]})
            continue
        out["cards"].append(card.to_dict())
    return out


def build_day(data_dir: Path, repo, day: date, *, as_of_close: date, watch: set[str], pm: dict) -> dict:
    """一个信号日的下单簿 (纯计算, 不写文件)。pm = watchlist_v4.permission(...)。
    tight-v1: 机会板模式 (rules/swing.json 存在) 走 build_swing_day, 板上卡 = 纸面卡。"""
    from app.services import swing_desk as _sd
    if _sd.swing_config_path(data_dir).exists():
        return build_swing_day(data_dir, repo, day, as_of_close=as_of_close, watch=watch, pm=pm)
    from app.services import watchlist_v4 as wv
    payload = wv._read(wv.signals_dir(data_dir) / f"{day.isoformat()}.json")
    out = {"as_of": day.isoformat(), "as_of_close": as_of_close.isoformat(), "cards": [], "skipped": [],
           "permission": {"ruleset": (pm.get("ruleset") or {}).get("version"),
                          "long": sorted(pm.get("tradable") or []), "short": sorted(pm.get("short_tradable") or []),
                          "proven": sorted(pm.get("tradable") or [])},
           "built_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not isinstance(payload, dict):
        out["skipped"].append({"reason": "no_signal_file"})
        return out
    closes = payload.get("closes") or {}
    groups: dict[tuple[str, str], list[dict]] = {}
    gate = None              # 做空大盘门槛 (SPY 收盘 < 50 日线才出看空卡): 只在该日有看空信号时懒算一次
    # 0.22 第一性原理: 若已启用 swing.json, 纸面簿只收「4h 同向」的策略命中 (与机会板硬门一致)
    swing_on = (Path(data_dir) / "rules" / "swing.json").exists()
    ph_long, ph_short = set(), set()
    if swing_on:
        for s in payload.get("signals") or []:
            if not isinstance(s, dict):
                continue
            sym, rule = s.get("symbol"), s.get("rule")
            if rule == "phantom_4h" and s.get("side") != "short":
                ph_long.add(sym)
            elif rule == "phantom_4h_short" or (rule == "phantom_4h" and s.get("side") == "short"):
                ph_short.add(sym)
    for x in payload.get("signals") or []:
        sym, rule = x.get("symbol"), x.get("rule")
        if not isinstance(sym, str) or not isinstance(rule, str) or sym not in watch:
            continue
        side = _side(x)
        if swing_on:
            if side == "long" and sym not in ph_long:
                out["skipped"].append({"symbol": sym, "side": side, "rule": rule, "reason": "swing_no_4h"})
                continue
            if side == "short" and sym not in ph_short:
                out["skipped"].append({"symbol": sym, "side": side, "rule": rule, "reason": "swing_no_4h"})
                continue
            if rule in ("evo", "phantom_4h", "phantom_4h_short", "phantom_4h_pine"):
                continue  # 方向源本身不进簿; 由策略命中出卡
        ok, risk, tier = wv.rule_permission(pm, rule, side)
        if not ok:
            continue
        if side == "short":
            if gate is None:
                gate = wv.short_gate(repo, day)
            if gate["ok"] is None:            # SPY 数据缺/读失败: 整天不落盘, 下次重试
                out["incomplete"] = True
                out["skipped"].append({"symbol": sym, "side": side, "rule": rule, "reason": "short_gate_missing"})
                continue
            if gate["ok"] is False:           # 大盘不在 50 日线下: 看空信号直接拦在分组前 (不与同票看多冲突)
                out["skipped"].append({"symbol": sym, "side": side, "rule": rule, "reason": "short_gate"})
                continue
        geo = [_num(v) for v in (*(x.get("entry_zone") or [None, None])[:2], x.get("invalidation"),
                                 *(x.get("target_zone") or [None, None])[:2], x.get("risk"))]
        if any(v is None or v <= 0 for v in geo):
            out["skipped"].append({"symbol": sym, "side": side, "rule": rule, "reason": "bad_geometry"})
            continue
        groups.setdefault((sym, side), []).append({**x, "_risk_pct": risk, "_tier": tier,
                                                   "ref_close": closes.get(sym)})
    sides: dict[str, set] = {}
    for sym, side in groups:
        sides.setdefault(sym, set()).add(side)
    syms = sorted({s for s, _ in groups})
    bars_by = {}
    if syms and as_of_close > day and repo is not None:
        try:
            from datetime import timedelta
            b = repo.get_daily_batch(syms, day - timedelta(days=10), as_of_close,
                                     columns=["symbol", "date", "open", "high", "low", "close"])
            if b is not None and not b.is_empty():
                bars_by = {g["symbol"][0]: g.sort("date") for g in b.partition_by("symbol")}
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist book %s: daily bars unavailable: %s", day, type(e).__name__)
            out["skipped"].append({"reason": "bars_unavailable"})
            return out           # 读不到日K 不落盘 (调用方下次重试), 不凭空断言“仍待触发”
    per_rule = (pm.get("status") or {}).get("per_rule") or {}

    def weight(x):   # 与机会卡/自选表同序: 前向通过的规则优先, 再按前向超额 × 收缩, 再按规则名
        f = per_rule.get(x["rule"]) or {}
        ex, n = f.get("excess") or 0.0, f.get("n") or 0
        return max(ex, 0.0001) * n / (n + 100) if n else 0.0

    for (sym, side), cands in sorted(groups.items()):
        best = sorted(cands, key=lambda x: (-weight(x), x["rule"]))[0]
        if len(sides[sym]) > 1:
            out["skipped"].append({"symbol": sym, "side": side, "rule": best["rule"], "reason": "direction_conflict"})
            continue
        if as_of_close > day:
            st = _replay_status(best, bars_by.get(sym), day, as_of_close)
            if st == "missing":
                out["incomplete"] = True          # 缺日K: 整天不落盘, 下次重试 (不把“没数据”当“没触发”)
                out["skipped"].append({"symbol": sym, "side": side, "rule": best["rule"], "reason": "bars_missing"})
                continue
            if st != "pending":
                out["skipped"].append({"symbol": sym, "side": side, "rule": best["rule"], "reason": f"not_pending:{st}"})
                continue
        short = side == "short"
        from app.services import card_replay as cr
        decision = {"book": True, "tab": "watchlist", "tier": best["_tier"], "risk_pct": best["_risk_pct"],
                    "sources": [best["rule"]], "tradable_sources": [best["rule"]], "sides": [side],
                    "hard_blockers": [], "rules": sorted({c["rule"] for c in cands}),
                    "accounting": "contract"}
        if isinstance(best.get("evo"), dict):   # 自进化卡: 冻结当天分数与命中规则权重 (审计)
            decision["evo"] = {k: best["evo"].get(k) for k in ("kind", "score", "hold_days", "rules")}
        if short and gate:          # 审计: 记录出卡当天的大盘门槛取值
            decision["short_gate"] = {"day": gate["day"], "spy_close": gate["close"], "spy_ma50": gate["ma50"]}
        from app.services import decision_desk as dd
        setup = best["rule"] if best["rule"] in dd.BUILTIN_SETUPS else f"strategy:{best['rule']}"   # S1/S2 沿用原名
        card = OpportunityCard(
            id=card_id(sym, best["rule"], side, day), symbol=sym, setup_type=setup,
            created_date=day, side=side, entry_zone=(float(best["entry_zone"][0]), float(best["entry_zone"][1])),
            invalidation=float(best["invalidation"]),
            target_zone=(float(best["target_zone"][0]), float(best["target_zone"][1])),
            max_holding_days=cr.hold_days(best) or 7,
            risk_per_share=float(best["risk"]), reward_risk=float(best.get("rr") or 0.0),
            evidence={"source": best["rule"], "side": side, "book": True, "signal_card_id": best.get("card_id"),
                      "signal_day": day.isoformat(), "ref_close": best.get("ref_close"), "decision": decision},
            state=CardState.OPEN, state_reason="自选机会卡" + ("看空" if short else "看多"))
        errs = card.validate(0.0)
        if errs:
            out["skipped"].append({"symbol": sym, "side": side, "rule": best["rule"], "reason": "invalid:" + errs[0]})
            continue
        out["cards"].append(card.to_dict())
    return out


def save_day(data_dir: Path, built: dict) -> int:
    """落盘一个信号日的簿, 返回新增卡数。已落盘的卡永不改写 (不可变提案); 之后新获许可的规则 (例如规则集新勾选)
    只追加“该票该方向尚无卡、且不与已有卡方向冲突”的新卡。缺数据的构建不落盘 (下次重试)。"""
    from app.services.fs_utils import atomic_write_text
    # 单票缺日K只跳过那只票 (该票下次补齐后追加); 整批读不到日K / 没有信号表才整天不落盘
    if any(s.get("reason") in ("bars_unavailable", "no_signal_file", "short_gate_missing")
           for s in built.get("skipped") or []):
        return 0
    day = date.fromisoformat(built["as_of"])
    p = book_path(data_dir, day)
    old = load_day(data_dir, day) if p.exists() else None
    if p.exists() and old is None:
        return 0                     # 已有文件读不出: 不覆盖 (人工处理)
    if old is None:
        if not built["cards"]:
            built = {**built, "cards": []}
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(built, ensure_ascii=False, indent=1))
        return len(built["cards"])
    have = {c["symbol"]: c.get("side", "long") for c in old["cards"]}
    add = [c for c in built["cards"] if c["symbol"] not in have]
    # 已落盘的卡事后出现同日反向信号 (例如之后才勾选某条看空规则): 标冲突, 执行时拦下 (卡本身不改写)
    conflict_syms = {s["symbol"] for s in built.get("skipped") or [] if s.get("reason") == "direction_conflict"}
    new_conf = sorted({c["id"] for c in old["cards"] if c["symbol"] in conflict_syms} - set(old.get("conflict_ids") or []))
    if not add and not new_conf:
        return 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    merged = {**old, "cards": old["cards"] + [{**c, "evidence": {**c["evidence"], "added_at": now}} for c in add],
              "appended": [*(old.get("appended") or []), {"at": now, "ids": [c["id"] for c in add],
                                                           "permission": built.get("permission")}]}
    if new_conf:
        merged["conflict_ids"] = sorted(set(old.get("conflict_ids") or []) | set(new_conf))
    atomic_write_text(p, json.dumps(merged, ensure_ascii=False, indent=1))
    return len(add)


def window_days(data_dir: Path, today: date, as_of_close: date) -> list[date]:
    """入场窗仍可能开着的信号日: 信号日之后的交易日 (截至 today) ≤ ENTRY_WINDOW, 且信号日 ≤ 已收盘日。"""
    from app.services import watchlist_v4 as wv
    out = []
    for d, _ in wv.signal_files(data_dir):
        if d > as_of_close:
            continue
        n = len(us_clock.trading_days_between(d, today)) - (1 if us_clock.is_trading_day(d) else 0)
        if n <= ENTRY_WINDOW:
            out.append(d)
    return out


_ENSURE_KEY: dict = {}
_ENSURE_RETRY: dict = {}
RETRY_AFTER_S = 300


def ensure(data_dir: Path, repo, *, today: date, as_of_close: date, watch: set[str] | None = None) -> dict[str, int]:
    """补齐窗口内各信号日的簿 (幂等; 同一输入只算一次)。v4 之外或无许可 → 不写。返回 {信号日: 新增卡数}。"""
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation
    from app.services import watchlist_v4 as wv
    if strategy_evaluation.evidence_mode(data_dir) != "v4":
        return {}
    pm = wv.permission_cached(data_dir)
    if not (pm.get("tradable") or pm.get("short_tradable")):
        return {}
    watch = set(job._watchlist()) if watch is None else set(watch)
    if not watch:
        return {}
    days = window_days(data_dir, today, as_of_close)

    def sig_key():   # 输入签名 (含簿文件自身: 写入后签名变化, 所以缓存一律在写完之后取)
        from app.services import swing_desk as _sd
        return (today, as_of_close, tuple(sorted(watch)), wv._sig(_sd.swing_config_path(data_dir)),
                tuple(sorted(pm.get("tradable") or [])),
                tuple(sorted(pm.get("short_tradable") or [])),
                tuple((d, wv._sig(wv.signals_dir(data_dir) / f"{d.isoformat()}.json"), wv._sig(book_path(data_dir, d)))
                      for d in days))

    import time
    key = sig_key()
    if _ENSURE_KEY.get(str(data_dir)) == key:
        return {}
    retry = _ENSURE_RETRY.get(str(data_dir))
    if retry and retry[0] == key and time.monotonic() - retry[1] < RETRY_AFTER_S:
        return {}                     # 上次缺数据: 5 分钟内不重读日K (盘中每 15 秒一轮)
    added, complete = {}, True
    for d in days:
        try:
            built = build_day(data_dir, repo, d, as_of_close=as_of_close, watch=watch, pm=pm)
            if built.get("incomplete") or any(s.get("reason") == "bars_unavailable" for s in built["skipped"]):
                complete = False
            n = save_day(data_dir, built)
            if n:
                added[d.isoformat()] = n
        except Exception as e:  # noqa: BLE001
            complete = False
            logger.warning("watchlist book %s build failed: %s", d, type(e).__name__)
    if complete:
        _ENSURE_KEY[str(data_dir)] = sig_key()
        _ENSURE_RETRY.pop(str(data_dir), None)
    else:
        _ENSURE_RETRY[str(data_dir)] = (sig_key(), time.monotonic())
    if added:
        logger.info("watchlist book cards added: %s", added)
    return added
