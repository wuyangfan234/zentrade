"""卡片回放内核 (0.29.1 从旧 watchlist_cards 迁出, 逐字照搬, 行为不变)。

- replay_state / replay_state_short: 与 strategy_evaluation.simulate_native_daily(_short) 同一套入场/离场规则,
  返回“截至最后一根已收盘日K”的状态 (pending / expired / gap_expired / triggered / closed)。
  下单簿 (旧规则集路径)、卡级结算、对拍测试共用这一份; 改结算规则时两边必须同步 (400 条随机路径对拍锁定)。
- hold_days: 信号行的最长持有天数 (evo 行 5 日, 其余 None = 7 日)。
- SHORT_NAMES / rule_name: 看空卡与持仓页来源的中文名 (TV 移植策略卖出 = 翻空)。
"""
from __future__ import annotations

import numpy as np


def replay_state(o, h, l, c, *, entry_zone, invalidation, target, risk, hold: int | None = None) -> dict:  # noqa: E741
    """与 simulate_native_daily 同一套规则, 但返回“截至最后一根已收盘日K”的状态:
    pending / expired / gap_expired / triggered(held) / closed。数组 = 信号日之后的已收盘日K。"""
    from app.services import strategy_evaluation as se
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry, gap_down_fill = None, None, False
    for i in range(min(se.ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(l[i])):
            continue
        if op > hi * se.GAP_LIMIT:
            return {"status": "gap_expired"}
        if lo <= op <= hi:
            px = op
        elif op < lo and h[i] >= lo:
            px = lo
        elif op > hi and l[i] <= hi:
            px, gap_down_fill = hi, True
        else:
            continue
        fill_i, entry = i, px * (1 + se.SLIP)
        break
    if fill_i is None:
        return {"status": "expired"} if n >= se.ENTRY_WINDOW else {"status": "pending", "elapsed": n}
    mh = se.MAX_HOLD if hold is None else int(hold)      # evo 卡 = 5 日 (单张卡最多持有一周)
    near, far = target[0], target[1]
    stop, peak = invalidation, entry
    half_done, legs = False, []

    def done(reason, raw_exit, k):
        exit_px = raw_exit * (1 - se.SLIP)
        w = 0.5 if half_done else 1.0
        legs.append({"reason": reason, "weight": w, "price": round(float(exit_px), 4), "day": k + 1})
        r = sum(x["weight"] * (x["price"] - entry) for x in legs) / risk - se.FEE_R(risk, entry) * (1.5 if half_done else 1.0)
        return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": k + 1}

    def held(k_done, exit_next=None):
        return {"status": "triggered", "entry": round(float(entry), 4), "fill_index": fill_i, "stop": round(float(stop), 4),
                "half": half_done, "hold_day": k_done, "exit_next_open": exit_next}

    for k in range(mh):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return held(k)
        high = max(entry, c[i]) if (k == 0 and gap_down_fill) else h[i]
        if k > 0 and np.isfinite(o[i]) and o[i] >= far:
            return done("target_zone_upper", o[i], k)
        if not half_done and high >= near:
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] >= near else near
            legs.append({"reason": "target_zone_lower", "weight": 0.5, "price": round(float(px * (1 - se.SLIP)), 4), "day": k + 1})
            half_done = True
            stop = max(stop, entry)
        if half_done and high >= far:
            return done("target_zone_upper", far, k)
        if k == mh - 1:
            return done("time_stop", c[i], k)
        peak = max(peak, high)
        if peak >= entry + 2 * risk:
            stop = max(stop, entry + risk)
        elif peak >= entry + risk:
            stop = max(stop, entry)
        if c[i] < stop:
            j = i + 1
            if j >= n or not np.isfinite(o[j]):
                reason = "invalidation" if stop == invalidation else ("breakeven_after_partial" if half_done and stop == entry
                                                                      else "trailing_stop")
                return held(k + 1, exit_next=reason)
            reason = "invalidation" if stop == invalidation else ("breakeven_after_partial" if half_done and stop == entry
                                                                  else "trailing_stop")
            return done(reason, o[j], k + 1)
    return held(mh)


def replay_state_short(o, h, lo_, c, *, entry_zone, invalidation, target, risk, hold: int | None = None) -> dict:
    """空头: 与 strategy_evaluation.simulate_native_daily_short 同一套规则 (replay_state 的逐条镜像, 与交易员空头规则一致),
    返回截至最后一根已收盘日K的状态。开盘低于入场下沿 2% 不追; 最低触及目标近端 (target[1]) 回补一半、止损下移到保本,
    远端 (target[0]) 回补余仓 (BH-03); 浮盈 1R 保本、2R 锁 1R (止损只下移); 收盘站上止损 → 次日开盘回补; 第 7 日收盘回补。
    开盘低于入场区、反弹到下沿成交 (gap_up_fill) 时成交日最低只计 min(入场, 收盘) (BH-02)。"""
    from app.services import strategy_evaluation as se
    lo, hi = entry_zone
    n = len(c)
    fill_i, entry, gap_up_fill = None, None, False
    for i in range(min(se.ENTRY_WINDOW, n)):
        op = o[i]
        if not (np.isfinite(op) and np.isfinite(h[i]) and np.isfinite(lo_[i])):
            continue
        if op < lo * (2 - se.GAP_LIMIT):
            return {"status": "gap_expired"}
        if lo <= op <= hi:
            px = op
        elif op > hi and lo_[i] <= hi:
            px = hi
        elif op < lo and h[i] >= lo:
            px, gap_up_fill = lo, True
        else:
            continue
        fill_i, entry = i, px * (1 - se.SLIP)
        break
    if fill_i is None:
        return {"status": "expired"} if n >= se.ENTRY_WINDOW else {"status": "pending", "elapsed": n}
    mh = se.MAX_HOLD if hold is None else int(hold)
    near, far = target[1], target[0]
    stop, trough = invalidation, entry
    half_done, legs = False, []

    def done(reason, raw_exit, k):
        exit_px = raw_exit * (1 + se.SLIP)
        w = 0.5 if half_done else 1.0
        legs.append({"reason": reason, "weight": w, "price": round(float(exit_px), 4), "day": k + 1})
        r = sum(x["weight"] * (entry - x["price"]) for x in legs) / risk - se.FEE_R(risk, entry) * (1.5 if half_done else 1.0)
        return {"status": "closed", "r": round(float(r), 4), "exit_reason": reason, "days": k + 1}

    def held(k_done, exit_next=None):
        return {"status": "triggered", "entry": round(float(entry), 4), "fill_index": fill_i, "stop": round(float(stop), 4),
                "half": half_done, "hold_day": k_done, "exit_next_open": exit_next}

    for k in range(mh):
        i = fill_i + k
        if i >= n or not np.isfinite(c[i]):
            return held(k)
        low = min(entry, c[i]) if (k == 0 and gap_up_fill) else lo_[i]
        if k > 0 and np.isfinite(o[i]) and o[i] <= far:
            return done("target_zone_upper", o[i], k)
        if not half_done and low <= near:
            px = o[i] if k > 0 and np.isfinite(o[i]) and o[i] <= near else near
            legs.append({"reason": "target_zone_lower", "weight": 0.5, "price": round(float(px * (1 + se.SLIP)), 4), "day": k + 1})
            half_done = True
            stop = min(stop, entry)
        if half_done and low <= far:
            return done("target_zone_upper", far, k)
        if k == mh - 1:
            return done("time_stop", c[i], k)
        trough = min(trough, low)
        if trough <= entry - 2 * risk:
            stop = min(stop, entry - risk)
        elif trough <= entry - risk:
            stop = min(stop, entry)
        if c[i] > stop:
            j = i + 1
            reason = "invalidation" if stop == invalidation else ("breakeven_after_partial" if half_done and stop == entry
                                                                  else "trailing_stop")
            if j >= n or not np.isfinite(o[j]):
                return held(k + 1, exit_next=reason)
            return done(reason, o[j], k + 1)
    return held(mh)


SHORT_NAMES = {
    "tv_supertrend": "Supertrend 翻空", "tv_ut_bot": "UT Bot 翻空", "tv_chandelier_exit": "吊灯止损翻空",
    "tv_halftrend": "HalfTrend 翻空", "tv_zlsma_cross": "收盘下穿 ZLSMA", "tv_squeeze_momentum": "挤压动能转弱",
    "tv_wavetrend": "WaveTrend 超买强死叉", "tv_smc_structure": "SMC 波段结构转空", "tv_phantom_shift": "Phantom 趋势换挡卖出",
    "tv_phantom_trail": "Phantom 追踪轨道转空",
}


def rule_name(names: dict, rule: str, short: bool) -> str:
    if rule == "evo":
        return "自进化 · 看空" if short else "自进化 · 看多"
    return SHORT_NAMES.get(rule, names.get(rule, rule)) if short else names.get(rule, rule)


def hold_days(x: dict) -> int | None:
    """信号行的最长持有天数: evo 行带 evo.hold_days (5); 其余规则 None = 沿用 7 日。"""
    h = (x.get("evo") or {}).get("hold_days") if isinstance(x.get("evo"), dict) else None
    return int(h) if type(h) is int and 1 <= h <= 7 else None
