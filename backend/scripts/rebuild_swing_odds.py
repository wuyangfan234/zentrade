"""机会板状态赔率表 + 基础率重算 (2026-11-10; 只读数据目录副本, 不选参)。

产物: config/swing_state_odds.json (卡面「回区 / 期望」按状态查的表) 与 config/swing_base_rates.json (多空各一行基础率)。
两份表都只对「生成时那一套 3+3」成立: swing_verdict.odds_for 与 swing_settlement.card_line 发现表里的规则与现行规则
不一致就不给数字。所以齿轮改了 3+3 之后要重跑这个脚本 (先跑 rebuild_swing_history.py --dump, 再跑本脚本)。

口径 (与 10-19 研究稿 s1 / s3 / s11 相同, 全部改用生产函数):
  - 卡 = 每个信号日的机会板: 生产 swing_desk.compose_boards (3+3 命中 -> 4h 门 -> 空头大盘门 -> 多空冲突 -> 每边前 5,
    同分按现行固定散列), 输入取 rebuild_swing_history.py --dump 的规则命中 / 4h 门 / 日K。
  - 结局 = dump 里同一张卡的 swing_settlement.replay_card 结果 (现行结算口径, 见 dump 的 settle_contract)。
  - 状态 = 方向 x 入场窗第几天 x 时段 x 现价离挂单区几个 ATR:
      时点: 盘前 08:00 / 09:00 (已完成的最后一根盘前 30 分钟K 收盘), 开盘 (09:30 开盘价), 10:00 起每根 30 分钟K 开始时
      (价 = 上一根收盘)。当天最后一根 (收盘前 30 分钟) 纸面已不能成交, 不设时点。
      时段: 盘前 pre / 开盘 open (09:30-10:30) / 上午 am (10:30-12:30) / 下午 pm (12:30 起), 与 swing_verdict.phase_of 相同。
      同一张卡在同一 (第几天, 时段, 距离档) 只记一次; 该时点之前已成交的不记。
      期望 = 这张卡从该时点起继续挂单最终拿到的 R (没成交记 0)。
  - 作废规则: 做多卡第 1 天 09:00 盘前价 < 止损 -> 整卡作废 (第 1 天盘前的行保留, 之后的行剔除; 基础率记 void)。
  - 样本 < 20 的格子退到 (方向, 第几天, 距离), 再退到 (方向, 距离); 区间 = 按周聚类自助法 95%。
  - birth 格 = 出卡后还没有新价格: 该方向全部卡第 1 天的结局。

用法: python rebuild_swing_odds.py <mirror_data_dir> <cells.pkl> <out_config_dir> [--long a,b,c --short d,e,f]
  规则默认读 <mirror>/rules/swing.json; 排序因子 (rank_*) 不是信号策略, 这里不支持。
"""
import json
import math
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

import numpy as np

MIRROR = Path(sys.argv[1]).resolve()
DUMP = Path(sys.argv[2])
OUT = Path(sys.argv[3])
os.environ["DATA_DIR"] = str(MIRROR)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

settings.data_dir = MIRROR

import polars as pl  # noqa: E402

from app.services import kline_4h  # noqa: E402
from app.services import swing_desk as sd  # noqa: E402
from app.services import swing_settlement as ss  # noqa: E402
from app.services import swing_verdict as sv  # noqa: E402
from app.storage.repository import DataStore, KlineRepository  # noqa: E402


def arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


t_all = time.time()
with DUMP.open("rb") as f:
    P = pickle.load(f)
if P.get("settle_contract") != ss.CONTRACT:
    raise SystemExit(f"dump settle_contract {P.get('settle_contract')} != code {ss.CONTRACT}: rerun rebuild_swing_history.py --dump")
DAYS = [date.fromisoformat(x) for x in P["days"]]
SYMS, B0, A1 = P["syms"], P["B0"], P["A1"]
T = len(DAYS)
WL = set(P["watchlist"])
COLS = [j for j, s in enumerate(SYMS) if s in WL and s not in ("SPY", "QQQ")]
C, ATR, G_L, G_S, E_L, E_S, SPY_B50, LH, SH = (P[k] for k in ("C", "ATR", "G_L", "G_S", "E_L", "E_S", "SPY_B50", "LH", "SH"))
DETAIL = P["detail"]
if arg("--long") or arg("--short"):
    LONG = tuple(x for x in arg("--long").split(",") if x)
    SHORT = tuple(x for x in arg("--short").split(",") if x)
else:
    LONG, SHORT = sd.strategies_from_ruleset(MIRROR)
bad = [r for r in LONG if r.startswith("rank_") or r not in LH] + [r for r in SHORT if r not in SH]
if bad:
    raise SystemExit(f"rules not replayable from the dump (ranking factors / unknown): {bad}")
print(f"rules long={LONG} short={SHORT} window={DAYS[B0]}..{DAYS[A1]} universe={P['universe']}", flush=True)

# ---- 每个信号日的板 (生产 compose_boards) ----
cards = []          # (t, j, side, card)
for t in range(B0, A1 + 1):
    sig, closes, atr, h4 = [], {}, {}, {}
    for j in COLS:
        s = SYMS[j]
        if np.isfinite(C[t, j]) and np.isfinite(ATR[t, j]):
            closes[s], atr[s] = float(C[t, j]), float(ATR[t, j])
        if E_L[t, j]:
            sig.append({"rule": "phantom_4h", "symbol": s, "side": "long"})
        elif G_L[t, j]:
            h4[s] = "long"
        if E_S[t, j]:
            sig.append({"rule": "phantom_4h_short", "symbol": s, "side": "short"})
        elif G_S[t, j]:
            h4[s] = "short"
        for r in LONG:
            if LH[r][t, j]:
                sig.append({"rule": r, "symbol": s, "side": "long"})
        for r in SHORT:
            if SH[r][t, j]:
                sig.append({"rule": r, "symbol": s, "side": "short"})
    board = sd.compose_boards(signals=sig, closes=closes, as_of=DAYS[t], long_rules=LONG, short_rules=SHORT, atr=atr,
                              h4_state=h4, short_ok=bool(SPY_B50[t]))
    for c in board["long"] + board["short"]:
        cards.append((t, SYMS.index(c["symbol"]), c["side"], c))
print(f"board cards {len(cards)} (long {sum(1 for x in cards if x[2] == 'long')}, short {sum(1 for x in cards if x[2] == 'short')})",
      flush=True)

# ---- 30 分钟K: 常规时段逐日 + 盘前最后一根 ----
repo = KlineRepository(DataStore(MIRROR))
M30: dict[int, dict] = {}
PRE: dict[int, dict] = {}


def load_symbol(j: int) -> None:
    s = SYMS[j]
    M30[j] = ss._m30_days(MIRROR, repo, s, DAYS[B0], DAYS[-1])
    raw = kline_4h.load_m30(MIRROR, [s], DAYS[B0], DAYS[-1])
    out: dict = {}
    if not raw.is_empty():
        raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
        raw = raw.with_columns(pl.col("ts").dt.date().alias("date"),
                               (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("mm"))
        raw = raw.filter((pl.col("mm") >= 240) & (pl.col("mm") < 570)).sort("ts")
        fac = ss._basis_factors(MIRROR, repo, s, DAYS[B0], DAYS[-1], None)
        for d, mm, c in raw.select(["date", "mm", "close"]).iter_rows():
            out.setdefault(d, []).append((mm, c * fac(d)))
    PRE[j] = out


def pm_at(j: int, d: date, cut: int) -> float | None:
    """时点 cut (美东分钟) 前已完成的最后一根盘前 30 分钟K 的收盘 (起点 + 30 <= cut)。"""
    v = [c for mm, c in PRE[j].get(d, []) if mm + 30 <= cut]
    return float(v[-1]) if v and math.isfinite(v[-1]) and v[-1] > 0 else None


def phase(slot: str) -> str:
    if slot.startswith("pm"):
        return "pre"
    if slot == "open":
        return "open"
    m = 570 + 30 * int(slot[1:])                           # b<k> = 第 k 根 30 分钟K 开始的时刻
    return sv.phase_of(dtime(m // 60, m % 60))


rows, feats, void, missing = [], [], set(), 0
for t, j, side, c in cards:
    if j not in M30:
        load_symbol(j)
    key = (side[0], DAYS[t].isoformat(), SYMS[j])
    det = DETAIL.get(key)
    if det is None:
        missing += 1
        continue
    st, fill_day, _exit_day, why, r, zone, stop, _target, _nseq, fill_bar = det
    if tuple(zone) != tuple(c["entry_zone"]) or stop != c["stop"]:
        raise SystemExit(f"geometry mismatch {key}: dump {zone}/{stop} vs board {c['entry_zone']}/{c['stop']}")
    filled = st == "closed"
    ff = (fill_day + 1, fill_bar) if filled else None
    R = float(r) if filled else 0.0
    a = float(c["atr"])
    year = DAYS[t].isoformat()[:4]
    seen_day1 = False
    for w in (1, 2):
        tt = t + w
        if tt >= T or DAYS[tt] not in M30[j]:
            break
        if ff is not None and ff[0] < w:
            break
        bars = M30[j][DAYS[tt]]
        p8, p9 = pm_at(j, DAYS[tt], 480), pm_at(j, DAYS[tt], 540)
        if w == 1:
            if side == "long" and p9 is not None and p9 < stop:
                void.add((t, j, side))
            feats.append({"t": t, "j": j, "side": side, "tt": tt, "year": year, "hang": R if ff is not None else 0.0,
                          "filled": ff is not None})
            seen_day1 = True
        slots = [("pm0800", p8, 0), ("pm0900", p9, 0), ("open", float(bars[0][0]), 0)]
        for k in range(1, len(bars) - 1):                  # 10:00 起; 最后一根不可入场, 不设时点
            slots.append((f"b{k}", float(bars[k - 1][3]), k))
        for name, p, k0 in slots:
            if p is None or not math.isfinite(p):
                continue
            if ff is not None and ff[0] == w and ff[1] < k0:
                continue                                   # 该时点前已成交
            d = sv.dist_atr(side, p, c["entry_zone"], a)
            if d is None:
                continue
            rows.append({"t": t, "j": j, "side": side, "w": w, "slot": name, "d": d, "hang": R if ff is not None else 0.0,
                         "filled": ff is not None, "year": year, "tt": tt})
    if not seen_day1:
        missing += 1
print(f"state rows {len(rows)} day-1 cards {len(feats)} void {len(void)} cards without bars {missing}", flush=True)


def boot(xs, n=2000, seed=7):
    wk = defaultdict(list)
    for r in xs:
        wk[r["tt"] // 5].append(r["hang"])
    ks = list(wk)
    if len(ks) < 8:
        return None
    sums = np.array([sum(wk[k]) for k in ks])
    cnts = np.array([len(wk[k]) for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n, len(ks)))
    m = sums[pick].sum(1) / cnts[pick].sum(1)
    return [round(float(np.percentile(m, 2.5)), 3), round(float(np.percentile(m, 97.5)), 3)]


def cell(xs, level):
    def seg(yrs):
        v = [r["hang"] for r in xs if r["year"] in yrs]
        return round(float(np.mean(v)), 3) if v else None
    return {"n": len(xs), "fill": round(float(np.mean([r["filled"] for r in xs])), 3),
            "ev": round(float(np.mean([r["hang"] for r in xs])), 3), "ci": boot(xs),
            "ev_train": seg(("2023", "2024")), "ev_hold": seg(tuple(str(y) for y in range(2025, 2100))), "level": level}


seen, g1, g2, g3 = set(), defaultdict(list), defaultdict(list), defaultdict(list)
dropped = 0
for r in rows:
    if (r["t"], r["j"], r["side"]) in void and not (r["w"] == 1 and r["slot"].startswith("pm")):
        dropped += 1
        continue
    ph, b = phase(r["slot"]), sv.dist_bin(r["d"])
    k = (r["t"], r["j"], r["side"], r["w"], ph, b)
    if k in seen:
        continue
    seen.add(k)
    g1[(r["side"], r["w"], ph, b)].append(r)
    g2[(r["side"], r["w"], b)].append(r)
    g3[(r["side"], b)].append(r)
BIN_NAMES = [x[1] for x in sv.BINS] + ["in_zone", "below_zone", "beyond_stop"]
out = {}
for s in ("long", "short"):
    for w in (1, 2):
        for ph in ("pre", "open", "am", "pm"):
            for b in BIN_NAMES:
                xs, lvl = g1.get((s, w, ph, b)) or [], "day+phase+dist"
                if len(xs) < 20:
                    xs, lvl = g2.get((s, w, b)) or [], "day+dist"
                if len(xs) < 20:
                    xs, lvl = g3.get((s, b)) or [], "dist"
                if len(xs) < 20:
                    continue
                out[f"{s}|{w}|{ph}|{b}"] = cell(xs, lvl)
    xs = [dict(r, hang=0.0 if (r["t"], r["j"], r["side"]) in void else r["hang"],
               filled=False if (r["t"], r["j"], r["side"]) in void else r["filled"]) for r in feats if r["side"] == s]
    if xs:
        out[f"{s}|1|birth"] = cell(xs, "day")
print(f"cells {len(out)} dropped post-void rows {dropped}", flush=True)
for k in ("long|1|birth", "short|1|birth", "long|1|pre|0-0.25", "long|1|pre|beyond_stop", "long|1|open|in_zone"):
    print(" ", k, out.get(k), flush=True)

now = datetime.now().astimezone().isoformat(timespec="seconds")
head = {"contract": sd.CONTRACT, "settle_contract": ss.CONTRACT, "window": [DAYS[B0].isoformat(), DAYS[A1].isoformat()],
        "rules": {"long": list(LONG), "short": list(SHORT)}, "universe": P["universe"], "data_end": P["data_end"],
        "tiebreak": sd.TIEBREAK, "entry_window": "09:45 to 30 min before close (same as paper)"}
VOID_RULE = "long day-1: last premarket 30m close (04:00-09:00 ET) < stop -> card void"
odds = {**head,
        "unit": "hang EV = per-share R of the card if it fills from this state on (0 if never fills), plan risk = zone mid to stop, "
                "incl. 5bps slippage, no fees, no account limits",
        "bins": {"gt1": ">1A", "0.5-1": "0.5-1A", "0.25-0.5": "0.25-0.5A", "0-0.25": "0-0.25A",
                 "in_zone": "in zone", "below_zone": "zone..stop", "beyond_stop": "beyond stop"},
        "phases": {"pre": "<09:30 ET", "open": "09:30-10:30", "am": "10:30-12:30", "pm": "12:30-15:30"},
        "void_rule": VOID_RULE, "source": "backend/scripts/rebuild_swing_odds.py (production compose_boards + replay_card)",
        "generated": now, "cells": out}
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "swing_state_odds.json").write_text(json.dumps(odds, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

base = {**head, "r_unit": "per-share R vs plan risk (zone mid to stop), incl. 5bps slippage, no fees, no account limits",
        "alpha": ss.ALPHA, "source": "backend/scripts/rebuild_swing_odds.py (production compose_boards + replay_card)",
        "generated": now}
nvoid = {}
for side in ("long", "short"):
    rs, nv = [], 0
    for t, j, sd_, _c in cards:
        if sd_ != side:
            continue
        if (t, j, side) in void:
            rs.append({"status": "void"})
            nv += 1
            continue
        det = DETAIL.get((side[0], DAYS[t].isoformat(), SYMS[j]))
        if det is None:
            continue
        rs.append({"status": "closed", "why": det[3], "r": float(det[4])} if det[0] == "closed" else {"status": "no_fill"})
    base[side] = ss.summarize_rows(rs)
    nvoid[side] = nv
    x = base[side]
    print(side, "cards", x["cards"], "void", nv, "fill", x["fill"]["p"], "win", x["win"]["p"], "mean_r", x["mean_r"]["v"],
          x["mean_r"]["ci"], flush=True)
base["void_rule"] = VOID_RULE
base["void_cards"] = nvoid
(OUT / "swing_base_rates.json").write_text(json.dumps(base, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"written {OUT}/swing_state_odds.json + swing_base_rates.json in {time.time() - t_all:.0f}s", flush=True)
