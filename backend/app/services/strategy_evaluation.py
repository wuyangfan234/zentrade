"""统一口径策略评估 (无限 2026-10-02 确认: 所有来源按同一 7 日纪律卖出; 回测标的 = 前 60 日成交额前 1000,
报告分 前100 / 101–1000 / 自选 三栏)。

日K近似 (生产用分钟级, 这里做来源之间的公平比较):
- 入场: 信号日次日起 5 个交易日入场窗; 开盘 > 入场区上沿 2% 视为跳空不追 (当天作废);
  开盘在区内按开盘价成交, 开盘低于区间且当日最高触及下沿按下沿成交, 开盘高于上沿 (≤2%) 且最低触及上沿按上沿成交。
- 持仓: 第 1 天=成交日。当日最高触及目标区下沿 → 按目标下沿全部卖出 (生产先卖一半, 此处取保守口径);
  收盘跌破止损 (初始=失效位; 浮盈达 1R 上移保本, 达 2R 上移到 +1R) → 次交易日开盘卖出;
  第 7 天收盘无条件卖出。
- 成本: 双边 5 bps 滑点 + 每股 $0.005 (单边最低 $1); R 以单笔 1% 风险 ($100,000 本金) 折算。
- 数据不够走完 → open_at_end, 不计入统计。
"""
from __future__ import annotations

import copy
import json
import logging
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SLIP = 0.0005
GAP_LIMIT = 1.02
ENTRY_WINDOW = 5
MAX_HOLD = 7
EQUITY = 100_000.0
RISK_PCT = 0.01
NOTIONAL_CAP = 0.25
TOP_N = 1000
TOP_SMALL = 100
PICKS_PER_DAY = 10


def FEE_R(risk: float, entry: float) -> float:  # noqa: N802 - mirrors a constant-like helper
    qty = math.floor(min(EQUITY * RISK_PCT / risk, EQUITY * NOTIONAL_CAP / entry)) if risk > 0 and entry > 0 else 0
    if qty <= 0:
        return 0.0
    fee = 2 * max(qty * 0.005, 1.0)
    return fee / (qty * risk)


def simulate_7d(o, h, l, c, *, entry_zone, invalidation, target, risk, side: str = "long") -> dict:
    """o/h/l/c: 信号日之后的日K (index 0 = 信号次日)。返回 {status, r, exit_reason, days, entry_price, exit_price}。

    side="short" (2026-10-03): 逐条镜像 — 开盘低于入场下沿 2% 不追; 卖空价 ×(1−滑点)、回补价 ×(1+滑点);
    当日最低触及目标近端 (target[1]) 按近端回补; 收盘站上止损 → 次日开盘回补; 浮盈 1R 保本、2R 锁 1R (止损只下移);
    第 7 日收盘回补。(不能用“价格取负”复用做多逻辑: 跳空 ×1.02 与滑点 ×(1±s) 是乘法, 取负后方向会反。)
    """
    if side == "short":
        return _simulate_short(o, h, l, c, entry_zone=entry_zone, invalidation=invalidation, target=target, risk=risk)
    return _simulate_long(o, h, l, c, entry_zone=entry_zone, invalidation=invalidation, target=target, risk=risk)


def _simulate_short(o, h, l, c, *, entry_zone, invalidation, target, risk) -> dict:
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry, gap_up_fill = None, None, False
    for i in range(min(ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(l[i])):
            continue
        if op < lo * (2 - GAP_LIMIT):
            return {"status": "gap_expired", "r": None}
        if lo <= op <= hi:
            px = op
        elif op > hi and l[i] <= hi:
            px = hi
        elif op < lo and h[i] >= lo:
            px, gap_up_fill = lo, True
        else:
            continue
        fill_i, entry = i, px * (1 - SLIP)
        break
    if fill_i is None:
        return {"status": "no_fill", "r": None}
    stop = invalidation
    trough = entry
    near = target[1]  # 目标近端 (先触及)
    for k in range(MAX_HOLD):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return {"status": "open_at_end", "r": None, "entry_price": entry}
        # 开盘低于入场区、反弹到下沿才成交 (gap_up_fill): 成交日最低可能在成交之前, 只计 min(入场, 收盘)
        # —— 与多头 simulate_native_daily 的 gap_down_fill 镜像 (BH-02)
        low = min(entry, c[i]) if (k == 0 and gap_up_fill) else l[i]
        trough = min(trough, low)
        if low <= near:
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] <= near else near
            return _close_short(entry, px, risk, "target_zone_lower", k + 1)
        if k == MAX_HOLD - 1:
            return _close_short(entry, c[i], risk, "time_stop", k + 1)
        if trough <= entry - 2 * risk:
            stop = min(stop, entry - risk)
        elif trough <= entry - risk:
            stop = min(stop, entry)
        if c[i] > stop:
            j = i + 1
            if j >= n or not np.isfinite(o[j]):
                return {"status": "open_at_end", "r": None, "entry_price": entry}
            reason = "invalidation" if stop == invalidation else "trailing_stop"
            return _close_short(entry, o[j], risk, reason, k + 2)
    return {"status": "open_at_end", "r": None, "entry_price": entry}


def _close_short(entry: float, raw_exit: float, risk: float, reason: str, days: int) -> dict:
    exit_px = raw_exit * (1 + SLIP)
    r = (entry - exit_px) / risk - FEE_R(risk, entry)
    return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": days,
            "entry_price": round(float(entry), 4), "exit_price": round(float(exit_px), 4)}


def _simulate_long(o, h, l, c, *, entry_zone, invalidation, target, risk) -> dict:
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry = None, None
    for i in range(min(ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(l[i])):
            continue
        if op > hi * GAP_LIMIT:
            return {"status": "gap_expired", "r": None}
        if lo <= op <= hi:
            px = op
        elif op < lo and h[i] >= lo:
            px = lo
        elif op > hi and l[i] <= hi:
            px = hi
        else:
            continue
        fill_i, entry = i, px * (1 + SLIP)
        break
    if fill_i is None:
        return {"status": "no_fill", "r": None}
    stop = invalidation
    peak = entry
    for k in range(MAX_HOLD):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return {"status": "open_at_end", "r": None, "entry_price": entry}
        peak = max(peak, h[i])
        if h[i] >= target[0]:
            # 成交日 (k=0) 只能在入场之后触及; 之后的交易日若开盘已越过目标, 按开盘价成交 (生产按真实报价卖出)
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] >= target[0] else target[0]
            return _close(entry, px, risk, "target_zone_lower", k + 1)
        if k == MAX_HOLD - 1:
            return _close(entry, c[i], risk, "time_stop", k + 1)
        if peak >= entry + 2 * risk:
            stop = max(stop, entry + risk)
        elif peak >= entry + risk:
            stop = max(stop, entry)
        if c[i] < stop:
            j = i + 1
            if j >= n or not np.isfinite(o[j]):
                return {"status": "open_at_end", "r": None, "entry_price": entry}
            reason = "invalidation" if stop == invalidation else "trailing_stop"
            return _close(entry, o[j], risk, reason, k + 2)
    return {"status": "open_at_end", "r": None, "entry_price": entry}


# ── v2 口径 (2026-10-05, 无限拍板“旧成绩和新成绩并行核对, 确认后再切换”) ────────────────────────
# 与生产交易员对齐的日线近似: 目标近端只卖一半 (余仓止损抬到保本), 远端清仓; 成交日若是“开盘高于入场区、
# 回落成交”, 当日最高价可能发生在成交之前, 不计入目标/移动止损; 交易记录分列信号日/成交日/退出日。
# 仍是日线近似: 生产 09:45 后才入场、两帧成交、第 7 日 15:30 退出, 日线无法逐分钟还原 —— 报告中照实标注。
CONTRACT_V2 = "v2-native-daily-2026-10-05"


def simulate_native_daily(o, h, l, c, *, entry_zone, invalidation, target, risk, last_index: int | None = None) -> dict:
    """多头 v2: 返回 {status, r, exit_reason, days, fill_index, exit_index, legs}。r = 两条腿合计 / 计划风险。

    last_index (v3, 2026-10-05): 已退市股票最后一根有效日K在 o/h/l/c 中的下标; 持仓到那天仍未离场 →
    按当日收盘平仓 (delisted_exit)。默认 None = 与 v2 完全相同。"""
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry, gap_down_fill = None, None, False
    for i in range(min(ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(l[i])):
            continue
        if op > hi * GAP_LIMIT:
            return {"status": "gap_expired", "r": None}
        if lo <= op <= hi:
            px = op
        elif op < lo and h[i] >= lo:
            px = lo
        elif op > hi and l[i] <= hi:
            px, gap_down_fill = hi, True
        else:
            continue
        fill_i, entry = i, px * (1 + SLIP)
        break
    if fill_i is None:
        return {"status": "no_fill", "r": None}
    near, far = target[0], target[1]
    stop, peak = invalidation, entry
    half_done, legs = False, []

    def done(reason, raw_exit, k, idx):
        exit_px = raw_exit * (1 - SLIP)
        w = 0.5 if half_done else 1.0
        legs.append({"reason": reason, "weight": w, "price": round(float(exit_px), 4), "day": k + 1})
        r = sum(x["weight"] * (x["price"] - entry) for x in legs) / risk - FEE_R(risk, entry) * (1.5 if half_done else 1.0)
        return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": k + 1,
                "fill_index": fill_i, "exit_index": idx, "entry_price": round(float(entry), 4), "legs": legs,
                "partial": half_done}

    for k in range(MAX_HOLD):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return {"status": "open_at_end", "r": None, "fill_index": fill_i}
        high = max(entry, c[i]) if (k == 0 and gap_down_fill) else h[i]
        if k > 0 and np.isfinite(o[i]) and o[i] >= far:              # 开盘跳过远端: 全部按开盘价
            return done("target_zone_upper", o[i], k, i)
        if not half_done and high >= near:
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] >= near else near
            legs.append({"reason": "target_zone_lower", "weight": 0.5, "price": round(float(px * (1 - SLIP)), 4), "day": k + 1})
            half_done = True
            stop = max(stop, entry)
        if half_done and high >= far:
            return done("target_zone_upper", far, k, i)
        if k == MAX_HOLD - 1:
            return done("time_stop", c[i], k, i)
        peak = max(peak, high)
        if peak >= entry + 2 * risk:
            stop = max(stop, entry + risk)
        elif peak >= entry + risk:
            stop = max(stop, entry)
        if c[i] < stop:
            j = i + 1
            if j >= n or not np.isfinite(o[j]):
                if last_index is not None and i >= last_index:
                    return done("delisted_exit", c[i], k, i)
                return {"status": "open_at_end", "r": None, "fill_index": fill_i}
            reason = "invalidation" if stop == invalidation else ("breakeven_after_partial" if half_done and stop == entry
                                                                  else "trailing_stop")
            return done(reason, o[j], k + 1, j)
        if last_index is not None and i >= last_index:
            return done("delisted_exit", c[i], k, i)
    return {"status": "open_at_end", "r": None, "fill_index": fill_i}



def simulate_native_daily_short(o, h, l, c, *, entry_zone, invalidation, target, risk,
                                last_index: int | None = None) -> dict:
    """空头 v2 (BH-03): simulate_native_daily 的逐条镜像, 与交易员空头规则一致 —— 最低触及目标近端 (target[1])
    回补一半 (余仓止损下移到保本), 远端 (target[0]) 回补余仓; 开盘低于入场区、反弹到下沿成交 (gap_up_fill) 时,
    成交日最低只计 min(入场, 收盘)。卖空价 ×(1−滑点)、回补价 ×(1+滑点)。返回结构同 simulate_native_daily。
    看空卡回放 card_replay.replay_state_short 与本函数同一规则, 由测试锁定。"""
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry, gap_up_fill = None, None, False
    for i in range(min(ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(l[i])):
            continue
        if op < lo * (2 - GAP_LIMIT):
            return {"status": "gap_expired", "r": None}
        if lo <= op <= hi:
            px = op
        elif op > hi and l[i] <= hi:
            px = hi
        elif op < lo and h[i] >= lo:
            px, gap_up_fill = lo, True
        else:
            continue
        fill_i, entry = i, px * (1 - SLIP)
        break
    if fill_i is None:
        return {"status": "no_fill", "r": None}
    near, far = target[1], target[0]
    stop, trough = invalidation, entry
    half_done, legs = False, []

    def done(reason, raw_exit, k, idx):
        exit_px = raw_exit * (1 + SLIP)
        w = 0.5 if half_done else 1.0
        legs.append({"reason": reason, "weight": w, "price": round(float(exit_px), 4), "day": k + 1})
        r = sum(x["weight"] * (entry - x["price"]) for x in legs) / risk - FEE_R(risk, entry) * (1.5 if half_done else 1.0)
        return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": k + 1,
                "fill_index": fill_i, "exit_index": idx, "entry_price": round(float(entry), 4), "legs": legs,
                "partial": half_done}

    for k in range(MAX_HOLD):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return {"status": "open_at_end", "r": None, "fill_index": fill_i}
        low = min(entry, c[i]) if (k == 0 and gap_up_fill) else l[i]
        if k > 0 and np.isfinite(o[i]) and o[i] <= far:              # 开盘跳过远端: 全部按开盘价
            return done("target_zone_upper", o[i], k, i)
        if not half_done and low <= near:
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] <= near else near
            legs.append({"reason": "target_zone_lower", "weight": 0.5, "price": round(float(px * (1 + SLIP)), 4), "day": k + 1})
            half_done = True
            stop = min(stop, entry)
        if half_done and low <= far:
            return done("target_zone_upper", far, k, i)
        if k == MAX_HOLD - 1:
            return done("time_stop", c[i], k, i)
        trough = min(trough, low)
        if trough <= entry - 2 * risk:
            stop = min(stop, entry - risk)
        elif trough <= entry - risk:
            stop = min(stop, entry)
        if c[i] > stop:
            j = i + 1
            if j >= n or not np.isfinite(o[j]):
                if last_index is not None and i >= last_index:
                    return done("delisted_exit", c[i], k, i)
                return {"status": "open_at_end", "r": None, "fill_index": fill_i}
            reason = "invalidation" if stop == invalidation else ("breakeven_after_partial" if half_done and stop == entry
                                                                  else "trailing_stop")
            return done(reason, o[j], k + 1, j)
        if last_index is not None and i >= last_index:
            return done("delisted_exit", c[i], k, i)
    return {"status": "open_at_end", "r": None, "fill_index": fill_i}

def _close(entry: float, raw_exit: float, risk: float, reason: str, days: int) -> dict:
    exit_px = raw_exit * (1 - SLIP)
    r = (exit_px - entry) / risk - FEE_R(risk, entry)
    return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": days,
            "entry_price": round(float(entry), 4), "exit_price": round(float(exit_px), 4)}


def prior_dollar_volume_rank(dv: np.ndarray, window: int = 60) -> np.ndarray:
    """dv: (T, N) 每日成交额。返回 (T, N) 名次 (1=最大), 只用 t 之前 window 天的中位数; 不足/缺失为 0。"""
    T, N = dv.shape
    out = np.zeros((T, N), dtype=np.int32)
    for t in range(window, T):
        med = np.nanmedian(dv[t - window:t], axis=0)
        valid = np.isfinite(med) & (med > 0)
        if not valid.any():
            continue
        order = np.argsort(-np.where(valid, med, -np.inf), kind="stable")
        ranks = np.empty(N, dtype=np.int32)
        ranks[order] = np.arange(1, N + 1)
        out[t] = np.where(valid, ranks, 0)
    return out


def _cluster_ci(rows: list[dict], draws: int = 1000, seed: int = 7) -> tuple[float | None, float | None]:
    by_day: dict = defaultdict(list)
    for r in rows:
        by_day[r["entry_day"]].append(r["r"])
    days = list(by_day.values())
    if len(days) < 2:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [x for _ in days for x in rng.choice(days)]
        means.append(statistics.fmean(sample))
    means.sort()
    return round(means[int(0.025 * draws)], 4), round(means[int(0.975 * draws) - 1], 4)


def _stats(rows: list[dict], seed: int) -> dict:
    if not rows:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "win_rate": None, "days": 0}
    rs = [r["r"] for r in rows]
    lo, hi = _cluster_ci(rows, seed=seed)
    wins = [x for x in rs if x > 0]
    losses = [-x for x in rs if x < 0]
    return {"n": len(rs), "mean": round(statistics.fmean(rs), 4), "ci_low": lo, "ci_high": hi,
            "win_rate": round(len(wins) / len(rs), 4), "days": len({r["entry_day"] for r in rows}),
            "profit_factor": round(sum(wins) / sum(losses), 3) if losses else None}


def summarize(trades: list[dict], seed: int = 7) -> dict:
    """trades: [{r, entry_day, bucket: 'top100'|'top1000', watchlist: bool}] (仅 closed)。"""
    out = _stats(trades, seed)
    out["by_bucket"] = {
        "top100": _stats([t for t in trades if t["bucket"] == "top100"], seed),
        "top1000": _stats([t for t in trades if t["bucket"] == "top1000"], seed),
        "watchlist": _stats([t for t in trades if t.get("watchlist")], seed),
    }
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[t.get("exit_reason") or "?"] += 1
    out["exit_reasons"] = dict(reasons)
    return out


@dataclass
class EvalWindow:
    start: date
    end: date


def evaluation_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "evaluation.json"


def evaluation_v2_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "evaluation_v2.json"


def evidence_mode_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "evidence_mode.json"


EVIDENCE_MODES = ("v1", "v2", "v3", "v4")


def evaluation_v3_path(data_dir: Path) -> Path:
    return Path(data_dir) / "decision_desk" / "evaluation_v3.json"


def evidence_mode(data_dir: Path) -> str:
    """许可/权重读哪一份证据: 默认 v1。v2/v3/v4 只经来源页带审计的切换生效。
    文件不存在 = v1。文件存在但损坏/非法: 若自选 v4 规则目录已存在 (曾切到 v4) → 按 v4 处理 (只交易自选, 更严),
    否则 v1 (沿用旧行为)。不让一个坏文件把“只交易自选”的限制静默解除。"""
    p = evidence_mode_path(data_dir)
    try:
        mode = json.loads(p.read_text(encoding="utf-8")).get("mode")
    except FileNotFoundError:
        return "v1"
    except (OSError, ValueError, AttributeError):
        mode = None
    if mode in EVIDENCE_MODES:
        return mode
    return "v4" if (Path(data_dir) / "rules").exists() else "v1"


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_mode_view_row(v) -> bool:
    """来源行是否是 load_evaluation 的「按模式解释后的视图」而非 v1 证据本体 (v4 许可行 / v3 叠加行 / v2 沿用标注)。
    审计 2026-10-26 M-9: 这类行不该出现在 v1 evaluation.json 里。"""
    return isinstance(v, dict) and (v.get("v4") is True or v.get("v3") is True or "carried_from" in v)


def load_evaluation(data_dir: Path) -> dict | None:
    """现行许可证据。v2 模式下: v2 已评估的来源用 v2; v2 未覆盖的 (S1/S2 分钟重放、4h Phantom) 沿用 v1 并标注。
    v3 模式下: 只认 v3.1 结论 (tier_v3) 叠加前向确认; v3 未覆盖的来源 = 尚未用新尺子评估 → 只展示 (fail-closed)。"""
    # v3/v4 不用 v1: 先判模式再读 1.9MB 的 v1 文件 (10-09 实测: v4 下每张卡每帧白读一次, 429 卡 ≈ 7s)
    mode = evidence_mode(data_dir)
    if mode == "v4":
        return _load_v4(data_dir)
    if mode == "v3":
        return _load_v3(data_dir)
    v1 = _read(evaluation_path(data_dir))
    if mode != "v2":
        return v1
    v2 = _read(evaluation_v2_path(data_dir))
    if not isinstance(v2, dict) or not isinstance(v2.get("sources"), dict):
        return v1  # 切换后 v2 文件缺失/损坏: 回到 v1, 不让来源因读不到而全部“缺证据”
    merged = dict(v2)
    sources = {k: {**v, "carried_from": "v1"} for k, v in ((v1 or {}).get("sources") or {}).items()
               if k not in v2["sources"] and isinstance(v, dict)}
    current = current_fingerprints(data_dir, [k for k, v in v2["sources"].items() if isinstance(v, dict) and v.get("fingerprint")])
    for k, v in v2["sources"].items():
        fp = (v or {}).get("fingerprint") if isinstance(v, dict) else None
        if fp and k in current and current[k] is not None and current[k] != fp.get("hash"):
            v = {**v, "stale_version": {"evaluated": fp.get("hash"), "current": current[k]}}
        sources[k] = v
    merged["sources"] = sources
    return merged


_V3_CACHE: dict = {}
_V3_CACHE_LOCK = __import__("threading").Lock()
V3_CACHE_MAX_AGE_S = 300   # 指纹 (策略源码/覆盖配置) 变化最多 5 分钟后被发现; 证据/影子文件变化立即重算


def _file_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load_v3(data_dir: Path) -> dict:
    """带缓存的 v3 证据: 盘中循环每 15 秒对每张卡都会问一次许可, 不能每次重读 1.7MB 证据 + 影子账本
    (实测 276ms/次 × 260 张卡)。缓存键 = 证据文件与影子账本的 (mtime, size) + 5 分钟上限; 任一文件变化立即重算。"""
    from app.services import shadow_forward
    key = (str(Path(data_dir).resolve()), _file_sig(evaluation_v3_path(data_dir)),
           _file_sig(shadow_forward.path(data_dir)))
    now = __import__("time").monotonic()
    with _V3_CACHE_LOCK:
        hit = _V3_CACHE.get(key[0])
        if hit and hit[0] == key and now - hit[1] < V3_CACHE_MAX_AGE_S:
            return _shallow(hit[2])
    out = _load_v3_uncached(data_dir)
    with _V3_CACHE_LOCK:
        _V3_CACHE[key[0]] = (key, now, out)
    return _shallow(out)


_V4_CACHE: dict = {}


def _load_v4(data_dir: Path) -> dict:
    """v4 (2026-10-06, 自选机会): 每条规则 = {v4, enabled, tradable, forward}; tradable = 在当前规则集里勾选
    且规则集通过自选前向确认 (同票随机日零点)。只认自选 (entry_guard 另拦非自选)。按文件签名缓存 (盘中每帧调用)。"""
    from app.services import watchlist_v4 as wv
    rd = wv.rules_dir(data_dir)
    vers = tuple(wv.versions(data_dir))
    key = (str(Path(data_dir).resolve()), _file_sig(wv.path(data_dir)), _file_sig(rd / "current.json"), vers,
           tuple(_file_sig(rd / f"R{v}.json") for v in vers), _file_sig(evidence_mode_path(data_dir)))
    now = __import__("time").monotonic()
    with _V3_CACHE_LOCK:
        hit = _V4_CACHE.get(key[0])
        if hit and hit[0] == key and now - hit[1] < V3_CACHE_MAX_AGE_S:
            return copy.deepcopy(hit[2])
    pm = wv.permission(data_dir)           # 唯一许可出处; 内部任何异常/非法 → 空集
    rs, st, tradable, risk = pm["ruleset"], pm["status"], pm["tradable"], pm["risk_pct"]
    enabled = set(x for x in rs.get("enabled") or [] if isinstance(x, str))
    sources = {}
    for rule in enabled | set(st.get("per_rule") or {}):
        sources[rule] = {"v4": True, "enabled": rule in enabled, "tradable": rule in tradable,
                         "risk_pct": risk if rule in tradable else 0.0, "forward": (st.get("per_rule") or {}).get(rule),
                         "ruleset": rs.get("version")}
    out = {"mode": "v4", "ruleset": rs, "forward": {k: st.get(k) for k in ("n", "months", "excess", "abs_mean", "p",
                                                                          "threshold", "promoted")},
           "sources": sources}
    with _V3_CACHE_LOCK:
        _V4_CACHE[key[0]] = (key, now, out)
    return copy.deepcopy(out)


def _shallow(ev: dict) -> dict:
    """调用方可能改动返回值: 给出顶层 + 每个来源的浅拷贝, 缓存本体不被污染。"""
    return {**ev, "sources": {k: dict(v) for k, v in (ev.get("sources") or {}).items()}}


def _load_v3_uncached(data_dir: Path) -> dict:
    """v3 模式证据: 每个来源 = {n, mean, ci_low…, tier_v3, reason_v3, version 校验, forward 前向确认}。
    文件缺失/损坏 → 空来源表 (全部只展示), 不回退到旧口径 —— 切到 v3 就是明确不再用旧尺子开仓。"""
    v3 = _read(evaluation_v3_path(data_dir))
    if not isinstance(v3, dict) or not isinstance(v3.get("sources"), dict):
        return {"contract": "v3-missing", "sources": {}, "v3_missing": True}
    try:
        from app.services import forward_confirmation
        fwd = forward_confirmation.status(data_dir, v3)
    except Exception:  # noqa: BLE001 - 前向确认读失败 = 没有任何来源晋级 (fail-closed), 不让许可链抛错
        fwd = {}
    merged = {k: v3.get(k) for k in ("contract", "window", "generated_at", "holdout_start", "cluster_unit",
                                     "calibration", "with_delisted")}
    fps = {k: (v.get("fingerprint") if isinstance(v.get("fingerprint"), dict) else {})
           for k, v in v3["sources"].items() if isinstance(v, dict)}
    current = current_fingerprints(data_dir, [k for k, fp in fps.items() if fp.get("hash")], contract=v3.get("contract"))
    sources = {}
    survivor_only = v3.get("with_delisted") is False
    for k, v in v3["sources"].items():
        if not isinstance(v, dict):
            continue
        row = {**v, "v3": True, "forward": fwd.get(k)}
        if survivor_only and row.get("tier_v3") == "proven":
            # 预登记第 3 节: 证据必须含已退市股票; 研究数据缺失时算出的“通过”不放行 (前向确认不受影响)
            row["tier_v3"] = "negative"
            row["reason_v3"] = (row.get("reason_v3") or "") + "；本次评估未含已退市股票（研究数据缺失），历史证据不放行"
        fp = fps.get(k) or {}
        if fp.get("hash") and current.get(k) is not None and current[k] != fp["hash"]:
            row["stale_version"] = {"evaluated": fp["hash"], "current": current[k]}
        sources[k] = row
    merged["sources"] = sources
    return merged


def current_fingerprints(data_dir: Path, sids: list[str], contract: str | None = None) -> dict[str, str | None]:
    """当前运行中策略的版本指纹 (算不出 = None, 不据此拦截)。"""
    if not sids:
        return {}
    try:
        from app.jobs.daily_pipeline import _get_app_state
        engine = getattr(_get_app_state(), "strategy_engine", None)
    except Exception:  # noqa: BLE001
        engine = None
    if engine is None:
        return {s: None for s in sids}
    from app.services.strategy_evaluation_runner import strategy_fingerprint
    out = {}
    for sid in sids:
        try:
            out[sid] = strategy_fingerprint(engine, sid, contract=contract)["hash"]
        except Exception:  # noqa: BLE001
            out[sid] = None
    return out


def load_evaluation_file(data_dir: Path, version: str) -> dict | None:
    return _read({"v2": evaluation_v2_path, "v3": evaluation_v3_path}.get(version, evaluation_path)(data_dir))


def save_evaluation(data_dir: Path, payload: dict) -> Path:
    p = evaluation_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p
