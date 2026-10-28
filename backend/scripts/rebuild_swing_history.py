"""tight-v1 规则回放重算 (2026-10-21, 无限 10-20 拍板「内置策略回放重新算」; 只读生产数据副本, 不选参)。

与 10-18 的 build_swing_history.py 同一口径, 换成当前自选和最新数据, 并全部改用生产函数:
  - 信号 = strategy_evaluation_runner._signal_matrix (生产回测/证据同一条矩阵路径), 只在自选 + SPY/QQQ 上算
    (与 10-18 build_panel.py 相同); 做多 = 入场信号, 做空 = TV 移植策略的卖出信号 (watchlist_v4.SELL_IS_SHORT)。
  - 4h 门 = kline_4h.bars_4h -> phantom_4h.scan_series, 可见窗 [前一交易日 13:30, 当日 09:30]:
    窗内「多头/空头条件齐备」或最后一根方向同向 (生产 tight-v1 门, 与 swing_candidates._gate_matrix 同式, 加空头)。
  - 卡几何 = swing_desk.geometry; ATR = 截至信号日 14 根 TR 均值 (decision_desk.atr 同式)。
  - 结局 = swing_settlement.replay_card (30 分钟K, 2 天入场窗, 5 天持有, 1R 保本 / 2R 锁 1R, 滑点 5bps)。
  - 做空另需信号日 SPY 收盘 < 50 日均线。
统计 (与 10-18 相同): 每条规则在「过门且该规则命中」的格子上
  n = 成交笔数, fill = 回区率, mean_r = 每笔 R, excess = 每笔 R - 同日同方向全部过门成交的平均 R,
  p = 周聚类自助法 P(excess <= 0)。成交 < 5 笔只给计数 (n / signals / raw, thin=True)。

与 10-18 结果不完全相同的来源 (用 old 名单 + old 窗口对比过, 同一张卡逐张对拍):
  - 4h 门: 10-18 用的是 zentrade-p0-1015/scans.parquet 里那批 4h K 快照; 现在用生产的 kline_4h.bars_4h。
    两者同一根 K 的开收盘差 median 1e-4 ~ 1e-2 (ORCL/HPE 1.2%~1.8%, 复权系数不同), 换掉少数格子的门。
  - 30 分钟K: 10-18 的研究面板是 10-16 拍的, 现在是生产 kline_4h_src 复权到当日日K口径 (同日内一致,
    跨日差 median 3e-4), 所以刀口上的成交会翻面。
  - 结算内核本身没有差异: 抽样 400 个同几何的格子, 用研究内核 core.sim 与 replay_card 各跑一遍,
    分别与两边的原始结果 400/400 完全一致。

用法: python rebuild_swing_history.py <mirror_data_dir> <out.json> [--universe old|current] [--dump]
       [--only rule1,rule2 --merge <existing.json>]
  --only + --merge (0.37.0, 新增一条内置策略时用): 只算列出的规则, 窗口右端沿用 existing.json 的 window[1],
  其余规则与文件头原样保留, 新条目合并进去 (另记 added[rule] = 生成时刻与数据截至)。名单仍是副本里的自选,
  所以要与已有条目同口径, 副本里的 watchlist.parquet 须是 existing.json 生成时那份。
  截面策略 (META.cross_sectional) 的信号在全市场矩阵上算, 再取名单列 (排名分母是全池)。
  current = 副本里的自选名单, 窗口 2023-01-03 ~ 数据倒数第 7 个交易日 (留够 2 天入场 + 5 天持有)。
  old = 10-18 的 58 只 + 窗口 2023-01-03 ~ 2026-09-18, 只用来核对这套重算与 10-18 结果是否同口径。
  --dump = 另存 <out>.cells.pkl, 供逐格核对。
"""
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

MIRROR = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2])
UNIVERSE = sys.argv[sys.argv.index("--universe") + 1] if "--universe" in sys.argv else "current"
BACKEND = Path(__file__).resolve().parents[1]      # 本脚本所在的 backend (工作树里跑就用工作树的代码)
os.environ["DATA_DIR"] = str(MIRROR)
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/zt-numba")
sys.path.insert(0, str(BACKEND))
from app.config import settings  # noqa: E402

settings.data_dir = MIRROR
settings.backtest_matrix_disk_cache_enabled = False
logging.disable(logging.WARNING)

import pandas as pd  # noqa: E402
import polars as pl  # noqa: E402

from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.strategy import StrategyBacktestService  # noqa: E402
from app.services import kline_4h, phantom_4h  # noqa: E402
from app.services import strategy_evaluation_runner as runner  # noqa: E402
from app.services import swing_desk as sd  # noqa: E402
from app.services import swing_settlement as ss  # noqa: E402
from app.services import watchlist_v4 as wv  # noqa: E402
from app.storage.repository import DataStore, KlineRepository  # noqa: E402

OLD_WL = ["AAOI", "AAPL", "ABBV", "ADI", "AMAT", "AMD", "AMZN", "ANET", "ARM", "ASML", "ASTS", "AVGO", "CEG", "CIEN", "CLS",
          "COHR", "COIN", "CRCL", "CRDO", "DELL", "ETN", "GEV", "GLW", "GOOG", "HOOD", "HPE", "INTC", "IREN", "JNJ", "LITE",
          "LLY", "LRCX", "META", "MRK", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NOW", "NVDA", "ORCL", "PLTR", "RDW", "RKLB",
          "SKHY", "SMCI", "SNDK", "SNOW", "SPCX", "STX", "TMO", "TSLA", "TSM", "VRT", "VRTX", "VST", "WDC"]
START = date(2021, 6, 1)
SIG0 = date(2023, 1, 3)

t_all = time.time()
repo = KlineRepository(DataStore(MIRROR))
if UNIVERSE == "old":
    WL = OLD_WL
else:
    WL = sorted(pl.read_parquet(MIRROR / "user_data" / "watchlist.parquet")["symbol"].drop_nulls().unique().to_list())
SYMS = sorted(set(WL) | {"SPY", "QQQ"})

# ---- 日K: 交易日轴 + 复权 OHLC ----
spy = repo.get_daily("SPY", START, date.today(), columns=["date", "close"]).sort("date")
DAYS = spy["date"].to_list()
DATA_END = DAYS[-1]
if UNIVERSE == "old":
    A1_DAY = date(2026, 9, 18)
    DAYS = [d for d in DAYS if d <= date(2026, 10, 1)]
else:
    # 信号日之后还差 2 天入场窗 + 5 天持有, 最远走到 信号日 + 6; 留到数据倒数第 7 个交易日为止
    A1_DAY = DAYS[-7]
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
MERGE = Path(sys.argv[sys.argv.index("--merge") + 1]) if "--merge" in sys.argv else None
if (ONLY is None) != (MERGE is None):
    raise SystemExit("--only and --merge go together")
EXISTING = json.loads(MERGE.read_text(encoding="utf-8")) if MERGE is not None else None
if EXISTING is not None:
    if EXISTING["window"][0] != SIG0.isoformat():
        raise SystemExit(f"existing window starts {EXISTING['window'][0]}, this script starts {SIG0}")
    A1_DAY = date.fromisoformat(EXISTING["window"][1])
    if A1_DAY not in DAYS or DAYS.index(A1_DAY) > len(DAYS) - 7:
        raise SystemExit(f"mirror data must cover {A1_DAY} + 7 trading days")
T, N = len(DAYS), len(SYMS)
DI = {d: i for i, d in enumerate(DAYS)}
SI = {s: i for i, s in enumerate(SYMS)}
B0, A1 = DAYS.index(SIG0), DAYS.index(A1_DAY)
daily = repo.get_daily_batch(SYMS, START, DAYS[-1], columns=["symbol", "date", "high", "low", "close"])
H = np.full((T, N), np.nan)
L = np.full((T, N), np.nan)
C = np.full((T, N), np.nan)
for s, d, h, lo, c in daily.select(["symbol", "date", "high", "low", "close"]).iter_rows():
    t, j = DI.get(d), SI.get(s)
    if t is not None and j is not None:
        H[t, j], L[t, j], C[t, j] = h, lo, c
ATR = np.full((T, N), np.nan)
for j in range(N):
    ok = np.flatnonzero(np.isfinite(C[:, j]))
    if ok.size < 15:
        continue
    h, lo, c = H[ok, j], L[ok, j], C[ok, j]
    tr = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
    for k in range(13, tr.size):
        ATR[ok[k + 1], j] = float(np.mean(tr[k - 13:k + 1]))       # = decision_desk.atr (截至当日最后 14 根 TR)
spy_c = C[:, SI["SPY"]]
SPY_B50 = np.zeros(T, bool)
for t in range(49, T):
    w = spy_c[t - 49:t + 1]
    SPY_B50[t] = bool(np.all(np.isfinite(w)) and spy_c[t] < w.mean())
COLS = np.array([s in set(WL) and s not in ("SPY", "QQQ") for s in SYMS])
print(f"universe={UNIVERSE} watchlist={len(WL)} days={T} {DAYS[0]}..{DAYS[-1]} window={DAYS[B0]}..{DAYS[A1]}", flush=True)

# ---- 信号 (生产矩阵路径) ----
engine = runner._strategy_engine(MIRROR)
svc = StrategyBacktestService(BacktestEngine(repo), engine)
ids = [m["id"] for m in engine.list_strategies()
       if m.get("source", "builtin") == "builtin" and not m.get("research_only")
       and m.get("execution_backend") == "matrix_native"
       and "stock" in m.get("asset_types", ["stock"]) and "1d" in m.get("timeframes", ["1d"])
       and (ONLY is None or m["id"] in ONLY)]
if ONLY is not None and set(ids) != ONLY:
    raise SystemExit(f"--only rules not found as public builtin matrix strategies: {sorted(ONLY - set(ids))}")
LH, SH, errs = {}, {}, {}


def align(md, m):
    out = np.zeros((T, N), bool)
    rows = {x[:10]: i for i, x in enumerate(md.timestamp_labels)}
    cols = {s: i for i, s in enumerate(md.symbols)}
    ri = [(t, rows.get(d.isoformat())) for t, d in enumerate(DAYS)]
    ci = [(j, cols.get(s)) for j, s in enumerate(SYMS)]
    for t, r in ri:
        if r is None:
            continue
        for j, c in ci:
            if c is not None:
                out[t, j] = bool(m[r, c])
    return out


for sid in ids:
    t0 = time.time()
    try:
        # 截面策略: 排名分母 = 全池, 必须在全市场矩阵上算, align 再按名单取列
        cross = bool(engine.get(sid).meta.get("cross_sectional"))
        md, sig, _stop = runner._signal_matrix(sid, svc, engine, START, DAYS[-1], symbols=None if cross else SYMS,
                                               disk_cache=False)
    except Exception as e:  # 单条规则失败不能拖住整轮回放
        errs[sid] = f"{type(e).__name__}: {str(e)[:160]}"
        print("ERR", sid, errs[sid], flush=True)
        continue
    LH[sid] = align(md, np.asarray(sig.entry, bool))
    if sid in wv.SELL_IS_SHORT:
        SH[sid] = align(md, np.asarray(sig.exit, bool))
    print(f"{sid:32s} entry={int(LH[sid][:, COLS].sum())} {time.time() - t0:.1f}s", flush=True)

# ---- 4h 门 (生产 4h 数据层 + Phantom 扫描) ----
t0 = time.time()
G_L = np.zeros((T, N), bool)
G_S = np.zeros((T, N), bool)
bars4 = kline_4h.bars_4h(MIRROR, repo, [s for s in SYMS if COLS[SI[s]]], DAYS[0] - timedelta(days=30), DAYS[-1] + timedelta(days=1))
cover = {}
for s, df in bars4.items():
    j = SI[s]
    if df is None or len(df) < phantom_4h.MIN_BARS:
        continue
    sc = phantom_4h.scan_series(df)
    if "direction" not in sc:
        continue
    idx, act, dr = sc.index.values, sc["action"].values, sc["direction"].values
    for t in range(1, T):
        lo = np.datetime64(pd.Timestamp(DAYS[t - 1]) + pd.Timedelta(hours=13, minutes=30))
        hi = np.datetime64(pd.Timestamp(DAYS[t]) + pd.Timedelta(hours=9, minutes=30))
        a = np.searchsorted(idx, lo, "left")
        b = np.searchsorted(idx, hi, "right")
        if b == 0:
            continue
        w = act[a:b]
        G_L[t, j] = bool((w == "多头条件齐备").any()) or str(dr[b - 1]) == "↑ 看多"
        G_S[t, j] = bool((w == "空头条件齐备").any()) or str(dr[b - 1]) == "↓ 看空"
    cover[s] = str(df.index[0])[:10]
print(f"4h gate built for {len(cover)} symbols in {time.time() - t0:.0f}s", flush=True)

# ---- 卡结局 (生产 replay_card, 30 分钟K) ----
t0 = time.time()
cells, detail = {}, {}
for j, s in enumerate(SYMS):
    if not COLS[j]:
        continue
    need = [t for t in range(B0, A1 + 1) if G_L[t, j] or (G_S[t, j] and SPY_B50[t])]
    if not need:
        continue
    m30 = ss._m30_days(MIRROR, repo, s, DAYS[B0], DAYS[-1])
    for t in need:
        for side, ok in (("long", G_L[t, j]), ("short", G_S[t, j] and SPY_B50[t])):
            if not ok:
                continue
            g = sd.geometry(side, float(C[t, j]), float(ATR[t, j])) if np.isfinite(C[t, j]) and np.isfinite(ATR[t, j]) else None
            if g is None:
                continue
            seq = []
            for k in range(1, 8):
                if t + k >= T or DAYS[t + k] not in m30:
                    break
                seq.append(m30[DAYS[t + k]])
            res = ss.replay_card(side, tuple(g["entry_zone"]), g["stop"], g["target"], seq)
            # 与 10-18 同: 数据缺口导致走不完 = 过门但未成交 (研究内核返回 None)
            cells[(side, t, j)] = float(res["r"]) if res["status"] == "closed" else None
            detail[(side, t, j)] = (res.get("status"), res.get("fill_day"), res.get("exit_day"), res.get("why"),
                                    res.get("r"), tuple(g["entry_zone"]), g["stop"], g["target"], len(seq))
print(f"cells {len(cells)} fills {sum(v is not None for v in cells.values())} in {time.time() - t0:.0f}s", flush=True)

base = defaultdict(list)
for (side, t, _j), v in cells.items():
    if v is not None:
        base[(side, t)].append(v)
base_mean = {k: float(np.mean(v)) for k, v in base.items()}


def week_boot_p(pairs, n_boot=4000, seed=7):
    """Same as 10-18: P(mean <= 0) by resampling whole weeks (t // 5)."""
    s, n = defaultdict(float), defaultdict(int)
    for t, x in pairs:
        s[t // 5] += x
        n[t // 5] += 1
    ks = list(s)
    sums, cnts = np.array([s[k] for k in ks]), np.array([n[k] for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n_boot, len(ks)))
    return float(((sums[pick].sum(1) / cnts[pick].sum(1)) <= 0).mean())


def rule_stats(side, hits):
    raw = cand = 0
    rs, ex = [], []
    for t in range(B0, A1 + 1):
        for j in np.flatnonzero(hits[t] & COLS):
            raw += 1
            k = (side, t, int(j))
            if k not in cells:
                continue
            cand += 1
            v = cells[k]
            if v is None:
                continue
            rs.append(v)
            ex.append((t, v - base_mean[(side, t)]))
    if len(rs) < 5:
        return {"n": len(rs), "signals": cand, "raw": raw, "thin": True}
    return {"n": len(rs), "fill": round(len(rs) / cand, 3), "mean_r": round(float(np.mean(rs)), 4),
            "excess": round(float(np.mean([x for _, x in ex])), 4), "p": round(week_boot_p(ex), 3)}


rules = {}
for r in sorted(LH):
    rules[r] = rule_stats("long", LH[r])
for r in sorted(SH):
    rules[f"{r}:short"] = rule_stats("short", SH[r])
out = {"contract": "swing-desk-tight-v1", "window": [DAYS[B0].isoformat(), DAYS[A1].isoformat()],
       "universe": f"watchlist {len(WL)} ({DATA_END.isoformat()})" if UNIVERSE == "current" else "watchlist 58 (2026-10-15)",
       "data_end": DATA_END.isoformat(),
       "universe_note": "universe = watchlist at data_end; rules with < 5 fills keep counts only",
       "r_unit": "per-share R vs plan risk (zone mid to stop), incl. 5bps slippage, no fees, no account limits",
       "baseline": "same-day same-side 4h-gated cells, same tight card",
       "source": "backend/scripts/rebuild_swing_history.py (production signal matrix, kline_4h + phantom_4h gate, "
                 "swing_settlement.replay_card, 30m)",
       "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
       "thin": "rules with < 5 fills: n = fills, signals = 4h-gated cards, raw = hits before the gate; no mean_r/excess",
       "rules": rules}
if errs:
    out["errors"] = errs
if EXISTING is not None:
    merged = dict(EXISTING)
    merged["rules"] = {**EXISTING["rules"], **rules}
    merged["added"] = {**(EXISTING.get("added") or {}),
                        **{r: {"generated": out["generated"], "data_end": DATA_END.isoformat(),
                               "universe": f"watchlist {len(WL)}"} for r in rules}}
    if errs:
        merged["errors"] = {**(EXISTING.get("errors") or {}), **errs}
    out = merged
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
if "--dump" in sys.argv:
    import pickle
    with OUT.with_suffix(".cells.pkl").open("wb") as f:
        pickle.dump({"days": [d.isoformat() for d in DAYS], "syms": SYMS, "B0": B0, "A1": A1,
                     "detail": {(side[0], DAYS[t].isoformat(), SYMS[j]): v for (side, t, j), v in detail.items()},
                     "C": C, "ATR": ATR, "H": H, "L": L, "G_L": G_L, "G_S": G_S, "SPY_B50": SPY_B50,
                     "LH": LH, "SH": SH}, f)
print("rules", len(rules), "thin", sum(1 for v in rules.values() if v.get("thin")), "errors", len(errs),
      "written", OUT, f"total {time.time() - t_all:.0f}s", flush=True)
print("4h coverage start (late):", sorted(((v, k) for k, v in cover.items() if v > "2022-12-01")), flush=True)
