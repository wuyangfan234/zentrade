"""期权全链 → 摘要指标 (纯函数, 无网络)。输入: Alpha Vantage REALTIME_OPTIONS 的 data 列表 (字段全是字符串)。

输出只给前端/AI 需要的摘要, 原始链 (NVDA 约 3800 合约 / 1.6MB) 不下发:
  - term: 各到期日 ATM 隐含波动率 (期限结构) + 跨式价格 → 预期波动幅度 (straddle / spot)
  - pcr: 全链与近月的认沽/认购比 (按成交量、按持仓量)
  - skew: 近月 25-delta 看跌 IV − 25-delta 看涨 IV (正值 = 下行保护更贵)
  - oi_walls: 近 4 个到期日合计持仓最大的看涨/看跌行权价 (上方压力 / 下方支撑参考)
  - unusual: 成交量/持仓量 > 5 且成交量 > 1000 的合约 (当日新开仓迹象)
  - earnings_move: 若给了财报日, 取财报日后第一个到期日的预期波动
口径: 用 mark (买卖中间价) 计价; IV 用数据源提供值; ATM 取行权价最接近现价的一档。
深度价外/价内、零买价、IV 异常 (<=0 或 >5) 的合约不参与 IV 统计。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

UNUSUAL_RATIO = 5.0
UNUSUAL_MIN_VOLUME = 1000
WALL_EXPIRIES = 4


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def normalize(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        k, exp = _f(r.get("strike")), r.get("expiration")
        typ = str(r.get("type") or "").lower()
        if k is None or not exp or typ not in ("call", "put"):
            continue
        out.append({
            "id": r.get("contractID"), "exp": str(exp)[:10], "strike": k, "type": typ,
            "bid": _f(r.get("bid")), "ask": _f(r.get("ask")), "mark": _f(r.get("mark")), "last": _f(r.get("last")),
            "volume": int(_f(r.get("volume")) or 0), "oi": int(_f(r.get("open_interest")) or 0),
            "iv": _f(r.get("implied_volatility")), "delta": _f(r.get("delta")), "gamma": _f(r.get("gamma")),
            "theta": _f(r.get("theta")), "vega": _f(r.get("vega")), "as_of": r.get("date"),
        })
    return out


def _iv_ok(c: dict) -> bool:
    return c["iv"] is not None and 0 < c["iv"] < 5 and (c["bid"] or 0) > 0


def _price(c: dict) -> float | None:
    if c["mark"] and c["mark"] > 0:
        return c["mark"]
    if c["bid"] and c["ask"] and c["ask"] >= c["bid"] > 0:
        return (c["bid"] + c["ask"]) / 2
    return c["last"]


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b > 0 else None


def analyze(rows: list[dict], spot: float, *, today: date, earnings_date: date | None = None) -> dict:
    cs = normalize(rows)
    if not cs or not spot or spot <= 0:
        return {"ok": False, "reason": "期权链为空或缺少现价"}
    by_exp: dict[str, list[dict]] = defaultdict(list)
    for c in cs:
        by_exp[c["exp"]].append(c)
    exps = sorted(e for e in by_exp if date.fromisoformat(e) >= today)
    term = []
    for e in exps:
        chain = by_exp[e]
        strikes = sorted({c["strike"] for c in chain})
        if not strikes:
            continue
        atm_k = min(strikes, key=lambda k: abs(k - spot))
        call = next((c for c in chain if c["strike"] == atm_k and c["type"] == "call"), None)
        put = next((c for c in chain if c["strike"] == atm_k and c["type"] == "put"), None)
        ivs = [c["iv"] for c in (call, put) if c and _iv_ok(c)]
        pc, pp = (_price(call) if call else None), (_price(put) if put else None)
        straddle = (pc + pp) if pc is not None and pp is not None else None
        dte = (date.fromisoformat(e) - today).days
        term.append({"expiration": e, "dte": dte, "atm_strike": atm_k,
                     "atm_iv": round(sum(ivs) / len(ivs), 4) if ivs else None,
                     "straddle": round(straddle, 2) if straddle is not None else None,
                     "expected_move_pct": round(straddle / spot, 4) if straddle is not None else None})

    def _pcr(chain):
        pv = sum(c["volume"] for c in chain if c["type"] == "put")
        cv = sum(c["volume"] for c in chain if c["type"] == "call")
        po = sum(c["oi"] for c in chain if c["type"] == "put")
        co = sum(c["oi"] for c in chain if c["type"] == "call")
        return {"volume": _ratio(pv, cv), "open_interest": _ratio(po, co), "put_volume": pv, "call_volume": cv,
                "put_oi": po, "call_oi": co}

    future = [c for c in cs if c["exp"] in set(exps)]
    front = by_exp[exps[0]] if exps else []
    # 近月偏斜: 优先 7–45 天到期的第一档 (太近的周期权 delta 失真)
    skew_exp = next((t["expiration"] for t in term if 7 <= t["dte"] <= 45), exps[0] if exps else None)
    skew = None
    if skew_exp:
        chain = [c for c in by_exp[skew_exp] if _iv_ok(c) and c["delta"] is not None]
        p25 = min((c for c in chain if c["type"] == "put"), key=lambda c: abs(c["delta"] + 0.25), default=None)
        c25 = min((c for c in chain if c["type"] == "call"), key=lambda c: abs(c["delta"] - 0.25), default=None)
        if p25 and c25:
            skew = {"expiration": skew_exp, "put25_iv": round(p25["iv"], 4), "call25_iv": round(c25["iv"], 4),
                    "put25_strike": p25["strike"], "call25_strike": c25["strike"],
                    "risk_reversal": round(p25["iv"] - c25["iv"], 4)}

    walls_scope = [c for c in future if c["exp"] in set(exps[:WALL_EXPIRIES])]
    oi_by: dict[tuple[str, float], int] = defaultdict(int)
    for c in walls_scope:
        oi_by[(c["type"], c["strike"])] += c["oi"]

    def _top(typ, above):
        cand = [(k, v) for (t, k), v in oi_by.items() if t == typ and (k >= spot if above else k <= spot) and v > 0]
        return [{"strike": k, "open_interest": v} for k, v in sorted(cand, key=lambda kv: -kv[1])[:3]]

    unusual = sorted(
        ({"contract": c["id"], "expiration": c["exp"], "strike": c["strike"], "type": c["type"], "volume": c["volume"],
          "open_interest": c["oi"], "ratio": round(c["volume"] / max(c["oi"], 1), 1), "iv": c["iv"]}
         for c in future if c["volume"] >= UNUSUAL_MIN_VOLUME and c["volume"] / max(c["oi"], 1) >= UNUSUAL_RATIO),
        key=lambda u: -u["volume"])[:8]

    earnings_move = None
    if earnings_date:
        after = next((t for t in term if date.fromisoformat(t["expiration"]) >= earnings_date and t["straddle"] is not None), None)
        if after:
            earnings_move = {"earnings_date": earnings_date.isoformat(), "expiration": after["expiration"],
                             "expected_move_pct": after["expected_move_pct"], "atm_iv": after["atm_iv"]}

    as_of = max((c["as_of"] for c in cs if c["as_of"]), default=None)
    return {"ok": True, "spot": spot, "as_of": as_of, "contracts": len(cs), "expirations": len(exps),
            "term": term[:12], "pcr": {"all": _pcr(future), "front": _pcr(front), "front_expiration": exps[0] if exps else None},
            "skew": skew, "oi_walls": {"calls_above": _top("call", True), "puts_below": _top("put", False),
                                       "expirations": exps[:WALL_EXPIRIES]},
            "unusual": unusual, "earnings_move": earnings_move,
            "heatmap": heatmap(cs, spot, exps)}


HEAT_EXPIRIES = 6
HEAT_STRIKES = 15


def heatmap(cs: list[dict], spot: float, exps: list[str]) -> dict | None:
    """多空力量热力图 (只展示): 横轴最近 6 个到期日, 纵轴现价附近 15 个行权价。
    格子 bias = (call OI − put OI) / (call OI + put OI) ∈ [-1, 1], 正 = 看涨力量大。"""
    ex = exps[:HEAT_EXPIRIES]
    if not ex or not spot:
        return None
    cells: dict[tuple[str, float], dict] = {}
    for c in cs:
        if c["exp"] not in ex:
            continue
        k = cells.setdefault((c["exp"], c["strike"]), {"call_oi": 0, "put_oi": 0, "call_vol": 0, "put_vol": 0})
        k[f"{c['type']}_oi"] += c["oi"]
        k[f"{c['type']}_vol"] += c["volume"]
    strikes_all = sorted({k for (_, k), v in cells.items() if v["call_oi"] + v["put_oi"] > 0})
    if not strikes_all:
        return None
    near = sorted(strikes_all, key=lambda k: abs(k - spot))[:HEAT_STRIKES]
    strikes = sorted(near, reverse=True)
    rows = []
    tot_c = tot_p = 0
    by_strike_c: dict[float, int] = defaultdict(int)
    by_strike_p: dict[float, int] = defaultdict(int)
    for k in strikes:
        row = []
        for e in ex:
            v = cells.get((e, k))
            if not v or v["call_oi"] + v["put_oi"] == 0:
                row.append(None)
                continue
            s = v["call_oi"] + v["put_oi"]
            tot_c += v["call_oi"]
            tot_p += v["put_oi"]
            by_strike_c[k] += v["call_oi"]
            by_strike_p[k] += v["put_oi"]
            row.append({**v, "bias": round((v["call_oi"] - v["put_oi"]) / s, 3), "oi": s})
        rows.append({"strike": k, "cells": row})
    above = [k for k in strikes if k > spot]
    below = [k for k in strikes if k < spot]
    # 墙必须真有持仓: 上方看涨 OI 最大处 = 压力, 下方看跌 OI 最大处 = 支撑
    resistance = max((k for k in above if by_strike_c[k] > 0), key=lambda k: by_strike_c[k], default=None)
    support = max((k for k in below if by_strike_p[k] > 0), key=lambda k: by_strike_p[k], default=None)
    tot = tot_c + tot_p
    share = tot_c / tot if tot else 0.5
    stance = "偏多" if share >= 0.6 else ("偏空" if share <= 0.4 else "多空均衡")
    top_calls = sorted((k for k in strikes if by_strike_c[k]), key=lambda k: -by_strike_c[k])[:3]
    focus = (min(top_calls), max(top_calls)) if top_calls else None
    return {"expirations": ex, "rows": rows, "spot": spot, "call_share": round(share, 3), "stance": stance,
            "resistance": resistance, "support": support, "call_focus": focus}
