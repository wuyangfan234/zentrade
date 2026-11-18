"""规则前向成绩 (全格子记分): 每个信号日、当天自选里的每只票、多空两个方向各记一张“格子卡”, 只结算、不下单。

为什么要有它: 机会板只给上过板的卡和现行 3+3 被砍 / 被 4h 门挡的卡记成绩。其余几十条规则每天也有命中、也进了
信号表, 但没人给它们记前向成绩 —— 想知道一条新规则好不好, 只能先把它勾进 3+3 再等几个月。这里把全部规则
一起记: 同一套紧几何 (swing_desk.geometry)、同一个回放 (swing_settlement._settle_one), 每条规则都从今天起攒成绩。

怎么比 (相对同日同向): 单看每笔 R, 大盘一跌做多全亏、一涨全赚, 看不出规则本身好坏。每张成交的格子卡减去
“同一天、同方向、其余成交格子”的平均 R (不含自己), 剩下的才是这条规则比随手挑多出来的部分; 区间按信号日整组重抽。
同类做法: alphalens demean_forward_returns (按日去均值)、GrowthBook RegressionAdjustedStatistic (用协变量降方差)。

边界 (守门测试锁住): 只写 opportunities/cells/ 与 research/swing_settle/cells.*.jsonl; 不进下单簿、台账、交易环、
准入、推送; 不改 3+3, “过线”只是标记, 勾不勾由人决定。名单取信号表当天冻结的 watchlist (当时名单, 不是事后名单)。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT = "rule-forward-v1"
MIN_FILLED = 15            # 成交不足这么多张不给结论 (只报张数)
MIN_DAYS = 8               # 有成交的信号日不足这么多个不给区间 (与 swing_settlement.cluster_ci 同值)
VERDICT_DAYS = 20          # 不足这么多个信号日只给区间、不下“过线 / 垫底”结论 (组太少时重抽区间偏窄)
N_BOOT = 4000
ALPHA = 0.05
SCOPES = ("gate", "all")   # gate = 只看 4h 门同向的格子 (上板卡所在的范围); all = 全部格子


def cells_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "opportunities" / "cells"


def cells_path(data_dir: Path, day: date) -> Path:
    return cells_dir(data_dir) / f"{day.isoformat()}.json"


def outcomes_path(data_dir: Path) -> Path:
    from app.services import swing_settlement as ss
    return ss.settle_dir(data_dir) / f"cells.{ss.CONTRACT}.jsonl"


def cell_id(day: date, symbol: str, side: str) -> str:
    return f"cell-{day.isoformat()}-{symbol}-{side}"


def _gate(symbol: str, side: str, events: dict[str, set], h4: dict) -> str:
    """event / state = 4h 门同向 (与 compose_boards 同一判法); opp = 4h 反向; none = 4h 震荡或没有状态。"""
    if symbol in events[side]:
        return "event"
    st = h4.get(symbol)
    if st == side:
        return "state"
    return "opp" if st in ("long", "short") else "none"


def build_cells(snap: dict, day: date) -> list[dict]:
    """信号表 -> 当天全部格子卡 (纯函数)。规则 = 该方向命中的全部规则 (4h 来源本身不算规则, 它是门);
    排序因子: 信号表里有值的每一条, 在 4h 门同向且能出卡的票里取前 5 记作命中 (与上板同一选法)。"""
    from app.services import swing_desk as sd
    closes = {k: float(v) for k, v in (snap.get("closes") or {}).items() if isinstance(v, (int, float))}
    atr = {k: float(v) for k, v in (snap.get("atr") or {}).items() if isinstance(v, (int, float))}
    h4_raw = snap.get("h4_state")
    h4: dict = h4_raw if isinstance(h4_raw, dict) else {}
    signals = [s for s in (snap.get("signals") or []) if isinstance(s, dict) and isinstance(s.get("symbol"), str)]
    ph_l, ph_s = sd._phantom_maps(signals)
    events = {"long": set(ph_l), "short": set(ph_s)}
    hits: dict[tuple[str, str], set[str]] = {}
    for s in signals:
        rule = s.get("rule")
        if not isinstance(rule, str) or rule.startswith("phantom_4h"):
            continue
        hits.setdefault((s["symbol"], s.get("side") or "long"), set()).add(rule)
    watch = [s for s in (snap.get("watchlist") or []) if isinstance(s, str)]
    geo = {(s, side): sd.geometry(side, closes.get(s), atr.get(s)) for s in watch for side in ("long", "short")}
    rv = snap.get("rank_values") if isinstance(snap.get("rank_values"), dict) else None
    if rv:
        try:
            from app.services import rank_lab
            elig = {s for s in watch if geo[(s, "long")] is not None and _gate(s, "long", events, h4) in ("event", "state")}
            for row in rank_lab.board_hits(rv, tuple(sorted(rv)), elig):
                hits.setdefault((row["symbol"], "long"), set()).add(row["rule"])
        except Exception as e:
            logger.warning("rule forward: rank hits skipped: %s", type(e).__name__)
    cards = []
    for s in sorted(set(watch)):
        for side in ("long", "short"):
            g = geo[(s, side)]
            if g is None:
                continue
            cards.append({"card_id": cell_id(day, s, side), "symbol": s, "side": side, "signal_day": day.isoformat(),
                          "zone": list(g["entry_zone"]), "stop": g["stop"], "target": g["target"],
                          "gate": _gate(s, side, events, h4), "rules": sorted(hits.get((s, side), ()))})
    return cards


def record_cells(data_dir: Path, day: date, *, now: datetime | None = None) -> dict | None:
    """信号日 day 的格子名单。已记过不改写; 没有信号表 / 信号表没有 ATR (出不了卡) -> 不记。返回写入的记录, 没写 = None。"""
    from app.market_clock import us_clock
    from app.services import swing_desk as sd
    from app.services.fs_utils import atomic_write_text
    p = cells_path(data_dir, day)
    if p.exists():
        return None
    snap = sd.load_watchlist_snapshot(data_dir, day)
    if snap is None:
        return None
    cards = build_cells(snap, day)
    if not cards:
        return None
    now = us_clock.market_datetime(now or us_clock.now())
    rec = {"contract": CONTRACT, "card_contract": sd.CONTRACT, "day": day.isoformat(),
           "watchlist": len(snap.get("watchlist") or []), "cards": cards,
           "built_at": now.isoformat(timespec="seconds"),
           # late = 入场窗第 1 天过后才补记的 (信号表是当晚冻结的, 补记仍是当时名单与当时命中)
           "late": us_clock.next_trading_day(day) < now.date()}
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(rec, ensure_ascii=False))
    return rec


def load_cells(data_dir: Path, day: date) -> dict | None:
    try:
        d = json.loads(cells_path(data_dir, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("cards"), list) else None


def read_outcomes(data_dir: Path) -> list[dict]:
    p = outcomes_path(data_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _signal_days(data_dir: Path, lo: date, hi: date) -> list[date]:
    from app.services import watchlist_v4 as wv
    out = []
    try:
        names = sorted(p.stem for p in wv.signals_dir(data_dir).glob("*.json"))
    except OSError:
        return []
    for n in names:
        try:
            d = date.fromisoformat(n[:10])
        except ValueError:
            continue
        if lo <= d <= hi:
            out.append(d)
    return out


def settle_cells(data_dir: Path, repo, *, as_of_close: date, lookback_days: int = 40) -> dict:
    """补记窗口内各信号日的格子名单, 再结算能定结局的格子卡 (与板卡同一个回放与作废规则), 只追加新行。"""
    from app.market_clock import us_clock
    from app.services import swing_settlement as ss
    done = {r.get("card_id") for r in read_outcomes(data_dir)}
    written, pending, recorded = [], 0, 0
    for d in _signal_days(data_dir, as_of_close - timedelta(days=lookback_days), as_of_close):
        try:
            recorded += 1 if record_cells(data_dir, d) is not None else 0
        except Exception as e:
            logger.warning("rule forward record %s failed: %s", d, type(e).__name__)
        rec = load_cells(data_dir, d)
        if rec is None:
            continue
        after = us_clock.trading_days_between(d + timedelta(days=1), as_of_close)
        for c in rec["cards"]:
            if c.get("card_id") in done:
                continue
            if not after:
                pending += 1
                continue
            try:
                row, _miss = ss._settle_one(data_dir, repo, {**c, "rules": c.get("rules") or []}, after, as_of_close)
            except Exception as e:
                logger.warning("rule forward settle %s failed: %s", c.get("card_id"), type(e).__name__)
                row = None
            if row is None:
                pending += 1
                continue
            written.append({**row, "gate": c.get("gate")})
            done.add(c["card_id"])
    ss._append_rows(outcomes_path(data_dir), written)
    return {"recorded": recorded, "written": len(written), "pending": pending}


def _boot(groups: dict[str, list[float]], qs: tuple[float, ...], *, n_boot: int = N_BOOT, seed: int = 7):
    """按信号日整组重抽的均值分位数。有数的组不足 MIN_DAYS -> None。"""
    import numpy as np
    ks = [k for k, v in groups.items() if v]
    if len(ks) < MIN_DAYS:
        return None
    sums = np.array([float(sum(groups[k])) for k in ks])
    cnts = np.array([len(groups[k]) for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n_boot, len(ks)))
    m = sums[pick].sum(1) / np.maximum(cnts[pick].sum(1), 1)
    return [float(np.percentile(m, q * 100)) for q in qs]


def _scope_rows(rows: list[dict], scope: str) -> list[dict]:
    shown = [r for r in rows if r.get("status") in ("no_fill", "closed")]
    return [r for r in shown if r.get("gate") in ("event", "state")] if scope == "gate" else shown


def table(data_dir: Path, rows: list[dict] | None = None) -> dict:
    """每条规则 x 方向的前向成绩, 两个范围 (gate / all) 各一份。
    excess = 成交格子的 R 减去同日同向其余成交格子的平均 R; ci = 95% 区间 (按信号日整组重抽);
    verdict: thin = 样本不足; early = 有区间但信号日不足 VERDICT_DAYS 个, 先不下结论;
    above / below = 按“同时看多少条就除以多少”收紧后的区间整体在 0 上 / 下; unclear = 跨 0。"""
    rows = read_outcomes(data_dir) if rows is None else rows
    out: dict = {"contract": CONTRACT, "min_filled": MIN_FILLED, "min_days": MIN_DAYS, "verdict_days": VERDICT_DAYS,
                 "scopes": {}}
    days = sorted({str(r.get("signal_day")) for r in rows if r.get("status") in ("no_fill", "closed")})
    out["window"] = [days[0], days[-1]] if days else None
    out["days"] = len(days)
    out["cells"] = sum(1 for r in rows if r.get("status") in ("no_fill", "closed"))
    for scope in SCOPES:
        pool = _scope_rows(rows, scope)
        peer: dict[tuple[str, str], list[float]] = {}
        for r in pool:
            if r.get("status") == "closed" and isinstance(r.get("r"), (int, float)):
                peer.setdefault((str(r["signal_day"]), r["side"]), []).append(float(r["r"]))
        by: dict[tuple[str, str], list[dict]] = {}
        for r in pool:
            for rule in r.get("rules") or []:
                by.setdefault((rule, r["side"]), []).append(r)
        prelim = []
        for (rule, side), xs in sorted(by.items()):
            filled = [r for r in xs if r.get("status") == "closed" and isinstance(r.get("r"), (int, float))]
            rs = [float(r["r"]) for r in filled]
            exc: dict[str, list[float]] = {}
            for r in filled:
                p = peer[(str(r["signal_day"]), side)]
                if len(p) > 1:
                    exc.setdefault(str(r["signal_day"]), []).append(float(r["r"]) - (sum(p) - float(r["r"])) / (len(p) - 1))
            ev = [x for v in exc.values() for x in v]
            prelim.append({"rule": rule, "side": side, "hits": len(xs), "filled": len(rs),
                           "days": len({str(r["signal_day"]) for r in filled}),
                           "fill": round(len(rs) / len(xs), 3) if xs else None,
                           "mean_r": round(sum(rs) / len(rs), 3) if rs else None,
                           "win": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else None,
                           "excess": round(sum(ev) / len(ev), 3) if ev else None, "_exc": exc})
        ready = [x for x in prelim if x["filled"] >= MIN_FILLED and len([1 for v in x["_exc"].values() if v]) >= MIN_DAYS]
        m = max(1, len(ready))
        a = ALPHA / m                                       # 同时看 m 条, 每条的门槛收紧到 0.05 / m
        for x in prelim:
            exc = x.pop("_exc")
            x["ci"] = None
            x["verdict"] = "thin"
            if x not in ready:
                continue
            q = _boot(exc, (0.025, 0.975, a / 2, 1 - a / 2))
            if q is None or not all(math.isfinite(v) for v in q):
                continue
            x["ci"] = [round(q[0], 3), round(q[1], 3)]
            if len([1 for v in exc.values() if v]) < VERDICT_DAYS:
                x["verdict"] = "early"
            else:
                x["verdict"] = "above" if q[2] > 0 else ("below" if q[3] < 0 else "unclear")
        out["scopes"][scope] = {"compared": m if ready else 0, "rules": prelim}
    return out


def by_rule(t: dict, scope: str = "all") -> dict[str, dict]:
    """table() -> {规则 id 或 "<id>:short": 那一行} (给规则控制台逐条取)。"""
    out = {}
    for x in ((t.get("scopes") or {}).get(scope) or {}).get("rules") or []:
        out[x["rule"] if x["side"] == "long" else f"{x['rule']}:short"] = x
    return out


def summary_path(data_dir: Path) -> Path:
    from app.services import swing_settlement as ss
    return ss.settle_dir(data_dir) / "rule_forward.json"


def read_summary(data_dir: Path) -> dict | None:
    try:
        d = json.loads(summary_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("contract") == CONTRACT else None


def run_daily(data_dir: Path, repo, *, day: date) -> dict:
    """收盘后: 补记 + 结算 + 重算成绩表落盘 (页面只读这份摘要, 请求内不重抽)。"""
    from app.services.fs_utils import atomic_write_text
    res = settle_cells(data_dir, repo, as_of_close=day)
    t = table(data_dir)
    p = summary_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({**t, "as_of": day.isoformat(),
                                     "built_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                    ensure_ascii=False))
    return res
