"""挖掘候选 · 按机会板卡回放验收 (0.26.0 第④批, 无限 10-18 确认“策略自主发现显然方案”)。

为什么: 周度挖掘与 AI 迭代原来按“次日开盘买、前 10 名等权、拿 5 天”验收, 与机会板卡 (挂单区回踩成交、止损、目标、
5 天) 是两套买法。W42 挖出的“低波动”排序, 按卡回放 +18.8R/年, 而“4h 方向对了就随便挑”是 +44.3R/年 (方向相反)。

做法 (只读、确定性, 不改交易):
  1. 候选 (已发布到 strategies/custom 或 ai 的日线策略) 用生产同一条信号路径 (矩阵: strategy_evaluation_runner._signals_for;
     其他: 回测非矩阵分支) 在全市场上出每日入场信号与分数, 再取自选列 (截面排序策略只在自选上算会换掉分母与名次)。
     窗口 2023-01-03 ~ 上一交易日。触发: 周度挖掘结束; 人点“发布”(挖掘候选 / AI 草稿) 后后台补一次。
  2. 与机会板同一出卡口径: 4h 门 (事件或同向状态, 来自生产 30 分钟K 合成的 4h Phantom) 过门, 每天按分数取前 5 张多头卡;
     卡几何 = tight-v1 (swing_desk.geometry), 结局 = 30 分钟K 逐根回放 (swing_settlement.replay_card, 与纸面同一套规则)。
  3. 账户约束与生产一致 (第②批): 每笔 0.5% -> 最多 12 仓 (SPY<200 日线 6 仓), 每天新开 10 笔, 同板块同方向 4 仓, 冷却 3 天。
  4. 基线 = 同一批门内格子每天随机挑 5 张 (固定种子 5 个, 取平均), 同一账户约束。
  5. 过关 = 候选每笔 R 减同日随机基线每笔 R 的差, 周聚类自助法 95% 区间下限 > 0 (只看均值不算过)。
     过关只打“候选”标记 (research/swing_candidates.json), 进 3+3 勾选列表, 勾不勾由人决定; 系统不自己换规则。
失败判据: 连续多周 0 个候选赢过随机 -> 这条路对这张卡没用, 可关掉 (设置页挖掘开关)。
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

CONTRACT = "swing-candidate-v1"
PER_SIDE = 5
SEEDS = (1, 2, 3, 4, 5)
N_BOOT = 2000
RISK_PCT = 0.005                       # 第②批: 每笔 0.5%
WINDOW_START = date(2023, 1, 3)        # 验收窗口起点 = 回测窗口 2023-2026 (与规则面板历史、卡面基础率同一起点), 终点 = 上一交易日


def candidates_path(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "swing_candidates.json"


def read_candidates(data_dir: Path) -> dict:
    try:
        d = json.loads(candidates_path(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def candidate_ids(data_dir: Path) -> set[str]:
    """过关的规则 id (3+3 勾选列表里标“候选”)。"""
    return {k for k, v in (read_candidates(data_dir).get("rules") or {}).items() if isinstance(v, dict) and v.get("passed")}


# ── 纯函数: 账户约束 + 选卡 + 基线 + 判定 ──────────────────────────────────

def account(picks: list[tuple], *, max_pos: int, day_cap: int, sect_cap: int, yellow_cap: int,
            yellow: dict[int, bool], sectors: dict[str, str], cooldown: int = 3) -> list[dict]:
    """picks = [(fill_t, exit_t, sym, t, r, score)] (已成交的卡), 按成交时刻、分数排队, 过账户约束。返回成交。
    与生产准入同序: 同票持仓 -> 冷却 -> 仓位上限 (SPY<200 日线时按 yellow_cap) -> 当日新开 -> 同板块同方向。"""
    taken, open_ = [], []
    od: dict[int, int] = defaultdict(int)
    last_exit: dict[str, int] = {}
    for fi, xi, sym, t, r, _sc in sorted(picks, key=lambda x: (x[0], -x[5], x[2])):
        open_ = [q for q in open_ if q[1] >= fi]
        if any(q[0] == sym for q in open_):
            continue
        if sym in last_exit and fi - last_exit[sym] < cooldown:
            continue
        cap = yellow_cap if yellow.get(fi) else max_pos
        if len(open_) >= cap or od[fi] >= day_cap:
            continue
        sec = sectors.get(sym)
        if sec and sum(1 for q in open_ if sectors.get(q[0]) == sec) >= sect_cap:
            continue
        open_.append((sym, xi))
        od[fi] += 1
        last_exit[sym] = max(last_exit.get(sym, -99), xi)
        taken.append({"t": t, "fill": fi, "exit": xi, "sym": sym, "r": r})
    return taken


def week_boot_ci(pairs: list[tuple[int, float]], *, n_boot: int = N_BOOT, seed: int = 7) -> list[float] | None:
    """pairs = [(t, x)] -> 均值的周聚类自助法 95% 区间。样本 < 10 -> None。"""
    if len(pairs) < 10:
        return None
    s, n = defaultdict(float), defaultdict(int)
    for t, x in pairs:
        s[t // 5] += x
        n[t // 5] += 1
    ks = list(s)
    sums, cnts = np.array([s[k] for k in ks]), np.array([n[k] for k in ks])
    pick = np.random.default_rng(seed).integers(0, len(ks), (n_boot, len(ks)))
    m = sums[pick].sum(1) / np.maximum(cnts[pick].sum(1), 1)
    return [round(float(np.percentile(m, 2.5)), 4), round(float(np.percentile(m, 97.5)), 4)]


def judge(cand: list[dict], base_runs: list[list[dict]], years: float) -> dict:
    """候选成交 vs 随机基线成交 (多个种子)。差 = 候选每笔 R - 同日基线每笔 R 的均值 (按信号日对齐)。"""
    by_day = defaultdict(list)
    for run in base_runs:
        for x in run:
            by_day[x["t"]].append(x["r"])
    day_mean = {t: float(np.mean(v)) for t, v in by_day.items()}
    glob = float(np.mean([x["r"] for run in base_runs for x in run])) if any(base_runs) else 0.0
    diff = [(x["t"], x["r"] - day_mean.get(x["t"], glob)) for x in cand]
    ci = week_boot_ci(diff)
    base_ryr = float(np.mean([sum(x["r"] for x in run) for run in base_runs])) / years if base_runs and years > 0 else None
    return {"n": len(cand), "mean_r": round(float(np.mean([x["r"] for x in cand])), 4) if cand else None,
            "r_per_year": round(sum(x["r"] for x in cand) / years, 1) if years > 0 else None,
            "base_r_per_year": round(base_ryr, 1) if base_ryr is not None else None,
            "excess": round(float(np.mean([d for _, d in diff])), 4) if diff else None, "excess_ci": ci,
            "passed": bool(ci is not None and ci[0] > 0)}


# ── 生产数据: 30 分钟K / 4h 门 / 卡结局 ────────────────────────────────────

def _cells(data_dir: Path, repo, symbols: list[str], days: list[date], close: np.ndarray, atr: np.ndarray,
           gate: np.ndarray) -> dict[tuple[int, int], tuple]:
    """全部过门的 (t, j) 格子的卡结局: {(t,j): (fill_t, exit_t, r) | None}。只算多头 (挖掘出的是多头排序)。"""
    from app.services import swing_desk as sd
    from app.services import swing_settlement as ss
    out = {}
    n_days = len(days)
    for j, sym in enumerate(symbols):
        ts = [t for t in range(n_days - 1) if gate[t, j] and math.isfinite(close[t, j]) and math.isfinite(atr[t, j]) and atr[t, j] > 0]
        if not ts:
            continue
        bars = ss._m30_days(data_dir, repo, sym, days[0], days[-1])
        if not bars:
            continue
        for t in ts:
            g = sd.geometry("long", float(close[t, j]), float(atr[t, j]))
            if g is None:
                continue
            seq = []
            for k in range(1, 8):
                if t + k >= n_days or days[t + k] not in bars:
                    break
                seq.append(bars[days[t + k]])
            res = ss.replay_card("long", tuple(g["entry_zone"]), g["stop"], g["target"], seq)
            if res["status"] == "no_fill":
                out[(t, j)] = None
            elif res["status"] == "closed":
                out[(t, j)] = (t + 1 + res["fill_day"], t + 1 + res["exit_day"], float(res["r"]))
    return out


def _gate_matrix(data_dir: Path, repo, symbols: list[str], days: list[date]) -> np.ndarray:
    """4h 门 (T,N): 生产 30 分钟K -> 4h Phantom, 可见窗 [前日 13:30, 当日 09:30] 内“多头条件齐备”或最后方向 = 多。"""
    import pandas as pd

    from app.services import kline_4h, phantom_4h
    from app.services.phantom_4h_source import DIRECTION_SIDE
    n_days, n_syms = len(days), len(symbols)
    gate = np.zeros((n_days, n_syms), bool)
    bars = kline_4h.bars_4h(data_dir, repo, symbols, days[0] - timedelta(days=400), days[-1] + timedelta(days=1))
    for j, sym in enumerate(symbols):
        df = bars.get(sym)
        if df is None or len(df) < phantom_4h.MIN_BARS:
            continue
        s = phantom_4h.scan_series(df)
        idx = s.index.values
        act = s["action"].values
        dr = s["direction"].values
        for t in range(1, n_days):
            lo = np.datetime64(pd.Timestamp(days[t - 1]) + pd.Timedelta(hours=13, minutes=30))
            hi = np.datetime64(pd.Timestamp(days[t]) + pd.Timedelta(hours=9, minutes=30))
            a = np.searchsorted(idx, lo, "left")
            b = np.searchsorted(idx, hi, "right")
            if b == 0:
                continue
            gate[t, j] = bool((act[a:b] == "多头条件齐备").any()) or DIRECTION_SIDE.get(str(dr[b - 1])) == "long"
    return gate


def evaluate(data_dir: Path, repo, strategy_ids: list[str], *, end: date, watchlist: set[str]) -> dict:
    """对每个候选策略做卡回放验收, 写 research/swing_candidates.json (原子写)。返回写入内容。"""
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.services import strategy_evaluation_runner as runner
    from app.services.fs_utils import atomic_write_text
    engine = runner._strategy_engine(Path(data_dir))
    svc = StrategyBacktestService(BacktestEngine(repo), engine)
    start = WINDOW_START
    out = {"contract": CONTRACT, "window": [start.isoformat(), end.isoformat()], "risk_pct": RISK_PCT,
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "rules": {}}
    prior = read_candidates(data_dir).get("rules") or {}
    shared = None
    for sid in strategy_ids:
        try:
            md, entry, score = _signals_any(sid, svc, engine, start, end, sorted(watchlist))
        except Exception as e:
            logger.warning("swing candidate %s: signals failed: %s: %s", sid, type(e).__name__, str(e)[:200])
            # 0.30.0: keep the message, the gear shows it (e.g. a custom signal the strategy needs was deleted)
            out["rules"][sid] = {"error": type(e).__name__, "detail": str(e)[:200]}
            continue
        labels = [date.fromisoformat(x[:10]) for x in md.timestamp_labels]
        keep = [i for i, d in enumerate(labels) if start <= d <= end]
        cols = [j for j, s in enumerate(md.symbols) if s in watchlist]
        if not keep or not cols:
            out["rules"][sid] = {"error": "no_watchlist_data"}
            continue
        days = [labels[i] for i in keep]
        syms = [md.symbols[j] for j in cols]
        close = md.close[np.ix_(keep, cols)].astype(float)
        hi, lo = md.high[np.ix_(keep, cols)].astype(float), md.low[np.ix_(keep, cols)].astype(float)
        if shared is None or shared["syms"] != syms or shared["days"] != days:
            pc = np.vstack([close[:1], close[:-1]])
            tr = np.fmax(hi - lo, np.fmax(np.abs(hi - pc), np.abs(lo - pc)))
            atr = np.full_like(close, np.nan)
            with np.errstate(all="ignore"):
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    for t in range(13, len(days)):
                        atr[t] = np.nanmean(tr[t - 13:t + 1], axis=0)
            gate = _gate_matrix(data_dir, repo, syms, days)
            cells = _cells(data_dir, repo, syms, days, close, atr, gate)
            yellow = _spy_below_ma200(repo, days)
            sectors = _sectors(repo)
            shared = {"syms": syms, "days": days, "cells": cells, "gate": gate, "yellow": yellow, "sectors": sectors}
        ent = entry[np.ix_(keep, cols)].astype(bool)
        scr = np.nan_to_num(score[np.ix_(keep, cols)].astype(float), nan=-np.inf)
        cells, gate = shared["cells"], shared["gate"]
        kw = {"max_pos": int(0.06 / RISK_PCT), "day_cap": int(0.05 / RISK_PCT), "sect_cap": int(0.02 / RISK_PCT),
              "yellow_cap": int(0.03 / RISK_PCT), "yellow": shared["yellow"], "sectors": shared["sectors"]}
        picks = []
        for t in range(len(days)):
            js = [j for j in np.flatnonzero(ent[t] & gate[t]) if (t, j) in cells]
            js = sorted(js, key=lambda j: (-scr[t, j], syms[j]))[:PER_SIDE]
            for j in js:
                v = cells[(t, j)]
                if v is not None:
                    picks.append((v[0], v[1], syms[j], t, v[2], scr[t, j]))
        cand = account(picks, **kw)
        base_runs = []
        for seed in SEEDS:
            rg = np.random.default_rng(seed)
            bp = []
            for t in range(len(days)):
                js = [j for j in np.flatnonzero(gate[t]) if (t, j) in cells]
                if not js:
                    continue
                for j in rg.permutation(js)[:PER_SIDE]:
                    v = cells[(t, int(j))]
                    if v is not None:
                        bp.append((v[0], v[1], syms[int(j)], t, v[2], 0.0))
            base_runs.append(account(bp, **kw))
        years = len(days) / 252
        res = judge(cand, base_runs, years)
        res["first_passed"] = (prior.get(sid) or {}).get("first_passed") or (end.isoformat() if res["passed"] else None)
        out["rules"][sid] = res
        logger.info("swing candidate %s: %s", sid, res)
    p = candidates_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(out, ensure_ascii=False, indent=1))
    return out


def _signals_any(sid: str, svc, engine, start: date, end: date, symbols: list[str]):
    """(market_like, entry (T,N) bool, score (T,N) float), 只留自选列。信号在全市场上算, 与生产出卡同一口径:
    生产盘后扫描在流动性池上跑策略再取自选命中; 挖掘发布的因子排名策略的分数是截面百分位、入场取前 20 名,
    只在几十只自选上算会换掉分母和名次, 验收的就不是会上线的那条策略 (0.26.0 自审 F2)。
    矩阵策略走 strategy_evaluation_runner._signals_for (与回测页同一条信号路径); 其他日线策略 (polars_expr /
    python_history_legacy) 走回测非矩阵分支同一组调用 (load_panel_for_backtest -> 候选层 & 买点层 -> 评分)。
    NAS 实测 (10-18, 生产容器, 关磁盘缓存): 全市场单策略峰值 1.49~1.57 GB, 可用 4.5 GB。"""
    from types import SimpleNamespace

    import polars as pl

    from app.backtest.strategy import StrategyDependencyResolver, _basic_filter_for_asset
    from app.services import strategy_evaluation_runner as runner
    want = set(symbols)
    s = engine.get(sid)
    if s.execution_backend == "matrix_native":
        md, entry, score, _stop = runner._signals_for(sid, svc, engine, start, end)
        cols = [j for j, x in enumerate(md.symbols) if x in want]
        sub = SimpleNamespace(timestamp_labels=list(md.timestamp_labels), symbols=[md.symbols[j] for j in cols],
                              close=np.asarray(md.close)[:, cols], high=np.asarray(md.high)[:, cols],
                              low=np.asarray(md.low)[:, cols])
        return sub, np.asarray(entry)[:, cols], np.asarray(score)[:, cols]
    if s.execution_backend not in ("polars_expr", "python_history_legacy"):
        raise ValueError(f"unsupported backend {s.execution_backend}")
    overrides = (engine._override_loader(sid) if engine._override_loader else {}) or {}
    params = svc._normalize_params(dict(overrides.get("params") or {}), s)
    basic_filter = _basic_filter_for_asset(svc._effective_basic_filter(s, overrides), "stock")
    entry_signals = svc._effective_signals(overrides, "entry_signals", s.entry_signals)
    exit_signals = svc._effective_signals(overrides, "exit_signals", s.exit_signals)
    plan = StrategyDependencyResolver().resolve(s, params=params, basic_filter=basic_filter, entry_signals=entry_signals,
                                                exit_signals=exit_signals, overrides=overrides, minute_fill=False,
                                                asset_type="stock")
    panel = svc.engine.load_panel_for_backtest(None, start - timedelta(days=200), end, plan, asset_type="stock")
    if panel.is_empty():
        raise ValueError("empty panel")
    cand = svc._build_candidate_filter_mask(panel, s, params)
    panel = svc._apply_score(panel, s, overrides, universe_mask=cand)
    ent = svc._build_entry_mask_from_candidate(panel, cand, s, entry_signals)
    panel = panel.with_columns(ent.alias("_entry"))
    if "score" not in panel.columns:
        panel = panel.with_columns(pl.lit(0.0).alias("score"))
    panel = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end) & pl.col("symbol").is_in(sorted(want)))
    days = sorted(panel["date"].unique().to_list())
    syms = sorted(panel["symbol"].unique().to_list())
    di, si = {d: i for i, d in enumerate(days)}, {x: j for j, x in enumerate(syms)}
    n_days, n_syms = len(days), len(syms)
    close, high, low = (np.full((n_days, n_syms), np.nan) for _ in range(3))
    entry, score = np.zeros((n_days, n_syms), bool), np.full((n_days, n_syms), np.nan)
    for sym, d, c, h, lo, e, sc in panel.select(["symbol", "date", "close", "high", "low", "_entry", "score"]).iter_rows():
        t, j = di[d], si[sym]
        close[t, j], high[t, j], low[t, j] = c, h, lo
        entry[t, j] = bool(e)
        score[t, j] = sc if sc is not None else np.nan
    md = SimpleNamespace(timestamp_labels=[d.isoformat() for d in days], symbols=syms, close=close, high=high, low=low)
    return md, entry, score


def discovered_ids(engine) -> list[str]:
    """系统发现的策略 = 挖掘发布 (source=custom) + AI 迭代 (source=ai), 日线股票、非研究草稿。内置策略的
    tight-v1 历史已在 config/swing_rule_history.json (规则面板)。"""
    out = []
    for m in engine.list_strategies():
        if m.get("source") not in ("custom", "ai") or m.get("research_only"):
            continue
        if "stock" not in m.get("asset_types", ["stock"]) or "1d" not in m.get("timeframes", ["1d"]):
            continue
        if m.get("execution_backend") not in ("matrix_native", "polars_expr", "python_history_legacy"):
            continue
        out.append(m["id"])
    return sorted(out)


def worker_run(data_dir: Path, repo, cfg: dict) -> dict:
    """子进程入口 (backtest/worker.py kind=swing_candidates)。与 NAS 实测同一配置 (10-18, 生产容器: 单策略峰值
    1.49~1.57 GB, 60~77 秒): 全市场矩阵只在内存里建, 不写也不挤占回测磁盘缓存; 复权系数/SPY 按日期扫 parquet,
    不在子进程里重建整份日K指标缓存 (那是主进程启动时的 50 秒级重活)。"""
    from app.config import settings
    settings.backtest_matrix_disk_cache_enabled = False
    repo._enriched_warming = True
    out = evaluate(data_dir, repo, list(cfg["strategy_ids"]), end=date.fromisoformat(cfg["end"]),
                   watchlist=set(cfg["watchlist"]))
    return {"rules": {k: {kk: vv for kk, vv in v.items() if kk in ("passed", "n", "excess", "error")}
                      for k, v in (out.get("rules") or {}).items()}}


def run_after_mining(data_dir: Path, repo, *, end: date) -> dict:
    """周度挖掘结束后、或人点“发布”策略后调用: 对全部系统发现的策略做卡回放验收; 失败由调用方记日志。
    串行靠共享重任务额度 (slot("mining") = 全部额度): 两次验收、验收与回测/挖掘都不会同时跑。
    不另加进程锁 —— 挖掘线程是持有额度后才调到这里, 再加一把锁会与“先拿锁再等额度”的后台线程互相等死。"""
    return _run_after_mining(data_dir, repo, end=end)


def pending_path(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "swing_candidates_pending.json"


def read_pending(data_dir: Path) -> dict | None:
    try:
        d = json.loads(pending_path(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _clear_pending(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        pending_path(data_dir).unlink()


def _busy(now: datetime | None, end_hm: tuple[int, int]) -> bool:
    """Trading day and 09:00 ET <= now < end (ET)."""
    from datetime import time as dtime

    from app.market_clock import us_clock
    now = us_clock.market_datetime(now or us_clock.now())
    return us_clock.is_trading_day(now.date()) and dtime(9, 0) <= now.time() < dtime(*end_hm)


def can_run_now(now: datetime | None = None) -> bool:
    """0.30.0: the check holds the whole heavy-job budget and reads the full-market matrix (~1.6 GB, ~60 s).
    During the session the quote service republishes the enriched data every ~10 s, so the read cannot get a
    stable snapshot (10-21 00:41 BJ: ai_febf208d failed this way) and the run competes with the trading loop.
    09:00-17:00 ET on trading days a request is queued instead (17:00 also keeps it clear of the 16:35 pipeline)."""
    return not _busy(now, (17, 0))


_RUN_LOCK = threading.Lock()
_RUNS = [0]


def is_running() -> bool:
    return _RUNS[0] > 0


def request_check(data_dir: Path, repo, *, reason: str, now: datetime | None = None, if_idle: bool = False) -> dict:
    """Ask for one card-replay check. Off-hours it starts now in the background; 09:00-17:00 ET on trading days
    it is queued in research/swing_candidates_pending.json and the 17:05 ET job (zen_swing_candidates_queue)
    starts it. if_idle (the gear button): do nothing while a check is already running."""
    if if_idle and is_running():
        return {"started": False, "queued": False, "running": True}
    if can_run_now(now):
        _clear_pending(data_dir)
        schedule_check(data_dir, repo, reason=reason)
        return {"started": True, "queued": False, "running": True}
    from app.services.fs_utils import atomic_write_text
    p = pending_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(p, json.dumps({"reason": reason, "requested_at": at}, ensure_ascii=False))
    return {"started": False, "queued": True, "running": False, "requested_at": at}


def run_pending(data_dir: Path, repo, *, now: datetime | None = None) -> bool:
    """17:05 ET job: start a queued request once. True = started. Inside the busy window it stays queued."""
    if read_pending(data_dir) is None or not can_run_now(now):
        return False
    _clear_pending(data_dir)
    schedule_check(data_dir, repo, reason="queued")
    return True


def status(data_dir: Path) -> dict:
    """For the gear: when the last check ran, whether one is running now, and whether one is queued."""
    d = read_candidates(data_dir)
    p = read_pending(data_dir) or {}
    return {"generated_at": d.get("generated_at"), "window": d.get("window"), "running": is_running(),
            "pending_at": p.get("requested_at")}


def schedule_check(data_dir: Path, repo, *, reason: str) -> threading.Thread:
    """人点“发布”(挖掘候选 / AI 草稿) 后在后台补一次验收, 不用等下周挖掘跑完才出“候选”标记 (0.26.0 自审 F3)。
    后台线程, 不阻塞发布请求; 失败只记日志。返回线程 (测试 join 用)。"""
    from app.market_clock import us_clock

    def run():
        try:
            run_after_mining(data_dir, repo, end=us_clock.previous_trading_day(us_clock.today()))
        except Exception as e:
            logger.warning("swing candidate check after %s failed (soft): %s", reason, type(e).__name__)
        finally:
            with _RUN_LOCK:
                _RUNS[0] -= 1
    # count before start: the gear reads is_running() right after the POST returns
    with _RUN_LOCK:
        _RUNS[0] += 1
    th = threading.Thread(target=run, name=f"swing-candidates-{reason}", daemon=True)
    try:
        th.start()
    except Exception:
        with _RUN_LOCK:
            _RUNS[0] -= 1
        raise
    return th


def _run_after_mining(data_dir: Path, repo, *, end: date) -> dict:
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation_runner as runner
    from app.services.heavy_job_limiter import shared_heavy_job_limiter
    ids = discovered_ids(runner._strategy_engine(Path(data_dir)))
    if not ids:
        out = {"contract": CONTRACT, "window": None, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "rules": {}, "note": "no_discovered_strategies"}
        from app.services.fs_utils import atomic_write_text
        p = candidates_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(out, ensure_ascii=False, indent=1))
        return out
    from app.backtest.worker import make_worker_task, run_worker_task
    cfg = {"strategy_ids": ids, "end": end.isoformat(), "watchlist": sorted(job._watchlist())}
    # 独占重任务额度 (与挖掘同权重): 全市场矩阵峰值约 1.5 GB, 不与回测/证据周更等其他重任务叠加。
    # 挖掘线程里调用时它本来就持有这份额度 (复用, 不会自锁); 发布后的后台线程会排队等其他重任务结束。
    with shared_heavy_job_limiter.slot("mining"):
        res = run_worker_task(make_worker_task("swing_candidates", Path(data_dir), cfg))
    if res.get("error"):
        raise RuntimeError(f"swing candidates worker failed: {res['error']}")
    return read_candidates(data_dir)


def _spy_below_ma200(repo, days: list[date]) -> dict[int, bool]:
    """{t: SPY 收盘 < 200 日线} (与交易环大盘灯同式: 收盘 < 最近 200 根收盘均值)。缺数据 -> False。"""
    try:
        spy = repo.get_daily("SPY", days[0] - timedelta(days=420), days[-1], columns=["date", "close"])
    except Exception:
        return {}
    if spy is None or spy.is_empty():
        return {}
    ds, cs = spy["date"].to_list(), [float(x) for x in spy["close"].to_list()]
    below = {}
    for i, d in enumerate(ds):
        if i >= 199:
            below[d] = cs[i] < sum(cs[i - 199:i + 1]) / 200
    return {t: bool(below.get(d)) for t, d in enumerate(days)}


def _sectors(repo) -> dict[str, str]:
    """板块表 (与 OpportunityService.sector_map 同口径)。读失败 -> 空 (同板块上限不生效, 只影响验收不影响交易)。"""
    try:
        df = repo.get_instruments()
        if "sector" not in df.columns:
            return {}
        return {r["symbol"]: r["sector"] for r in df.select(["symbol", "sector"]).to_dicts()
                if r.get("sector") and r["sector"] not in ("?", "N/A", "Unknown")}
    except Exception:
        return {}
