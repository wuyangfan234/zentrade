# ruff: noqa: RUF002
"""模型状态 (0.57.0): 今日多空排名模型的登记、冠军 / 挑战者同窗对照、特征漂移。只展示, 不进交易环、不推送。

冠军 = 出页面名单的模型; 登记在 opportunities/ls_rank/model/registry.json。没有登记时取最近一份名单用的季度模型
  (上线前的名单照旧算数), 一份名单都没有时取名单日所在季度。
挑战者 = 名单日进入比冠军更新的季度时, 按原口径训练的新季度模型 (训练截止 = 该季度首日 − 8 自然日)。
  它每天出一份影子名单 (opportunities/ls_rank/shadow/<季度>/<名单日>.json, 与正式名单同格式、同随机对照、同结算),
  不上页面名单。
同窗对照 = 两边都已结算、都不是开盘后补出的名单日。满 GATE_N 份时裁决一次 (只看最早的 GATE_N 份):
  挑战者「看多 − 自选全体」不低于冠军 → 下一份名单起由它出; 否则冠军留任, 挑战者记未晋级, 影子名单停出。
  同一季度只裁决一次。
特征漂移 = 自选股 8 个特征的分布, 近 CUR_DAYS 个交易日 对 训练截止前 REF_DAYS 个自然日, 按参照窗十分位算 PSI;
  盘后算一次写 model/drift.json, 页面只读这份文件。
  注意线按特征各算: 把参照窗切成不重叠的 CUR_DAYS 日小窗, 每个小窗对整个参照窗算 PSI, 取其中最高值, 且不低于 PSI_WARN。
  动量 / 隔夜收益这类滚动均值特征, 任意一个月对全年的 PSI 本来就在 0.2~0.9 (真实数据实测), 用固定 0.25 会常年报「注意」;
  超过自己过去一年出现过的最高值才算。
status(): 页面段 (只读, 不建目录、不出网、不读模型文件)。asof 给了 → 只用名单日 ≤ asof 的名单、
  只认结算到期日 ≤ asof 的成绩 (回答「那天知道什么」); 之后的结算只放在 post 里, 与当天的数分开。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.services import ls_rank

logger = logging.getLogger(__name__)

CONTRACT = "ls-model-v1"
GATE_N = 25                        # 同窗对照满多少份名单裁决
GATE_KEY = "long_minus_all"        # 门槛指标: 看多减自选全体 (每笔 ATR)
PSI_WARN = 0.25
PSI_BINS = 10
REF_DAYS = 365                     # 参照窗: 训练截止前多少自然日
CUR_DAYS = 20                      # 当前窗: 最近多少个交易日
MIN_REF, MIN_CUR = 200, 50         # 样本不足不算
MIN_BASE_WINDOWS = 4               # 参照窗切不出这么多小窗 → 注意线退回 PSI_WARN
COMPARE_KEYS = ("long_minus_all", "long", "short", "pair")


# ── 登记 ──────────────────────────────────────────────────────────

def registry_path(data_dir: Path) -> Path:
    return ls_rank.root(data_dir) / "model" / "registry.json"


def drift_path(data_dir: Path) -> Path:
    return ls_rank.root(data_dir) / "model" / "drift.json"


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def load_registry(data_dir: Path) -> dict:
    d = _read(registry_path(data_dir)) or {}
    hist = [h for h in d.get("history") or [] if isinstance(h, dict) and h.get("quarter") and h.get("event")]
    return {"champion": d.get("champion") or None, "challenger": d.get("challenger") or None, "history": hist}


def _save_registry(data_dir: Path, reg: dict) -> None:
    ls_rank._write_json(registry_path(data_dir), {"contract": CONTRACT, **reg})


def quarter_day(q: str) -> date:
    """季度首日 ("2026Q4" → 2026-10-01)。"""
    return date(int(q[:4]), 3 * (int(q[5]) - 1) + 1, 1)


def _latest_list_quarter(data_dir: Path) -> str | None:
    for d in reversed(ls_rank.list_days(data_dir)):
        q = ls_rank.model_quarter(ls_rank.load_day(data_dir, d) or {})
        if q:
            return q
    return None


def champion_quarter(data_dir: Path, nd: date, *, persist: bool = False) -> str:
    reg = load_registry(data_dir)
    q = reg["champion"] or _latest_list_quarter(data_dir) or ls_rank.quarter_of(nd)
    if persist and reg["champion"] != q:
        reg["champion"] = q
        reg["history"].append({"quarter": q, "event": "champion", "day": nd.isoformat(), "reason": "initial"})
        _save_registry(data_dir, reg)
    return q


def champion_model(data_dir: Path, nd: date, *, watch: list[str] | None = None) -> dict:
    """出正式名单用的模型 (文件不在或口径变了 → 按该季度原截止日重训)。"""
    return ls_rank.train(data_dir, quarter_day(champion_quarter(data_dir, nd, persist=True)), watch=watch)


# ── 同窗对照与裁决 ─────────────────────────────────────────────────

def _settled_by(rec: dict | None, asof: date | None) -> bool:
    st = rec.get("settled") if isinstance(rec, dict) else None
    if not isinstance(st, dict) or not isinstance(st.get("r"), dict):
        return False
    return asof is None or str(st.get("end") or "9999") <= asof.isoformat()


def same_window(data_dir: Path, chal: str, asof: date | None = None) -> tuple[list[dict], list[dict]]:
    """(冠军名单, 挑战者影子名单) 逐日配对: 同一名单日、两边都已结算 (到期日 ≤ asof)、都不是开盘后补出的。"""
    a, b = [], []
    for d in ls_rank.list_days(data_dir, chal):
        if asof is not None and d > asof:
            continue
        s, c = ls_rank.load_day(data_dir, d, chal), ls_rank.load_day(data_dir, d)
        if not s or not c or s.get("late") or c.get("late") or ls_rank.model_quarter(c) == chal:
            continue
        if _settled_by(s, asof) and _settled_by(c, asof):
            a.append(c)
            b.append(s)
    return a, b


def _gate(champ: list[dict], chal: list[dict]) -> tuple[float | None, float | None]:
    sa, sb = ls_rank.series(champ[:GATE_N]), ls_rank.series(chal[:GATE_N])
    return (sa or {}).get(GATE_KEY), (sb or {}).get(GATE_KEY)


def decide(data_dir: Path, day: date) -> dict:
    """同窗满 GATE_N 份 → 裁决一次并写登记。返回 {state, n, ...}。"""
    reg = load_registry(data_dir)
    chal = reg["challenger"]
    if not chal:
        return {"state": "none"}
    a, b = same_window(data_dir, chal)
    n = min(len(a), GATE_N)
    if n < GATE_N:
        return {"state": "running", "n": n}
    va, vb = _gate(a, b)
    if va is None or vb is None:
        return {"state": "running", "n": n}
    passed = vb >= va
    was = champion_quarter(data_dir, day)
    reg["challenger"] = None
    if passed:
        reg["champion"] = chal
    reg["history"].append({"quarter": chal, "event": "promoted" if passed else "rejected", "day": day.isoformat(),
                           "n": n, "champion": was, "champ": va, "chal": vb, "key": GATE_KEY})
    _save_registry(data_dir, reg)
    logger.info("ls_model: challenger %s %s (n=%d %s %.3f vs %.3f)", chal, "promoted" if passed else "rejected", n, GATE_KEY, vb, va)
    return {"state": "promoted" if passed else "rejected", "n": n, "champ": va, "chal": vb}


def run_shadow(data_dir: Path, as_of: date, *, next_day: date | None = None, watch: list[str] | None = None,
               daily=None, model: dict | None = None) -> dict:
    """盘后: 需要时立挑战者 → 结算影子名单 → 裁决 → 仍在对照就出下一交易日影子名单。"""
    from app.market_clock import us_clock
    data_dir = Path(data_dir)
    if next_day is None and not us_clock.calendar_available(as_of + timedelta(days=1), as_of + timedelta(days=14)):
        return {"status": "skipped", "reason": "calendar_unavailable"}
    nd = next_day or us_clock.next_trading_day(as_of)
    reg = load_registry(data_dir)
    champ = champion_quarter(data_dir, nd)
    q_new = ls_rank.quarter_of(nd)
    chal = reg["challenger"]
    decided = {h["quarter"] for h in reg["history"] if h["event"] in ("promoted", "rejected")}
    if not chal and q_new > champ and q_new not in decided:
        chal = q_new
        reg["challenger"] = chal
        reg["history"].append({"quarter": chal, "event": "challenger", "day": nd.isoformat(), "champion": champ})
        if not reg["champion"]:
            reg["champion"] = champ
        _save_registry(data_dir, reg)
    if not chal:
        return {"status": "none"}
    out: dict[str, Any] = {"challenger": chal, "settle": ls_rank.settle(data_dir, as_of, sub=chal)}
    out["decide"] = decide(data_dir, as_of)
    if out["decide"]["state"] != "running":
        return out
    m = model or ls_rank.train(data_dir, quarter_day(chal), watch=watch)
    out["generate"] = ls_rank.generate(data_dir, as_of, next_day=next_day, watch=watch, daily=daily, model=m, sub=chal)
    return out


# ── 特征漂移 ──────────────────────────────────────────────────────

def psi(ref, cur, bins: int = PSI_BINS) -> float | None:
    """按参照样本的分位切箱; 每箱加 0.5 平滑 (空箱不出无穷大)。样本不足 = None。"""
    import numpy as np
    r = np.asarray(ref, dtype=float)
    c = np.asarray(cur, dtype=float)
    r, c = r[np.isfinite(r)], c[np.isfinite(c)]
    if len(r) < MIN_REF or len(c) < MIN_CUR:
        return None
    edges = np.unique(np.quantile(r, [i / bins for i in range(1, bins)]))
    k = len(edges) + 1
    pr = (np.bincount(np.searchsorted(edges, r, side="right"), minlength=k) + 0.5) / (len(r) + 0.5 * k)
    pc = (np.bincount(np.searchsorted(edges, c, side="right"), minlength=k) + 0.5) / (len(c) + 0.5 * k)
    return float(np.sum((pc - pr) * np.log(pc / pr)))


def compute_drift(data_dir: Path, as_of: date, *, watch: list[str] | None = None, daily=None) -> dict | None:
    """算一次特征漂移并写 model/drift.json。没有名单 / 样本不足 → None, 不写。"""
    import polars as pl
    data_dir = Path(data_dir)
    days = ls_rank.list_days(data_dir)
    rec = ls_rank.load_day(data_dir, days[-1]) if days else None
    q = ls_rank.model_quarter(rec or {})
    try:
        trained_to = date.fromisoformat(str(((rec or {}).get("model") or {}).get("trained_to")))
    except ValueError:
        return None
    if not q or as_of <= trained_to:
        return None
    syms = sorted(set(watch if watch is not None else ls_rank._watchlist()))
    if not syms:
        return None
    ref_from = trained_to - timedelta(days=REF_DAYS)
    if daily is None:
        daily = ls_rank.load_daily(data_dir, ref_from - timedelta(days=ls_rank.WARMUP_DAYS), as_of, symbols=syms)
    panel = ls_rank.build_panel(daily.filter(pl.col("date") <= as_of))
    ref = panel.filter((pl.col("date") > ref_from) & (pl.col("date") <= trained_to))
    cur_days = sorted(d for d in panel["date"].unique().to_list() if d > trained_to)[-CUR_DAYS:]
    if not cur_days or ref.height == 0:
        return None
    cur = panel.filter(pl.col("date").is_in(cur_days))
    ref_days = sorted(ref["date"].unique().to_list())
    windows = [ref_days[i:i + CUR_DAYS] for i in range(0, len(ref_days) - CUR_DAYS + 1, CUR_DAYS)]
    parts = [ref.filter(pl.col("date").is_in(w)) for w in windows]
    rows = []
    for k in ls_rank.FEATURES:
        rv = ref[k].to_numpy()
        v = psi(rv, cur[k].to_numpy())
        if v is None:
            return None
        base = [b for b in (psi(rv, p[k].to_numpy()) for p in parts) if b is not None]
        top = max(base) if len(base) >= MIN_BASE_WINDOWS else None
        line = max(PSI_WARN, top) if top is not None else PSI_WARN
        rows.append({"key": k, "psi": round(v, 3), "base": round(top, 3) if top is not None else None,
                     "line": round(line, 3), "warn": round(v, 3) >= round(line, 3)})
    out = {"contract": CONTRACT, "as_of": as_of.isoformat(), "quarter": q, "warn": PSI_WARN, "windows": len(windows),
           "ref": {"from": ref["date"].min().isoformat(), "to": ref["date"].max().isoformat(), "n": ref.height},
           "cur": {"from": cur_days[0].isoformat(), "to": cur_days[-1].isoformat(), "n": cur.height},
           "rows": rows}
    ls_rank._write_json(drift_path(data_dir), out)
    return out


def run_post_close(data_dir: Path, day: date) -> dict:
    """盘后入口 (ls_rank.run_post_close 末尾调): 影子名单 / 裁决与漂移各自容错。"""
    out: dict[str, Any] = {}
    try:
        out["shadow"] = run_shadow(data_dir, day)
    except Exception as e:
        logger.warning("ls_model shadow failed (soft): %s: %s", type(e).__name__, e)
        out["shadow"] = {"error": type(e).__name__}
    try:
        d = compute_drift(data_dir, day)
        out["drift"] = {"rows": len(d["rows"]), "warn": sum(1 for r in d["rows"] if r["warn"])} if d else None
    except Exception as e:
        logger.warning("ls_model drift failed (soft): %s: %s", type(e).__name__, e)
        out["drift"] = {"error": type(e).__name__}
    return out


# ── 页面段 (只读) ──────────────────────────────────────────────────

def _next_quarter(d: date) -> date:
    s = ls_rank.quarter_start(d)
    return date(s.year + (s.month == 10), (s.month + 2) % 12 + 1, 1)


def _series(recs: list[dict]) -> dict | None:
    s = ls_rank.series(recs) if recs else None
    return {k: s.get(k) for k in (*COMPARE_KEYS, "n_lists", "win_long", "win_short", "all")} if s else None


def _challenger_view(data_dir: Path, reg: dict, asof: date | None) -> dict | None:
    """asof 那天 (或现在) 最近一个挑战者: 对照中 / 已晋级 / 未晋级。之后才发生的裁决不算。"""
    lim = asof.isoformat() if asof else "9999"
    starts = [h for h in reg["history"] if h["event"] == "challenger" and str(h.get("day") or "") <= lim]
    if not starts:
        return None
    h0 = starts[-1]
    q = h0["quarter"]
    dec = next((h for h in reg["history"] if h["quarter"] == q and h["event"] in ("promoted", "rejected")
                and str(h.get("day") or "") <= lim), None)
    a, b = same_window(data_dir, q, asof)
    if dec:
        a, b = a[:GATE_N], b[:GATE_N]
    sdays = [d for d in ls_rank.list_days(data_dir, q) if asof is None or d <= asof]
    first = ls_rank.load_day(data_dir, sdays[0], q) if sdays else None
    return {"quarter": q, "name": f"GBM {q}", "trained_to": ((first or {}).get("model") or {}).get("trained_to"),
            "since": h0.get("day"), "state": dec["event"] if dec else "running", "decided": dec.get("day") if dec else None,
            "n": min(len(a), GATE_N), "gate_n": GATE_N, "key": GATE_KEY, "lists": len(sdays),
            "champ": _series(a), "chal": _series(b)}


def status(data_dir: Path, *, today: date, asof: date | None = None) -> dict | None:
    """模型状态段。没有名单 = None。"""
    data_dir = Path(data_dir)
    days = [d for d in ls_rank.list_days(data_dir) if asof is None or d <= asof]
    pick = (days[-1] if days else None) if asof else ls_rank.pick_day(days, today)
    rec = ls_rank.load_day(data_dir, pick) if pick else None
    if rec is None or pick is None:
        return None
    q = ls_rank.model_quarter(rec)
    every = [x for x in (ls_rank.load_day(data_dir, d) for d in days) if x]
    by_q: dict[str, list[dict]] = {}
    for x in every:
        by_q.setdefault(ls_rank.model_quarter(x) or "?", []).append(x)

    def fwd(xs: list[dict]) -> list[dict]:
        return [x for x in xs if _settled_by(x, asof) and not x.get("late")]

    mine = fwd(by_q.get(q or "?", []))
    s = _series(mine)
    lma = (s or {}).get(GATE_KEY)
    forward = {"days": len(mine), "check_days": ls_rank.CHECK_DAYS, "first": mine[0]["day"] if mine else None,
               "last_list": mine[-1]["day"] if mine else None, "total": s,
               "failed": None if len(mine) < ls_rank.CHECK_DAYS or lma is None else lma < 0}
    rows_all = [r for r in rec.get("rows") or [] if isinstance(r, dict)]
    missing, stale = list(rec.get("missing") or []), list(rec.get("stale") or [])
    n_ranked = rec.get("n") if isinstance(rec.get("n"), int) else len(rows_all)
    nq = _next_quarter(pick)
    reg = load_registry(data_dir)
    chal = _challenger_view(data_dir, reg, asof)
    registry = []
    for qq in sorted(by_q):
        xs = by_q[qq]
        m = xs[-1].get("model") or {}
        f = fwd(xs)
        registry.append({"quarter": qq, "name": f"GBM {qq}", "trained_to": m.get("trained_to"), "rows": m.get("rows"),
                         "role": "champion" if qq == q else "archived", "first": xs[0]["day"], "last": xs[-1]["day"],
                         "days": len(f), GATE_KEY: (_series(f) or {}).get(GATE_KEY)})
    if chal and chal["quarter"] not in by_q:
        registry.append({"quarter": chal["quarter"], "name": chal["name"], "trained_to": chal["trained_to"], "rows": None,
                         "role": "challenger" if chal["state"] == "running" else chal["state"], "first": chal["since"],
                         "last": None, "days": chal["n"], GATE_KEY: (chal["chal"] or {}).get(GATE_KEY)})
    drift = None if asof else _read(drift_path(data_dir))
    post = None
    if asof and pick == asof and isinstance((rec.get("settled") or {}).get("r"), dict):
        r = rec["settled"]["r"]
        ll = [r[x]["long"] for x in rec.get("long") or [] if x in r]
        ss = [r[x]["short"] for x in rec.get("short") or [] if x in r]
        if ll and ss:
            post = {"day": rec["day"], "end": rec["settled"].get("end"), "long": round(sum(ll) / len(ll), 3),
                    "short": round(sum(ss) / len(ss), 3)}
    return {"contract": CONTRACT, "asof": asof.isoformat() if asof else None, "list_day": rec["day"], "as_of": rec.get("as_of"),
            "champion": {"quarter": q, "name": f"GBM {q}", "trained_to": (rec.get("model") or {}).get("trained_to"),
                         "rows": (rec.get("model") or {}).get("rows"), "since": by_q[q or "?"][0]["day"]},
            "forward": forward, "challenger": chal, "gate": {"n": GATE_N, "key": GATE_KEY},
            "coverage": {"ranked": n_ranked, "young": sum(1 for r in rows_all if r.get("young")), "missing": len(missing),
                         "stale": len(stale), "total": n_ranked + len(missing) + len(stale)},
            "next_retrain": {"quarter": ls_rank.quarter_of(nq), "train_end": ls_rank.train_end(nq).isoformat()},
            "drift": drift, "registry": registry, "backtest": {"period": ls_rank.BACKTEST["period"],
                                                               GATE_KEY: ls_rank.BACKTEST.get(GATE_KEY)},
            "post": post}
