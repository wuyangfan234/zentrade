"""大盘 regime 三态 (P1-A): 进攻 / 震荡 / 防守。

两层输入 + 滞回:

- 慢层 (盘后 1 次, 随 decision_scan_job 盘后扫描): ① SPY 收盘相对 MA50; ② MA50 的 20 日斜率符号;
  ③ 自选宽度 = 收盘 > MA20 的自选占比。
- 快层 (盘中, 挂在已有的 09:25 + 10:00-15:00 整点大盘重判任务上, 不另起定时器): ① SPY 现价相对 10 日 EMA;
  ② 是否突破前 20 日高 / 跌破前 20 日低; ③ 5 日 / 60 日已实现波动比。盘后慢层评估时用收盘价再读一次快层。
- 滞回: 降级只需任一层单次触发 (宁可早防守); 升级要求「慢层同意」或「快层连续 2 个整点 (同一交易日) 确认」,
  且盘后升级不越过同时刻快层读数。
- 最小驻留: 切换后至少驻留 3 个交易日才允许再次升级; 降级不受驻留限制。

状态与审计:
- ``data/rules/regime_state.json``: 当前状态 (每次评估覆盖写)。
- ``data/rules/regime_audit.jsonl``: 只追加, 每次状态切换一行 (时间、触发层、前/后状态、触发条件、依据数值)。

作用面 (swing_desk.board_for_day): 盘后快照里的 ``regime_state``, 再与同一交易日盘中整点写入的状态文件取较低者
(盘中只许降、不许升, 前一交易日的状态文件不影响当日板) → 每侧是否开放 + 每侧卡数上限; 空头只在「防守」开,
且空头卡一律观察级 (不进纸面下单簿)。多头上限可由 ``rules/swing.json`` 的 ``regime_limits`` 配置
(缺省 进攻 5 / 震荡 5 / 防守 0)。``"regime": false`` = 回到 tight-v1 (空头只看 SPY < MA50, 每边 5 张),
且 regime 不再评估、不写状态与审计; 无需改代码。

纯函数 (slow_target / fast_target / step) 不读文件、不出网, 便于单测与历史回放。
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

ATTACK, RANGE, DEFENSE = "进攻", "震荡", "防守"
STATES = (DEFENSE, RANGE, ATTACK)           # 按“风险偏好”升序
RANK = {s: i for i, s in enumerate(STATES)}
DEFAULT_STATE = RANGE                       # 冷启动 (没有历史状态) 从震荡起

INDEX = "SPY"
SLOW_MA = 50
SLOW_SLOPE_DAYS = 20
BREADTH_MA = 20
BREADTH_ATTACK = 0.55                       # 宽度 ≥ 55% 才算“多数在 MA20 上”
BREADTH_DEFENSE = 0.40                      # 宽度 < 40% = 宽度转弱
FAST_EMA = 10
FAST_RANGE_DAYS = 20
VOL_SHORT, VOL_LONG = 5, 60
VOL_RATIO_HOT = 1.5                         # 短波动 ≥ 1.5 倍长波动 = 波动放大
VOL_RATIO_CALM = 1.2
UP_CONFIRM_SLOTS = 2                        # 快层升级要连续 2 个整点确认
MIN_DWELL_DAYS = 3                          # 切换后至少驻留 3 个交易日才允许再升级 (降级不受限)

# 每侧卡数上限 (多头, 缺省; 可由 swing.json 的 regime_limits 覆盖); 空头只在防守开, 上限沿用每边 5 张
LONG_CAP = {ATTACK: 5, RANGE: 5, DEFENSE: 0}
SHORT_CAP = {ATTACK: 0, RANGE: 0, DEFENSE: 5}
LIMIT_ALIASES = {"attack": ATTACK, "range": RANGE, "defense": DEFENSE}

STATE_FILE = "regime_state.json"
AUDIT_FILE = "regime_audit.jsonl"
CONTRACT = "regime-v1"

_LOCK = threading.Lock()


# ── 配置开关 ──────────────────────────────────────────────────────────

def _swing_cfg(data_dir: Path | None) -> dict | None:
    if data_dir is None:
        return None
    try:
        raw = json.loads((Path(data_dir) / "rules" / "swing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def long_limits(data_dir: Path | None) -> dict[str, int]:
    """多头每态上限: swing.json 的 ``regime_limits`` ({"进攻": n, "震荡": n, "防守": n}, 也认 attack/range/defense);
    缺省 / 坏值按 LONG_CAP。上限再被机会板的每边 5 张封顶。"""
    out = dict(LONG_CAP)
    raw = (_swing_cfg(data_dir) or {}).get("regime_limits")
    if isinstance(raw, dict):
        for k, v in raw.items():
            st = LIMIT_ALIASES.get(k, k)
            if st in RANK and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[st] = v
    return out


def enabled(data_dir: Path | None) -> bool:
    """``rules/swing.json`` 的 ``"regime"``: 缺省 = 开 (直接生效); ``false`` / ``{"enabled": false}`` = 回到 tight-v1。
    文件缺失/损坏 = 开 (regime 只会收紧: 空头更难开、多头卡更少)。"""
    if data_dir is None:
        return True
    try:
        raw = json.loads((Path(data_dir) / "rules" / "swing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(raw, dict) or "regime" not in raw:
        return True
    v = raw.get("regime")
    if isinstance(v, bool):
        return v
    if isinstance(v, dict) and isinstance(v.get("enabled"), bool):
        return v["enabled"]
    return True


def caps(state: str | None, data_dir: Path | None = None) -> dict[str, int] | None:
    """{"long": n, "short": n}; 未知状态 = None (调用方走 tight-v1 旧口径)。多头上限按 regime_limits 配置。"""
    if state not in RANK:
        return None
    return {"long": long_limits(data_dir)[state], "short": SHORT_CAP[state]}


def lower(a: str | None, b: str | None) -> str | None:
    """两态取更防守的那个; 任一未知 = 另一个。"""
    if a not in RANK:
        return b if b in RANK else None
    if b not in RANK:
        return a
    return a if RANK[a] <= RANK[b] else b


# ── 指标 (纯函数) ─────────────────────────────────────────────────────

def _finite(a) -> np.ndarray:
    x = np.asarray(a, dtype=float)
    return x[np.isfinite(x)]


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """简单均线, 前 n-1 位为 NaN。"""
    x = np.asarray(x, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(x.size, np.nan)
    if x.size < n:
        return out
    a = 2.0 / (n + 1)
    v = float(np.mean(x[:n]))
    out[n - 1] = v
    for i in range(n, x.size):
        v = v + a * (x[i] - v)
        out[i] = v
    return out


def realized_vol(closes: np.ndarray, n: int) -> float | None:
    c = _finite(closes)
    if c.size < n + 1 or (c <= 0).any():
        return None
    r = np.diff(np.log(c[-(n + 1):]))
    return float(np.std(r, ddof=1)) if r.size > 1 else None


def slow_values(spy_closes, breadth: float | None) -> dict | None:
    """慢层依据数值: SPY 收盘序列 (截至信号日, 含当天) + 自选宽度。数据不足 = None。"""
    c = _finite(spy_closes)
    if c.size < SLOW_MA + SLOW_SLOPE_DAYS:
        return None
    ma = sma(c, SLOW_MA)
    last, m, m_prev = float(c[-1]), float(ma[-1]), float(ma[-1 - SLOW_SLOPE_DAYS])
    return {"close": round(last, 4), "ma50": round(m, 4), "ma50_prev20": round(m_prev, 4),
            "above_ma50": last > m, "ma50_slope_up": m > m_prev,
            "breadth": None if breadth is None else round(float(breadth), 4)}


def slow_target(v: dict | None) -> tuple[str | None, list[str]]:
    """慢层目标态。进攻 = SPY 在 MA50 上 + MA50 上行 + 宽度 ≥ 55%;
    防守 = SPY 在 MA50 下, 且 (MA50 下行 或 宽度 < 40%); 其余 = 震荡。数据缺 = (None, [])。"""
    if not v:
        return None, []
    above, up, br = v["above_ma50"], v["ma50_slope_up"], v.get("breadth")
    reasons = [f"SPY {'>' if above else '<'} MA50", f"MA50 20日{'上行' if up else '下行'}"]
    if br is not None:
        reasons.append(f"自选宽度 {br:.0%}")
    if above and up and br is not None and br >= BREADTH_ATTACK:
        return ATTACK, reasons
    if not above and (not up or (br is not None and br < BREADTH_DEFENSE)):
        return DEFENSE, reasons
    return RANGE, reasons


def fast_values(spy_closes_prev, spy_highs_prev, spy_lows_prev, price: float) -> dict | None:
    """快层依据数值: 截至上一交易日 (不含当天) 的 SPY 日K + 当前价 (盘中现价或当日收盘)。数据不足 = None。"""
    c = _finite(spy_closes_prev)
    h, lo = _finite(spy_highs_prev), _finite(spy_lows_prev)
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p <= 0:
        return None
    if c.size < max(VOL_LONG + 1, FAST_EMA) or h.size < FAST_RANGE_DAYS or lo.size < FAST_RANGE_DAYS:
        return None
    e_prev = float(ema(c, FAST_EMA)[-1])
    e_now = e_prev + 2.0 / (FAST_EMA + 1) * (p - e_prev)
    hi20, lo20 = float(h[-FAST_RANGE_DAYS:].max()), float(lo[-FAST_RANGE_DAYS:].min())
    series = np.append(c, p)
    vs, vl = realized_vol(series, VOL_SHORT), realized_vol(series, VOL_LONG)
    vr = (vs / vl) if (vs is not None and vl) else None
    return {"price": round(p, 4), "ema10": round(e_now, 4), "above_ema10": p > e_now,
            "high20": round(hi20, 4), "low20": round(lo20, 4),
            "break_up": p > hi20, "break_down": p < lo20,
            "vol_ratio": None if vr is None else round(float(vr), 4)}


def fast_target(v: dict | None) -> tuple[str | None, list[str]]:
    """快层目标态。防守 = 跌破前 20 日低, 或 (EMA10 下 且 波动比 ≥ 1.5);
    进攻 = EMA10 上 且 未跌破 且 (突破前 20 日高 或 波动比 < 1.2); 其余 = 震荡。数据缺 = (None, [])。"""
    if not v:
        return None, []
    vr = v.get("vol_ratio")
    reasons = [f"SPY {'>' if v['above_ema10'] else '<'} EMA10"]
    if v["break_down"]:
        reasons.append("跌破前20日低")
    if v["break_up"]:
        reasons.append("突破前20日高")
    if vr is not None:
        reasons.append(f"波动比5/60 {vr:.2f}")
    hot = vr is not None and vr >= VOL_RATIO_HOT
    calm = vr is not None and vr < VOL_RATIO_CALM
    if v["break_down"] or (not v["above_ema10"] and hot):
        return DEFENSE, reasons
    if v["above_ema10"] and (v["break_up"] or calm):
        return ATTACK, reasons
    return RANGE, reasons


# ── 滞回状态机 (纯函数) ───────────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    state: str
    changed: bool
    trigger: str | None          # "slow" / "fast" / "fast_confirm" / None
    streak: dict                 # 快层升级确认计数 {"day", "target", "count", "slots"}
    note: str = ""


def _empty_streak() -> dict:
    return {"day": None, "target": None, "count": 0, "slots": []}


def step(prev: str | None, *, mode: str, slow: str | None = None, fast: str | None = None,
         streak: dict | None = None, day: str | None = None, slot: str | None = None,
         dwell: int | None = None) -> Step:
    """一次评估。mode = "close" (盘后慢层 + 收盘快层) / "hourly" (盘中整点快层, slow 传最近一次慢层目标)。

    - 降级: slow / fast 任一低于当前态 → 降到两者中更低的那个 (单次触发即生效)。
    - 升级 (close): 慢层目标高于当前态 → 升到 min(慢层, 收盘快层) (慢层同意, 且不越过同时刻快层读数)。
    - 升级 (hourly): 快层目标高于当前态时, 若最近慢层目标 ≥ 快层目标 → 直接升 (慢层同意);
      否则累计同一交易日连续整点确认, 满 2 次 → 升到这几次快层目标的最小值。
    - 最小驻留: dwell = 上次切换以来的交易日数 (None = 未知/冷启动, 不限); dwell < 3 时不升级 (note="min_dwell"),
      降级不受限。
    """
    hold = dwell is not None and dwell < MIN_DWELL_DAYS
    cur = prev if prev in RANK else DEFAULT_STATE
    st = dict(streak) if isinstance(streak, dict) else _empty_streak()
    if st.get("day") != day:
        st = _empty_streak() | {"day": day}
    layers = [x for x in (slow if mode == "close" else None, fast) if x in RANK]
    if mode == "hourly" and fast not in RANK:
        return Step(cur, False, None, st, "fast_missing")
    if mode == "close" and slow not in RANK:
        # 慢层缺数据: 只允许快层降级, 不升级
        if fast in RANK and RANK[fast] < RANK[cur]:
            return Step(fast, True, "fast", _empty_streak() | {"day": day})
        return Step(cur, False, None, st, "slow_missing")
    low = min(layers, key=lambda s: RANK[s])
    if RANK[low] < RANK[cur]:
        trig = "fast" if fast == low else "slow"
        return Step(low, True, trig, _empty_streak() | {"day": day})
    if mode == "close":
        tgt = slow if fast not in RANK else min((slow, fast), key=lambda s: RANK[s])
        if RANK[tgt] > RANK[cur] and hold:
            return Step(cur, False, None, _empty_streak() | {"day": day}, "min_dwell")
        if RANK[tgt] > RANK[cur]:
            return Step(tgt, True, "slow", _empty_streak() | {"day": day})
        return Step(cur, False, None, _empty_streak() | {"day": day})
    # hourly 升级
    if RANK[fast] <= RANK[cur]:
        return Step(cur, False, None, _empty_streak() | {"day": day})
    if hold:
        return Step(cur, False, None, _empty_streak() | {"day": day}, "min_dwell")
    if slow in RANK and RANK[slow] >= RANK[fast]:
        return Step(fast, True, "fast", _empty_streak() | {"day": day}, "slow_agrees")
    slots = [*list(st.get("slots") or []), slot]
    targets = [*list(st.get("targets") or []), fast]
    count = int(st.get("count") or 0) + 1
    if count >= UP_CONFIRM_SLOTS:
        tgt = min(targets[-UP_CONFIRM_SLOTS:], key=lambda s: RANK[s])
        if RANK[tgt] > RANK[cur]:
            return Step(tgt, True, "fast_confirm", _empty_streak() | {"day": day}, ",".join(map(str, slots[-2:])))
    return Step(cur, False, None, {"day": day, "target": fast, "count": count, "slots": slots, "targets": targets},
                "await_confirm")


# ── 持久化 ────────────────────────────────────────────────────────────

def _rules_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "rules"


def load_state(data_dir: Path) -> dict | None:
    try:
        d = json.loads((_rules_dir(data_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("state") in RANK else None


def load_audit(data_dir: Path) -> list[dict]:
    p = _rules_dir(data_dir) / AUDIT_FILE
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if isinstance(x, dict):
                out.append(x)
    except OSError:
        pass
    return out


def _append_audit(data_dir: Path, row: dict) -> None:
    """只追加: 打开方式 'a', 一行一事件, 写完 fsync。"""
    import os
    p = _rules_dir(data_dir) / AUDIT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _save_state(data_dir: Path, d: dict) -> None:
    from app.services.fs_utils import atomic_write_text
    p = _rules_dir(data_dir) / STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=1))


def _dwell(since_day: str | None, day: str) -> int | None:
    """上次切换日 → 本次评估日 经过的交易日数 (同日 = 0)。未知 = None。"""
    if not since_day:
        return None
    try:
        a, b = date.fromisoformat(since_day), date.fromisoformat(day)
    except ValueError:
        return None
    if b <= a:
        return 0
    try:
        from app.market_clock import us_clock
        return max(len(us_clock.trading_days_between(a, b)) - 1, 0)
    except Exception:
        return int(np.busday_count(a, b))


def _apply(data_dir: Path, *, mode: str, at: str, day: str, slot: str | None, slow_t: str | None,
           slow_r: list[str], slow_v: dict | None, fast_t: str | None, fast_r: list[str],
           fast_v: dict | None) -> dict:
    with _LOCK:
        prev = load_state(data_dir) or {}
        prev_state = prev.get("state")
        last_slow = slow_t if mode == "close" else (prev.get("slow") or {}).get("target")
        since_day = prev.get("since_day") if prev_state in RANK else None
        res = step(prev_state, mode=mode, slow=last_slow, fast=fast_t, streak=prev.get("streak"), day=day,
                   slot=slot, dwell=_dwell(since_day, day))
        reasons = (slow_r if mode == "close" else []) + fast_r
        new = {
            "contract": CONTRACT, "state": res.state, "as_of": day, "updated_at": at, "mode": mode, "slot": slot,
            "slow": {"target": last_slow, "reasons": slow_r if mode == "close" else (prev.get("slow") or {}).get(
                "reasons", []), "values": slow_v if mode == "close" else (prev.get("slow") or {}).get("values")},
            "fast": {"target": fast_t, "reasons": fast_r, "values": fast_v, "slot": slot},
            "streak": res.streak, "reasons": reasons, "note": res.note,
            "since": at if res.changed or not prev.get("since") else prev.get("since"),
            "since_day": day if res.changed or not since_day else since_day,
        }
        if res.changed or prev_state not in RANK:
            _append_audit(data_dir, {
                "at": at, "day": day, "mode": mode, "slot": slot, "trigger": res.trigger or "init",
                "from": prev_state if prev_state in RANK else None, "to": res.state,
                "conditions": reasons, "note": res.note,
                "values": {"slow": slow_v if mode == "close" else None, "fast": fast_v},
            })
        _save_state(data_dir, new)
        return new


# ── 数据读取 ──────────────────────────────────────────────────────────

def _spy_bars(repo, day: date, *, include_day: bool) -> dict | None:
    """SPY 日K (date <= day 或 < day)。读失败/空 = None。"""
    try:
        b = repo.get_daily_batch([INDEX], day - timedelta(days=200), day,
                                 columns=["symbol", "date", "high", "low", "close"])
    except Exception as e:
        logger.warning("regime: SPY daily unavailable: %s", type(e).__name__)
        return None
    if b is None or b.is_empty():
        return None
    g = b.filter(b["symbol"] == INDEX).sort("date")
    g = g.filter(g["date"] <= day) if include_day else g.filter(g["date"] < day)
    if g.is_empty():
        return None
    return {"date": g["date"].to_list(), "close": g["close"].to_numpy().astype(float),
            "high": g["high"].to_numpy().astype(float), "low": g["low"].to_numpy().astype(float)}


def breadth(frames: dict, watchlist, as_of: date, repo=None) -> float | None:
    """自选里收盘 > MA20 的占比 (截至 as_of)。可算的票 < 5 只 = None。"""
    import polars as pl
    above = total = 0
    missing = []
    for s in sorted(watchlist or ()):
        f = (frames or {}).get(s)
        closes = None
        if f is not None:
            try:
                g = f.filter(pl.col("date") <= as_of).sort("date") if "date" in f.columns else f
                closes = g["close"].to_numpy().astype(float)
            except Exception:
                closes = None
        if closes is None:
            missing.append(s)
            continue
        r = _above_ma20(closes)
        if r is not None:
            total += 1
            above += int(r)
    if missing and repo is not None:
        try:
            b = repo.get_daily_batch(missing, as_of - timedelta(days=60), as_of, columns=["symbol", "date", "close"])
            if b is not None and not b.is_empty():
                for g in b.filter(pl.col("date") <= as_of).partition_by("symbol"):
                    r = _above_ma20(g.sort("date")["close"].to_numpy().astype(float))
                    if r is not None:
                        total += 1
                        above += int(r)
        except Exception as e:
            logger.warning("regime breadth: daily unavailable: %s", type(e).__name__)
    return above / total if total >= 5 else None


def _above_ma20(closes) -> bool | None:
    c = _finite(closes)
    if c.size < BREADTH_MA:
        return None
    return bool(c[-1] > c[-BREADTH_MA:].mean())


# ── 入口 ──────────────────────────────────────────────────────────────

def evaluate_close(data_dir: Path, repo, as_of: date, *, frames: dict | None = None, watchlist=None,
                   now: datetime | None = None) -> dict | None:
    """盘后慢层 (decision_scan_job 调): 慢层 + 收盘快层 → 滞回 → 写状态 (+ 切换时追加审计)。
    返回快照字段 {"regime_state", "regime_as_of", "regime_reasons", "regime_contract"}; SPY 数据缺 = None
    (快照不写 regime 字段 → 机会板按 tight-v1 旧口径, 空头仍受 SPY < MA50 + fail-closed 约束)。
    ``"regime": false`` (回滚) = 不评估、不写状态与审计, 返回 None。"""
    if not enabled(data_dir):
        return None
    bars = _spy_bars(repo, as_of, include_day=True)
    if bars is None or not bars["date"] or bars["date"][-1] != as_of:
        logger.warning("regime: SPY close for %s missing; snapshot without regime", as_of)
        return None
    br = breadth(frames or {}, watchlist or (), as_of, repo)
    sv = slow_values(bars["close"], br)
    st, sr = slow_target(sv)
    fv = fast_values(bars["close"][:-1], bars["high"][:-1], bars["low"][:-1], float(bars["close"][-1]))
    ft, fr = fast_target(fv)
    at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    new = _apply(data_dir, mode="close", at=at, day=as_of.isoformat(), slot="close", slow_t=st, slow_r=sr,
                 slow_v=sv, fast_t=ft, fast_r=fr, fast_v=fv)
    return snapshot_fields(new)


def snapshot_fields(state: dict | None) -> dict | None:
    if not state or state.get("state") not in RANK:
        return None
    return {"regime_state": state["state"], "regime_as_of": state.get("updated_at"),
            "regime_reasons": list(state.get("reasons") or []), "regime_contract": CONTRACT}


def evaluate_hourly(data_dir: Path, repo, *, now: datetime | None = None, price: float | None = None) -> dict | None:
    """盘中整点快层 (挂在 zen_market_mood_preopen / zen_market_mood_intraday 同一任务上, 不另起定时器)。
    只在大盘重判的检查点窗口内执行 (market_mood.due_slot), 同一 slot 只评估一次。现价取不到 = 不评估。
    ``"regime": false`` (回滚) = 不评估、不写状态与审计。结果由 swing_desk.board_for_day 读入当日板 (只降不升)。"""
    if not enabled(data_dir):
        return None
    from app.market_clock import us_clock
    from app.services import market_mood
    now = us_clock.market_datetime(now or us_clock.now())
    slot = market_mood.due_slot(now)
    if slot is None:
        return None
    key = slot.strftime("%H:%M")
    day = now.date()
    prev = load_state(data_dir) or {}
    if prev.get("as_of") == day.isoformat() and prev.get("slot") == key:
        return prev
    if price is None:
        price = _live_price(repo, day, now)
    if price is None:
        logger.info("regime hourly %s %s: SPY live price unavailable, skipped", day, key)
        return None
    bars = _spy_bars(repo, day, include_day=False)
    if bars is None:
        return None
    fv = fast_values(bars["close"], bars["high"], bars["low"], price)
    ft, fr = fast_target(fv)
    if ft is None:
        return None
    return _apply(data_dir, mode="hourly", at=now.isoformat(timespec="seconds"), day=day.isoformat(), slot=key,
                  slow_t=None, slow_r=[], slow_v=None, fast_t=ft, fast_r=fr, fast_v=fv)


def _live_price(repo, day: date, now) -> float | None:
    try:
        from app.services import swing_desk
        px = swing_desk._live_prices(repo, {INDEX}, day, now=now).get(INDEX)
        if px and px[1] == "live":
            return float(px[0])
    except Exception as e:
        logger.warning("regime hourly: live price failed: %s", type(e).__name__)
    return None


def intraday_override(data_dir: Path, snapshot_day: date) -> dict | None:
    """盘中整点状态 → 当日板 (只降不升由调用方 lower() 保证)。
    只认「快照日之后的下一个交易日」(= 该快照的板在盘中生效的那一天) 由整点评估写入的状态;
    前一交易日 / 快照日当天 / 盘后评估写入的状态一律不认 (防跨日污染)。"""
    st = load_state(data_dir)
    if not st or st.get("mode") != "hourly":
        return None
    try:
        as_of = date.fromisoformat(str(st.get("as_of")))
        from app.market_clock import us_clock
        live_day = us_clock.next_trading_day(snapshot_day)
    except Exception:
        return None
    if as_of != live_day or as_of <= snapshot_day:
        return None
    return st


def status(data_dir: Path) -> dict[str, Any]:
    """只读: 当前状态 + 最近 20 条切换审计。"""
    return {"enabled": enabled(data_dir), "state": load_state(data_dir), "audit": load_audit(data_dir)[-20:]}
