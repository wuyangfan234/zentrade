"""Phantom 4h 候选来源 (决策台用) — 原版 4h Phantom 简化扫描 + 吊灯止损。

盘后扫描 (美东 16:35 后) 使用区间 [前一交易日13:30, 当日09:30] 内真实存在的已完成根。
04:00–20:00 ET 源在完整交易日通常贡献5根：前日13:30/17:30和当日01:30/05:30/09:30。
01:30标签包含当日04:00–05:00的成交；21:30无成交的空箱由原函数删除，不合成隔夜K线。
当日信号 = 这些根里出现过“多头条件齐备”, 取最后一根的原 score 与吊灯止损 (long_trail) 为保护位。
每日按 score 取前 10 只 (与其他来源同一 PICKS_PER_DAY), 只看流动性池内股票。
止损比例 = long_trail / 收盘 − 1 (逐股); long_trail ≥ 收盘时按原 _protection_for 视为不可用 → 只用统一纪律 2ATR。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SOURCE_ID = "phantom_4h"
SOURCE_NAME = "Phantom 4h 波段（原版·吊灯止损）"
SHORT_SOURCE_ID = "phantom_4h_short"
SHORT_SOURCE_NAME = "Phantom 4h 做空（原版·空头吊灯止损）"
# 2026-10-09: Pine 原文仪表盘“积极做多”(≥4/5 灯) 出现 —— 与上面的简化扫描是两套规则,
# 结构判定只有 64% 一致 (见 phantom_4h_pine 模块说明)。独立来源: 只进影子账本/自选信号表攒前向样本, 不改 phantom_4h、不进规则集。
PINE_SOURCE_ID = "phantom_4h_pine"
PINE_SOURCE_NAME = "Phantom 4h Pine 原版 (仪表盘积极做多·吊灯止损)"
SCAN_POOL_SIZE = 200          # 前 60 日成交额中位数前 200 (与回测同口径)
HISTORY_DAYS = 200            # 计算用 4h 历史 (约 9 个月, 吊灯/EMA50/ADX 充分预热)
PINE_HISTORY_DAYS = 390       # Pine 动能振荡器要 1000 根分位窗 (≈1060 根预热); 390 自然日 ≈ 1340 根, 与全历史逐根一致


def pool(repo, as_of: date, frames: dict) -> list[str]:
    """流动性池内按前 60 日成交额中位数取前 200 (frames = 决策台已加载的日K)。"""
    import polars as pl
    rows = []
    for sym, b in frames.items():
        if b.is_empty() or b.height < 60:
            continue
        t = b.filter(pl.col("date") < as_of).tail(60) if "date" in b.columns else b.tail(60)
        if t.is_empty():
            continue
        rows.append((sym, float((t["close"] * t["volume"]).median())))
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:SCAN_POOL_SIZE]]


DIRECTION_SIDE = {"↑ 看多": "long", "↓ 看空": "short"}


def signals_by_side(bars: dict, as_of: date, prev_day: date) -> dict:
    """一次扫描同时给出多/空信号。多: “多头条件齐备” + long_trail (须低于收盘); 空: “空头条件齐备” + short_trail (须高于收盘)。
    保护位不可用 (吊灯已越过收盘) 时与原 _protection_for 一致视为无保护位 → 只用统一纪律 2ATR。
    另给 "state": {symbol: "long"|"short"|None} = 可见窗内最后一根已完成 4h 的方向状态 (tight-v1 机会板 4h 门用)。"""
    from app.services import phantom_4h
    out: dict = {"long": [], "short": [], "state": {}}
    lo = datetime.combine(prev_day, datetime.min.time()).replace(hour=13, minute=30)
    hi = datetime.combine(as_of, datetime.min.time()).replace(hour=9, minute=30)
    for sym, df in bars.items():
        if len(df) < phantom_4h.MIN_BARS:
            continue
        s = phantom_4h.scan_series(df)
        upto = s[s.index <= hi]
        if len(upto) and "direction" in upto.columns:
            out["state"][sym] = DIRECTION_SIDE.get(str(upto["direction"].iloc[-1]))
        win = s[(s.index >= lo) & (s.index <= hi)]
        for side, action, col in (("long", "多头条件齐备", "long_trail"), ("short", "空头条件齐备", "short_trail")):
            w = win[win["action"] == action]
            if w.empty:
                continue
            r = w.iloc[-1]
            close, trail = float(r["close"]), float(r[col])
            if side == "long":
                stop = trail / close - 1 if np.isfinite(trail) and 0 < trail < close else None
            else:
                stop = trail / close - 1 if np.isfinite(trail) and trail > close > 0 else None
            out[side].append({"symbol": sym, "side": side, "score": float(r["score"]), "bar": w.index[-1].isoformat(),
                              "trigger": r["trigger"], "close_4h": close,
                              "chandelier": trail if stop is not None else None, "stop_loss": stop})
    for k in ("long", "short"):
        out[k].sort(key=lambda x: -x["score"])
    return out


def pine_tiebreak(sym: str, day: date) -> float:
    """Pine 仪表盘没有排序分: 同日多只时用 (代码, 日期) 的确定性伪随机数排序 (与回测同一规则, 不引入额外因子倾向)。"""
    import random
    return random.Random(f"{sym}{day.isoformat()}").random()


def pine_signals(bars: dict, as_of: date, prev_day: date, daily: dict) -> tuple[list[dict], int]:
    """Pine 原版仪表盘 (phantom_4h_pine.dashboard_series) 在扫描可见窗 [前日13:30, 当日09:30] 内出现“积极做多”的股票。
    daily: {symbol: 日K polars (date, close)} 供 L5 日线趋势灯。返回 (信号, 历史不足而跳过的股票数)。"""
    import pandas as pd

    from app.services import phantom_4h_pine as pp
    lo = datetime.combine(prev_day, datetime.min.time()).replace(hour=13, minute=30)
    hi = datetime.combine(as_of, datetime.min.time()).replace(hour=9, minute=30)
    out, short_hist = [], 0
    for sym, df in bars.items():
        if len(df) < pp.MIN_BARS:
            short_hist += 1
            continue
        d = daily.get(sym)
        ok = None
        if d is not None and not d.is_empty():
            dd_ = d.select(["date", "close"]).to_pandas().set_index("date")
            dd_ = dd_[pd.to_datetime(dd_.index) <= pd.Timestamp(as_of)]
            ok = pp.daily_trend_ok(dd_)
        s = pp.dashboard_series(df, ok)
        w = s[(s.index >= lo) & (s.index <= hi) & s["enter"]]
        if w.empty:
            continue
        r = w.iloc[-1]
        close, trail = float(r["close"]), float(r["long_trail"])
        stop = trail / close - 1 if np.isfinite(trail) and 0 < trail < close else None
        out.append({"symbol": sym, "side": "long", "score": pine_tiebreak(sym, as_of), "lights": int(r["score"]),
                    "bar": w.index[-1].isoformat(), "close_4h": close, "chandelier": trail if stop is not None else None,
                    "stop_loss": stop})
    out.sort(key=lambda x: -x["score"])
    return out, short_hist


def signals_for_day(bars: dict, as_of: date, prev_day: date) -> list[dict]:
    return signals_by_side(bars, as_of, prev_day)["long"]


def run(repo, provider, as_of: date, frames: dict, *, picks: int = 10, include: set[str] | None = None) -> dict:
    """返回 {"picks": [symbol…], "stops": {symbol: stop_loss}, "meta": {...}}; 任何失败只降级 (返回空)。

    include (2026-10-05 自选独立选股): 额外计算的标的 (须在流动性池 frames 内)。它们只进 hits/stops,
    不参与全池前 10 的竞争 —— 全池 picks 仍只从成交额前 200 里取, 与评估口径一致。
    """
    from app.services import kline_4h
    t0 = time.perf_counter()
    data_dir = Path(repo.store.data_dir)
    base = pool(repo, as_of, frames)
    base_set = set(base)
    extra = sorted(s for s in (include or set()) if s in frames and s not in base_set)
    syms = base + extra
    meta: dict = {"pool": len(base), "extra": len(extra)}
    try:
        if provider is not None:
            # 0.22.5: 数据层覆盖全部自选 (含上市不足 min_history_days、不在扫描池的新股), 只补 30m 数据;
            # 信号仍只对 syms 计算, 出卡口径不变。
            data_syms = syms + sorted(s for s in (include or set()) if s not in set(syms))
            meta["refresh"] = kline_4h.refresh_recent(data_dir, repo, provider, data_syms, as_of)
            meta["history"] = kline_4h.ensure_history(data_dir, repo, provider, syms, as_of)
            meta["data_only"] = len(data_syms) - len(syms)
    except Exception as e:  # noqa: BLE001
        meta["refresh_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("phantom_4h refresh failed (使用已有 4h 数据): %s", meta["refresh_error"])
    days = kline_4h.trading_days(repo, as_of - timedelta(days=10), as_of)
    if not days or days[-1] != as_of or len(days) < 2:
        meta["skipped"] = "as_of 不是已知交易日"
        return {"picks": [], "stops": {}, "meta": meta}
    bars = kline_4h.bars_4h(data_dir, repo, syms, as_of - timedelta(days=HISTORY_DAYS * 7 // 5), as_of + timedelta(days=1),
                            frames=frames)
    sig = signals_by_side(bars, as_of, days[-2])
    chosen = [x for x in sig["long"] if x["symbol"] in base_set][:picks]
    chosen_s = [x for x in sig["short"] if x["symbol"] in base_set][:picks]
    meta.update({"with_4h": len(bars), "ready": len(sig["long"]), "ready_short": len(sig["short"]),
                 "elapsed_s": round(time.perf_counter() - t0, 1), "detail": chosen, "detail_short": chosen_s})

    def _hits(rows):
        return [{"symbol": x["symbol"], "score": round(float(x["score"]), 6), "rank": i + 1} for i, x in enumerate(rows)]
    # 止损给到: 全池前 10 + 全部自选 (自选可能就在成交额前 200 里但排在第 10 名之后, 也要带吊灯止损)
    want = {c["symbol"] for c in chosen} | set(include or ())
    want_s = {c["symbol"] for c in chosen_s} | set(include or ())
    stops = {x["symbol"]: x["stop_loss"] for x in sig["long"] if x["symbol"] in want}
    short_stops = {x["symbol"]: x["stop_loss"] for x in sig["short"] if x["symbol"] in want_s}
    out = {"picks": [x["symbol"] for x in chosen], "stops": stops,
           "short_picks": [x["symbol"] for x in chosen_s], "short_stops": short_stops,
           "hits": _hits(sig["long"]), "short_hits": _hits(sig["short"]), "meta": meta,
           # tight-v1: 自选的 4h 方向状态 (只给自选, 机会板 4h 门 = 齐备事件 或 同向状态)
           "state": {s: v for s, v in (sig.get("state") or {}).items() if s in set(include or ())}}
    # Pine 原版: 独立加载更长 4h 历史 (不改上面 phantom_4h 的输入), 失败只降级为空
    try:
        t1 = time.perf_counter()
        bars_p = kline_4h.bars_4h(data_dir, repo, syms, as_of - timedelta(days=PINE_HISTORY_DAYS), as_of + timedelta(days=1),
                                  frames=frames)
        sig_p, short_hist = pine_signals(bars_p, as_of, days[-2], frames)
        chosen_p = [x for x in sig_p if x["symbol"] in base_set][:picks]
        want_p = {c["symbol"] for c in chosen_p} | set(include or ())
        out.update({"pine_picks": [x["symbol"] for x in chosen_p],
                    "pine_stops": {x["symbol"]: x["stop_loss"] for x in sig_p if x["symbol"] in want_p},
                    "pine_hits": _hits(sig_p)})
        meta["pine"] = {"ready": len(sig_p), "short_history": short_hist, "elapsed_s": round(time.perf_counter() - t1, 1),
                        "detail": chosen_p}
    except Exception as e:  # noqa: BLE001
        logger.warning("phantom_4h_pine failed (跳过该来源): %s", type(e).__name__)
        meta["pine"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    return out
