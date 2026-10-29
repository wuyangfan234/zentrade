"""卡级结局结算 (方向 A, 2026-10-11 产品决策)。研究级: 只读自选信号表与日K, 只写 data/research/settlement/。

结算对象 = 自选信号表 (opportunities/watchlist/<信号日>.json) 的每一行 = 规则 x 票 x 方向 x 信号日, 含试用/下单/观察/记账全部档位。
回放直接调用 card_replay.replay_state / replay_state_short (与交易员、影子账本同一规则, 本模块不另写出场逻辑):
  多头目标按盘中最高价成交、止损只看收盘 (破了次日开盘离场); 空头镜像 (最低触及近端 target[1] 回补一半、远端 target[0] 回补余仓, BH-03)、收盘站上止损次日开盘回补。
  因此同一根K“先碰止损还是目标”不存在歧义; 另记 same_bar (同一根K 盘中既触目标又触失效价) 供人工看保守口径。

结局 (按卡实际状态, 7 个交易日只是最低保护期):
  HIT       已触发且已离场 (止损/追踪/保本/目标/第 7 日), 且信号日后已收盘 >= 7 个交易日
  EXP       入场窗 5 个交易日走完未触发 (且过保护期) —— 计“未触发”, 不计胜负
  PASS      开盘越过入场区 2% 作废 —— 当天结算; 「能成交」表计作废不计胜负; 「信号准」表按跳空当天开盘价虚拟入场,
            R 分母 = 跳空后的实际风险 |虚拟入场 - 失效价| (另记按原风险折算的 signal_r_orig)
  UNSETTLED 窗内未触发 / 持有中 / 已终结但未过保护期
  MISSING   缺日K (单独计数, 不当成未触发)
只追加明细 (唯一键 = 卡 + record_kind): settled (HIT/EXP, 终结且过保护期写一次, 两表共用) / pass_void (PASS 当天写, 进能成交)
/ pass_signal (PASS 虚拟交易走完且过保护期后写, 进信号准)。已写的行永不改写; 口径变了升 CONTRACT 写新文件。

同日随机入场基准: 宇宙 = 该信号日信号表里冻结的自选名单 (剔除本票、剔除缺价的票), 同方向, 同一根成交K 开盘入场,
同一根离场K 离场 (卡开盘离场取开盘, 收盘离场取收盘, 盘中按目标价离场取收盘), 不设止损目标, 滑点与费用同卡;
K = 200 次有放回抽样, 种子 = sha256(卡键 + 契约)。excess_r = (卡收益 - 基准均值) x 入场价 / 每股风险。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

CONTRACT = "zt-settle-v1"
MIN_AGE = 7            # 最低保护期 (信号日之后已收盘交易日数)
BENCH_K = 200
N_GRAY = 20            # n < 20 灰显, 数字不出
N_COLOR = 30           # n >= 30 且超额区间不跨 0 才着色 (与试用止血 TRIAL_STOP_MIN_N 一致)
BOOT_DRAWS = 2000
BOOT_SEED = 7
MIN_BLOCKS = 10         # 按信号日分块的自助法: 不同信号日少于这么多 → 不给区间 (只有 2-3 天时区间窄得失真), 不着色
STOP_REASONS = ("invalidation", "trailing_stop", "breakeven_after_partial")


def settle_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "settlement"


def cards_path(data_dir: Path) -> Path:
    return settle_dir(data_dir) / f"cards.{CONTRACT}.jsonl"


def provenance(payload: dict, row: dict) -> str:
    """live = 盘后扫描当时写出的行; backfill = 事后补算 (整份 backfill, 或 10-09 补写的 TV / 空头行)。"""
    if payload.get("backfill"):
        return "backfill"
    rule = str(row.get("rule") or "")
    if payload.get("tv_backfill_at") and (rule.startswith("tv_") or rule == "phantom_4h_pine"):
        return "backfill"
    if payload.get("short_backfill_at") and row.get("side") == "short":
        return "backfill"
    return "live"


def bench_exit_at_open(reason: str, side: str, *, exit_open: float, target, same_bar_as_fill: bool) -> bool:
    """卡在离场那根K 是不是按开盘价离场 (基准就取同一价位类型)。"""
    if reason in STOP_REASONS:
        return True
    if same_bar_as_fill or not np.isfinite(exit_open):
        return False
    if side == "short":
        return reason == "target_zone_upper" and exit_open <= target[0]   # 空头远端 = target[0] (BH-03 两段)
    return reason == "target_zone_upper" and exit_open >= target[1]


def _replay(side):
    from app.services import card_replay as cr
    return cr.replay_state_short if side == "short" else cr.replay_state


def _first(arrs, side, kw, want: str):
    """逐根加长回放, 找第一次出现 want 状态的位置 (今日收盘置空: 只判入场, 与盘中判定同一用法)。"""
    from app.services import strategy_evaluation as se
    o, h, lo, c = arrs
    for m in range(1, min(se.ENTRY_WINDOW, len(c)) + 1):
        c2 = c[:m].copy()
        c2[-1] = np.nan
        res = _replay(side)(o[:m], h[:m], lo[:m], c2, **kw)
        if res["status"] == want:
            return m - 1, res
        if res["status"] not in ("pending",):
            return None, res
    return None, None


def _same_bar(arrs, side, x, i0, i1) -> bool:
    o, h, lo, c = arrs
    for i in range(i0, min(i1, len(c) - 1) + 1):
        if side == "short":
            if lo[i] <= x["target_zone"][1] and h[i] >= x["invalidation"]:
                return True
        elif h[i] >= x["target_zone"][0] and lo[i] <= x["invalidation"]:
            return True
    return False


def _bench(key: str, side: str, sym: str, universe: list[str], px: dict, fill_d: date, exit_d: date, at_open: bool,
           ret: float, fee_frac: float) -> dict:
    from app.services import strategy_evaluation as se
    elig = []
    for s in sorted(set(universe) - {sym}):
        a, b = px.get(s, {}).get(fill_d), px.get(s, {}).get(exit_d)
        if a and b and np.isfinite(a[0]) and a[0] > 0 and np.isfinite(b[0 if at_open else 1]):
            elig.append((a[0], b[0 if at_open else 1]))
    if not elig:
        return {"bench_ret": None, "bench_pctl": None, "bench_n": 0}
    rng = random.Random(int(hashlib.sha256(f"{key}|{CONTRACT}".encode()).hexdigest()[:16], 16))
    rets = []
    for _ in range(BENCH_K):
        p0, p1 = elig[rng.randrange(len(elig))]
        if side == "short":
            e0 = p0 * (1 - se.SLIP)
            rets.append((e0 - p1 * (1 + se.SLIP)) / e0 - fee_frac)
        else:
            e0 = p0 * (1 + se.SLIP)
            rets.append((p1 * (1 - se.SLIP) - e0) / e0 - fee_frac)
    return {"bench_ret": round(statistics.fmean(rets), 6), "bench_pctl": round(sum(r < ret for r in rets) / len(rets), 4),
            "bench_n": len(elig)}


def _trade(x, side, arrs, dates, fill_i, entry, res, risk, key, universe, px, sym) -> dict:
    """已平仓一笔 → 日期、收益、基准、超额。"""
    from app.services import strategy_evaluation as se
    exit_i = fill_i + res["days"] - 1
    ret = res["r"] * risk / entry
    at_open = bench_exit_at_open(res["exit_reason"], side, exit_open=float(arrs[0][exit_i]) if exit_i < len(dates) else np.nan,
                                 target=x["target_zone"], same_bar_as_fill=exit_i == fill_i)
    fee_frac = se.FEE_R(risk, entry) * risk / entry
    b = _bench(key, side, sym, universe, px, dates[fill_i], dates[exit_i], at_open, ret, fee_frac) \
        if exit_i < len(dates) else {"bench_ret": None, "bench_pctl": None, "bench_n": 0}
    ex = (ret - b["bench_ret"]) if b["bench_ret"] is not None else None
    return {"fill_date": dates[fill_i].isoformat(), "exit_date": dates[exit_i].isoformat() if exit_i < len(dates) else None,
            "hold_days": res["days"], "entry": round(float(entry), 4), "exit_reason": res["exit_reason"], "r": res["r"],
            "ret": round(float(ret), 6), "bench_exit": "open" if at_open else "close", **b,
            "excess_ret": round(ex, 6) if ex is not None else None,
            "excess_r": round(ex * entry / risk, 4) if ex is not None else None}


def evaluate(x: dict, frame, day: date, as_of_close: date, *, universe: list[str], px: dict) -> dict:
    """一行信号 → 结局 (纯计算)。frame = 该票日K (polars, 升序)。"""
    from app.services import strategy_evaluation as se
    side = "short" if x.get("side") == "short" else "long"
    key = f"{day.isoformat()}|{x.get('rule')}|{x.get('symbol')}|{side}"
    out = {"key": key, "card_id": x.get("card_id") or key, "signal_day": day.isoformat(), "rule": x.get("rule"),
           "symbol": x.get("symbol"), "side": side, "outcome": "MISSING", "state": "missing", "final": False,
           "final_fill": False, "signal_final": False, "age": None, "r": None, "excess_r": None, "same_bar": False}
    if frame is None or not frame.height:
        return out
    upto = frame.filter(frame["date"] <= day)
    after = frame.filter((frame["date"] > day) & (frame["date"] <= as_of_close))
    if not upto.height or upto["date"][-1] != day:
        return out
    expected = len(us_clock.trading_days_between(day, as_of_close)) - 1 if as_of_close > day else 0
    if after.height != max(expected, 0):
        return out
    scale = 1.0
    ref = x.get("ref_close")
    now_close = float(upto["close"][-1])
    if isinstance(ref, (int, float)) and ref > 0 and now_close > 0 and abs(now_close / ref - 1) > 0.02:
        scale = ref / now_close
    arrs = tuple(after[k].to_numpy().astype(float) * scale for k in ("open", "high", "low", "close"))
    dates = after["date"].to_list()
    from app.services import card_replay as cr
    kw = dict(entry_zone=tuple(x["entry_zone"]), invalidation=x["invalidation"], target=tuple(x["target_zone"]), risk=x["risk"],
              hold=cr.hold_days(x))       # evo 行 = 5 日 (单张卡最多一周), 其余 None = 7 日
    try:
        res = _replay(side)(*arrs, **kw)
    except Exception:  # noqa: BLE001
        return out
    age = len(dates)
    st = res["status"]
    out.update(state=st, age=age, outcome="UNSETTLED")
    if st == "closed":
        fill_i, trig = _first(arrs, side, kw, "triggered")
        if fill_i is None:
            return out                                          # 同根成交即离场等极端路径: 留待人工
        out.update(_trade(x, side, arrs, dates, fill_i, trig["entry"], res, x["risk"], key, universe, px, x["symbol"]))
        out["same_bar"] = _same_bar(arrs, side, x, fill_i, fill_i + res["days"] - 1)
        if age >= MIN_AGE:
            out.update(outcome="HIT", final=True, final_fill=True)
    elif st == "expired":
        if age >= MIN_AGE:
            out.update(outcome="EXP", final=True, final_fill=True)
    elif st == "gap_expired":
        out.update(outcome="PASS", final_fill=True)
        g, _ = _first(arrs, side, kw, "gap_expired")
        if g is None:
            out["signal_state"] = "invalid"
            return out
        op = float(arrs[0][g])
        lo, hi = kw["entry_zone"]
        if side == "short":
            ez, entry_v = (min(lo, op), hi), op * (1 - se.SLIP)
            risk_v = x["invalidation"] - entry_v
        else:
            ez, entry_v = (lo, max(hi, op)), op * (1 + se.SLIP)
            risk_v = entry_v - x["invalidation"]
        out.update(gap_date=dates[g].isoformat(), signal_entry=round(entry_v, 4), signal_risk=round(float(risk_v), 6))
        if not risk_v > 0:
            out.update(signal_state="invalid", signal_final=True, signal_r=None)
            return out
        sl = tuple(a[g:] for a in arrs)
        rv = _replay(side)(*sl, **{**kw, "entry_zone": ez, "risk": risk_v})
        out["signal_state"] = rv["status"]
        if rv["status"] == "closed":
            t = _trade(x, side, sl, dates[g:], 0, entry_v, rv, risk_v, key, universe, px, x["symbol"])
            out.update(signal_r=t["r"], signal_r_orig=round(t["r"] * risk_v / x["risk"], 4), signal_ret=t["ret"],
                       signal_exit_date=t["exit_date"], signal_exit_reason=t["exit_reason"],
                       signal_bench_ret=t["bench_ret"], signal_excess_r=t["excess_r"])
            out["signal_final"] = age >= MIN_AGE
    return out


# ── 全量计算 ──────────────────────────────────────────────

def _meta(data_dir: Path, payload: dict, x: dict, cache: dict) -> dict:
    from app.services import card_lineage as cl
    lin = payload.get("lineage")
    rule = x.get("rule")
    if isinstance(lin, dict):
        fp = (lin.get("strategy_fingerprint") or {}).get(rule)
        return {"ruleset_version": lin.get("ruleset_version"), "mode": x.get("mode"), "params_hash": x.get("params_hash"),
                "strategy_fingerprint": fp, "lineage_src": "stamped", "rule_family": cl.rule_family(rule)}
    day = payload.get("as_of")
    if day not in cache:
        try:
            cache[day] = cl.reconstruct(data_dir, payload)
        except Exception:  # noqa: BLE001
            cache[day] = None
    m = cache[day]
    if m is None:
        return {"ruleset_version": None, "mode": None, "params_hash": None, "strategy_fingerprint": None,
                "lineage_src": "unknown", "rule_family": cl.rule_family(rule)}
    return {"ruleset_version": m["ruleset_version"], "mode": m["mode"](x), "params_hash": m["params_hash"](x),
            "strategy_fingerprint": None, "lineage_src": "reconstructed", "rule_family": cl.rule_family(rule)}


def compute(data_dir: Path, repo, *, as_of_close: date) -> dict:
    from app.services import watchlist_v4 as wv
    data_dir = Path(data_dir)
    files = [(d, f) for d, f in wv.signal_files(data_dir) if d <= as_of_close]
    payloads = []
    for d, f in files:
        p = wv._read(f)
        if isinstance(p, dict) and isinstance(p.get("signals"), list):
            payloads.append((d, p))
    syms = sorted({x.get("symbol") for _, p in payloads for x in p["signals"] if isinstance(x.get("symbol"), str)}
                  | {s for _, p in payloads for s in p.get("watchlist") or [] if isinstance(s, str)})
    by_sym, px = {}, {}
    if syms and repo is not None and payloads:
        try:
            start = min(d for d, _ in payloads) - timedelta(days=10)
            b = repo.get_daily_batch(syms, start, as_of_close, columns=["symbol", "date", "open", "high", "low", "close"])
            if b is not None and not b.is_empty():
                for g in b.partition_by("symbol"):
                    g = g.sort("date")
                    by_sym[g["symbol"][0]] = g
                    px[g["symbol"][0]] = {dd: (float(o), float(c)) for dd, o, c in
                                          zip(g["date"].to_list(), g["open"].to_list(), g["close"].to_list())}
        except Exception as e:  # noqa: BLE001
            logger.warning("card settlement: daily bars unavailable: %s", type(e).__name__)
    rows, cache = [], {}
    for d, p in payloads:
        closes = p.get("closes") or {}
        universe = [s for s in p.get("watchlist") or [] if isinstance(s, str)]
        for x in p["signals"]:
            if not (isinstance(x, dict) and isinstance(x.get("symbol"), str) and isinstance(x.get("rule"), str)):
                continue
            try:
                r = evaluate({**x, "ref_close": closes.get(x["symbol"])}, by_sym.get(x["symbol"]), d, as_of_close,
                             universe=universe, px=px)
            except Exception as e:  # noqa: BLE001
                logger.warning("card settlement row failed: %s", type(e).__name__)
                continue
            r.update(provenance=provenance(p, x), data_asof=as_of_close.isoformat(), contract=CONTRACT,
                     **_meta(data_dir, p, x, cache))
            rows.append(r)
    counts = defaultdict(int)
    for r in rows:
        counts[r["outcome"]] += 1
    return {"rows": rows, "as_of_close": as_of_close.isoformat(), "counts": dict(counts)}


# ── 表 ────────────────────────────────────────────────────

def _boot_ci(rows: list[tuple[str, float]]):
    by = defaultdict(list)
    for d, v in rows:
        by[d].append(v)
    blocks = list(by.values())
    if len(blocks) < MIN_BLOCKS:
        return None
    rng = random.Random(BOOT_SEED)
    means = []
    for _ in range(BOOT_DRAWS):
        s = [v for _ in blocks for v in blocks[rng.randrange(len(blocks))]]
        means.append(statistics.fmean(s))
    means.sort()
    return [round(means[int(0.025 * BOOT_DRAWS)], 4), round(means[int(0.975 * BOOT_DRAWS) - 1], 4)]


def _cell(rows: list[dict], kind: str) -> dict:
    hit = [r for r in rows if r["outcome"] == "HIT"]
    pas = [r for r in rows if r["outcome"] == "PASS"]
    exp = sum(r["outcome"] == "EXP" for r in rows)
    trades = [(r["signal_day"], r["r"], r.get("excess_r")) for r in hit]
    if kind == "signal":
        trades += [(r["signal_day"], r["signal_r"], r.get("signal_excess_r")) for r in pas
                   if r.get("signal_final") and r.get("signal_r") is not None]
    unsettled = sum(r["outcome"] == "UNSETTLED" for r in rows)
    if kind == "signal":
        unsettled += sum(1 for r in pas if not r.get("signal_final"))
    n = len(trades)
    settled = len(hit) + exp + (len(pas) if kind == "fill" else sum(1 for r in pas if r.get("signal_final")))
    out = {"n": n, "settled": settled, "exp": exp, "pass": len(pas), "unsettled": unsettled,
           "missing": sum(r["outcome"] == "MISSING" for r in rows),
           "same_bar": sum(bool(r.get("same_bar")) for r in hit),
           "trigger_rate": round(n / settled, 4) if settled else None,
           "win_rate": None, "mean_r": None, "mean_excess_r": None, "ci": None, "worst_r": None, "tone": "gray"}
    if n < N_GRAY:
        return out
    rs = [t[1] for t in trades]
    ex = [(t[0], t[2]) for t in trades if t[2] is not None]
    out.update(win_rate=round(sum(r > 0 for r in rs) / n, 4), mean_r=round(statistics.fmean(rs), 4), worst_r=round(min(rs), 4),
               mean_excess_r=round(statistics.fmean(v for _, v in ex), 4) if ex else None, tone="plain")
    out["ci"] = _boot_ci(ex) if ex else None
    if n >= N_COLOR and out["ci"]:
        lo, hi = out["ci"]
        out["tone"] = "pos" if lo > 0 else "neg" if hi < 0 else "plain"
    return out


def table(rows: list[dict], *, kind: str = "fill", scope: str = "live") -> dict:
    """kind: fill = 能成交 (按规则入场价; PASS 计作废) / signal = 信号准 (PASS 按跳空开盘价虚拟入场计入)。
    scope: live = 只统计盘后扫描当时写出的行 / all = 含补算。主行 = 规则 x 多/空。"""
    rows = [r for r in rows if scope == "all" or r.get("provenance") == "live"]
    groups = defaultdict(list)
    for r in rows:
        groups[(r["rule"], r["side"])].append(r)
    cells = [{"rule": k[0], "side": k[1], "family": (g[0].get("rule_family") if g else None), **_cell(g, kind)}
             for k, g in sorted(groups.items())]
    return {"kind": kind, "scope": scope, "m": len(cells), "cells": cells, "all": _cell(rows, kind)}


# ── 落盘 ──────────────────────────────────────────────────

def _records(data_dir: Path) -> list[dict]:
    p = cards_path(data_dir)
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if isinstance(x, dict) and x.get("key") and x.get("record_kind"):
                out.append(x)
    return out


_SIGNAL_KEYS = ("signal_state", "signal_final", "signal_r", "signal_r_orig", "signal_ret", "signal_exit_date",
                "signal_exit_reason", "signal_bench_ret", "signal_excess_r", "signal_entry", "signal_risk", "gap_date")


def _merge(rows: list[dict], recs: list[dict]) -> list[dict]:
    """已写的明细为准 (不随日K 事后修订而变); 其余用本次计算。"""
    by = defaultdict(dict)
    for r in recs:
        by[r["key"]][r["record_kind"]] = r
    out = []
    for r in rows:
        got = by.get(r["key"], {})
        if "settled" in got:
            r = {k: v for k, v in got["settled"].items() if k not in ("record_kind", "settle_run_id", "settled_at")}
        else:
            if "pass_void" in got:
                r = {**r, "outcome": "PASS", "final_fill": True}
            if "pass_signal" in got:
                r = {**r, **{k: got["pass_signal"].get(k) for k in _SIGNAL_KEYS}, "signal_final": True}
        out.append(r)
    return out


def run(data_dir: Path, repo, *, as_of_close: date) -> dict:
    """计算 → 追加新终结的明细 (幂等) → 写 table.json (派生物) 与 runs.jsonl。只写 settle_dir。"""
    from app.services.fs_utils import atomic_write_text
    data_dir = Path(data_dir)
    sd = settle_dir(data_dir)
    sd.mkdir(parents=True, exist_ok=True)
    res = compute(data_dir, repo, as_of_close=as_of_close)
    recs = _records(data_dir)
    have = {(r["key"], r["record_kind"]) for r in recs}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    run_id = f"{as_of_close.isoformat()}@{now}"
    new = []
    for r in res["rows"]:
        kinds = []
        if r["outcome"] in ("HIT", "EXP") and r["final"]:
            kinds.append("settled")
        if r["outcome"] == "PASS":
            kinds.append("pass_void")
            if r.get("signal_final"):
                kinds.append("pass_signal")
        for k in kinds:
            if (r["key"], k) in have:
                continue
            body = dict(r) if k != "pass_void" else {kk: r.get(kk) for kk in (
                "key", "card_id", "signal_day", "rule", "symbol", "side", "outcome", "state", "gap_date", "provenance",
                "ruleset_version", "mode", "params_hash", "strategy_fingerprint", "lineage_src", "rule_family", "contract")}
            if k == "pass_void":
                body.update(outcome="PASS", final_fill=True)
            new.append({**body, "record_kind": k, "settle_run_id": run_id, "settled_at": now, "data_asof": r["data_asof"]})
            have.add((r["key"], k))
    if new:
        with open(cards_path(data_dir), "a", encoding="utf-8") as f:
            for x in new:
                f.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
    merged = _merge(res["rows"], recs + new)
    tables = {k: {s: table(merged, kind=k, scope=s) for s in ("live", "all")} for k in ("fill", "signal")}
    counts = defaultdict(int)
    for r in merged:
        counts[r["outcome"]] += 1
    kinds = defaultdict(int)
    for x in new:
        kinds[x["record_kind"]] += 1
    summary = {"settle_run_id": run_id, "contract": CONTRACT, "data_asof": as_of_close.isoformat(), "at": now,
               "rows": len(merged), "appended": len(new), "appended_by_kind": dict(kinds), "counts": dict(counts)}
    atomic_write_text(sd / "table.json", json.dumps({"contract": CONTRACT, "data_asof": as_of_close.isoformat(),
                                                     "generated_at": now, "counts": dict(counts), "tables": tables,
                                                     "thresholds": {"gray_below": N_GRAY, "color_from": N_COLOR, "min_blocks": MIN_BLOCKS,
                                                                    "min_age": MIN_AGE, "bench_k": BENCH_K}},
                                                    ensure_ascii=False, default=str))
    with open(sd / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def run_if_enabled(data_dir: Path, repo, *, as_of_close: date) -> dict | None:
    """盘后管线入口: data/research/settlement/ 不存在 (回填尚未经批准执行) → 什么都不做、不建目录。"""
    if not settle_dir(data_dir).is_dir():
        return None
    return run(data_dir, repo, as_of_close=as_of_close)


def read_table(data_dir: Path) -> dict | None:
    try:
        d = json.loads((settle_dir(data_dir) / "table.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def main(argv: list[str] | None = None) -> int:
    """回填 / 试算: python -m app.services.card_settlement --data-dir /app/data [--as-of YYYY-MM-DD] [--dry-run OUT.json]"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--as-of", default=None, help="已收盘日 (美东); 缺省 = 上一个已收盘交易日")
    ap.add_argument("--dry-run", default=None, help="只算不写数据目录, 结果写到这个文件")
    a = ap.parse_args(argv)
    from app.storage.repository import DataStore, KlineRepository
    dd = Path(a.data_dir)
    as_of = date.fromisoformat(a.as_of) if a.as_of else us_clock.previous_trading_day(us_clock.today())
    repo = KlineRepository(DataStore(dd))
    if a.dry_run:
        res = compute(dd, repo, as_of_close=as_of)
        tabs = {k: {s: table(res["rows"], kind=k, scope=s) for s in ("live", "all")} for k in ("fill", "signal")}
        Path(a.dry_run).write_text(json.dumps({"counts": res["counts"], "tables": tabs, "rows": res["rows"]},
                                              ensure_ascii=False, default=str, indent=1), encoding="utf-8")
        print(json.dumps(res["counts"], ensure_ascii=False))
        return 0
    print(json.dumps(run(dd, repo, as_of_close=as_of), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
