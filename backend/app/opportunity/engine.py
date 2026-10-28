"""机会引擎编排 — 每日管线 (docs/architecture.md 管线 ⓪→⑤)。

纯编排, 不含 LLM/网络: 流动性过滤 → setup 识别 → 提案构造 → 组合约束 → 成卡。
scan(bars_by_symbol, params, as_of) 是纯函数 — 生产/回测同吃。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.setups.breakout import detect_breakout
from app.opportunity.setups.pullback import detect_pullback

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path(__file__).parents[2] / "config" / "setups.json"


@dataclass
class ScanResult:
    as_of: date
    cards: list[OpportunityCard] = field(default_factory=list)   # 通过组合约束的成卡
    watch: list[OpportunityCard] = field(default_factory=list)   # 降级观察 (组合约束拦截)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (symbol, 理由)
    scanned: int = 0
    gate: str = "green"          # green / yellow (SPY<MA200)
    params_revision: int = 0


def load_params(path: Path | None = None) -> dict:
    """参数即数据: 从 config/setups.json 读取 (带 version/revision 校验)。"""
    import json
    p = path or DEFAULT_PARAMS_PATH
    params = json.loads(p.read_text(encoding="utf-8"))
    if "version" not in params or "revision" not in params:
        raise ValueError("setups.json 缺 version/revision")
    return params


def market_gate(spy_bars: pl.DataFrame, params: dict) -> str:
    """大盘状态门 (docs/architecture.md ⓪): SPY 收盘 < MA200 → yellow。"""
    g = params["market_gate"]
    if spy_bars.height < g["ma_days"]:
        return "unavailable"  # 无基准证据不能假定绿灯开卡
    ma = spy_bars["close"].tail(g["ma_days"]).mean()
    last = spy_bars["close"][-1]
    return "green" if last >= ma else "yellow"


def universe_filter(bars: pl.DataFrame, params: dict) -> bool:
    """流动性+反垃圾门槛 (docs/architecture.md ①+①b)。bars 须升序且只含 as_of 及之前。"""
    u = params["universe"]
    if bars.height < u["min_history_days"]:
        return False
    last = float(bars["close"][-1])
    if last < u["min_price"]:
        return False
    recent = bars.tail(21)
    adv = float((recent["close"] * recent["volume"]).mean())
    if adv < u["min_adv_usd"]:
        return False
    # 反垃圾: 60 日最低价不低于 min_days_low_price (防仙股贴线复活)
    if float(bars.tail(60)["low"].min()) < u["min_days_low_price"]:
        return False
    # 反向拆股检测: 日内 close 较前日跳涨 >50% 且量骤降到零头 → 疑似合股
    closes = bars["close"].to_numpy()
    if len(closes) > 5:
        ratios = closes[1:] / closes[:-1]
        if np.any(ratios > 2.0):  # 单日翻倍以上 = 大概率合股/数据异常
            return False
    # 复权后序列连续, 合股只能由 raw_close/close 比值骤升识别 (docs/architecture.md ①b 近1年无反向拆股)。
    if "raw_close" in bars.columns:
        window = bars
        if "date" in bars.columns:
            from datetime import timedelta
            window = bars.filter(pl.col("date") > bars["date"][-1] - timedelta(days=int(u.get("no_reverse_split_days", 365))))
        ratio = (window["raw_close"] / window["close"]).to_numpy()
        if len(ratio) > 1 and np.any(ratio[1:] / ratio[:-1] > 1.5):
            return False
    # ATR塌缩会虚抬RR，现金收购钉价/极低波动不得当突破机会。
    if {"high", "low", "close"}.issubset(bars.columns):
        recent = bars.tail(21)
        tr = recent.select(pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr"))["tr"].tail(20).mean()
        if tr is None or last <= 0 or float(tr) / last * 100 < u.get("min_atr_pct", 0.5):
            return False
    return True


def _build_breakout_card(symbol: str, hit, params: dict, as_of: date) -> OpportunityCard | None:
    """提案构造 (管线 ③): 入场区/失效/目标/RR 过滤。"""
    p = params["breakout"]
    lo_pct, hi_pct = p["entry_zone_above_pct"]
    entry_lo = round(hit.cons_top * (1 + lo_pct / 100), 4)
    entry_hi = round(hit.cons_top * (1 + hi_pct / 100), 4)
    entry_mid = (entry_lo + entry_hi) / 2
    invalidation = round(max(hit.cons_bottom, hit.ma10 * 0.99), 4)  # 取紧者
    target = round(hit.leg_target, 4)
    if invalidation >= entry_lo:
        return None
    risk = entry_mid - invalidation
    if risk <= 0:
        return None
    rr = (target - entry_mid) / risk
    if rr < p["min_reward_risk"]:
        return None
    card = OpportunityCard(
        id=OpportunityCard.make_id(symbol, "breakout", as_of, params["revision"]),
        symbol=symbol,
        setup_type="breakout",
        created_date=as_of,
        entry_zone=(entry_lo, entry_hi),
        invalidation=invalidation,
        target_zone=(round(target * 0.98, 4), target),
        max_holding_days=7,
        risk_per_share=round(risk, 4),
        reward_risk=round(rr, 2),
        evidence={
            "leg_gain_pct": hit.leg_gain_pct,
            "cons_days": hit.cons_days,
            "atr_ratio": hit.atr_ratio,
            "last_close": hit.last_close,
            "cons_top": hit.cons_top,
            "cons_bottom": hit.cons_bottom,
            "ma10": hit.ma10,
            "params_revision": params["revision"],
        },
    )
    errs = card.validate(p["min_reward_risk"])
    return None if errs else card


def _build_pullback_card(symbol: str, hit, params: dict, as_of: date) -> OpportunityCard | None:
    """回踩提案构造: hit 已带入场区/失效/目标。"""
    p = params["pullback"]
    entry_mid = sum(hit.entry_zone) / 2
    risk = entry_mid - hit.invalidation
    if risk <= 0:
        return None
    rr = (hit.target - entry_mid) / risk
    if rr < p["min_reward_risk"]:
        return None
    card = OpportunityCard(
        id=OpportunityCard.make_id(symbol, "pullback", as_of, params["revision"]),
        symbol=symbol,
        setup_type="pullback",
        created_date=as_of,
        entry_zone=hit.entry_zone,
        invalidation=hit.invalidation,
        target_zone=(round(hit.target * 0.99, 4), round(hit.target, 4)),
        max_holding_days=7,
        risk_per_share=round(risk, 4),
        reward_risk=round(rr, 2),
        evidence={
            "retrace_pct": hit.retrace_pct,
            "rs_pctile": hit.rs_pctile,
            "last_close": hit.last_close,
            "cons_low": hit.consolidation_low,
            "target": hit.target,
            "params_revision": params["revision"],
        },
    )
    errs = card.validate(p["min_reward_risk"])
    return None if errs else card


@dataclass
class CandidateSet:
    """管线 ⓪-③ 的纯检测结果 (组合约束之前)。回放与生产共用同一检测。"""
    as_of: date
    gate: str
    params_revision: int
    scanned: int = 0
    candidates: list[OpportunityCard] = field(default_factory=list)  # 已按强度降序
    rejected: list[tuple[str, str]] = field(default_factory=list)


def _strength(c: OpportunityCard) -> float:
    """排序键 = RR × 证据强度 (突破: 腿幅×ATR收缩; 回踩: RS分位)。"""
    if c.setup_type == "breakout":
        # rev3: atr_ratio 恒为真实值 (>0); 下限 0.3 防止极小值把排序放大数倍
        return c.evidence.get("leg_gain_pct", 0) * (1 / max(c.evidence.get("atr_ratio", 1), 0.3))
    return c.evidence.get("rs_pctile", 0)


def detect_candidates(
    bars_by_symbol: dict[str, pl.DataFrame],
    spy_bars: pl.DataFrame,
    params: dict,
    as_of: date,
) -> CandidateSet:
    """管线 ⓪-③ (纯函数): 大盘门 → 流动性/反垃圾 → setup 识别 → 提案构造 → 强度排序。

    不含组合层约束，结果只依赖 as_of 及之前的 bars。
    """
    bars_by_symbol = {s: (b.filter(pl.col("date") <= as_of).sort("date") if "date" in b.columns else b) for s,b in bars_by_symbol.items()}
    if "date" in spy_bars.columns:
        spy_bars = spy_bars.filter(pl.col("date") <= as_of).sort("date")
    out = CandidateSet(as_of=as_of, gate=market_gate(spy_bars, params), params_revision=params["revision"])

    # 横截面 RS: 近 63 日涨幅分位 (回踩 setup 的相对强度门槛)
    closes_map = {s: b["close"].tail(63).to_numpy() for s, b in bars_by_symbol.items() if b.height >= 63}
    rs_rank: dict[str, float] = {}
    if closes_map:
        rets = {s: float(c[-1] / c[0] - 1) for s, c in closes_map.items() if c[0] > 0}
        import numpy as np
        vals = sorted(rets.values())
        for s, r in rets.items():
            # side="right": 单票时 rank=100 (唯一即最强), 避免 left 锚定到 0
            rs_rank[s] = float(np.searchsorted(vals, r, side="right") / max(len(vals), 1) * 100)

    for symbol, bars in bars_by_symbol.items():
        out.scanned += 1
        if not universe_filter(bars, params):
            out.rejected.append((symbol, "universe"))
            continue
        # 突破
        hit = detect_breakout(symbol, bars, params, as_of)
        if hit is not None:
            card = _build_breakout_card(symbol, hit, params, as_of)
            if card is None:
                out.rejected.append((symbol, "rr_or_zone:breakout"))
            else:
                out.candidates.append(card)
        # 回踩 (横截面 RS 前 20% 才走)
        if rs_rank.get(symbol, 0) >= (100 - params["pullback"]["rs_top_pct"]):
            pb = detect_pullback(symbol, bars, params, as_of)
            if pb is not None:
                from dataclasses import replace as _dc_replace
                pb = _dc_replace(pb, rs_pctile=rs_rank[symbol])
                card = _build_pullback_card(symbol, pb, params, as_of)
                if card is None:
                    out.rejected.append((symbol, "rr_or_zone:pullback"))
                else:
                    out.candidates.append(card)

    out.candidates.sort(key=lambda c: -(c.reward_risk * _strength(c)))
    return out


def apply_portfolio_constraints(
    detected: CandidateSet,
    params: dict,
    *,
    active_symbols: set[str] | None = None,   # 已有活跃卡的 symbol (排他)
    sector_map: dict[str, str] | None = None, # symbol → sector
    portfolio_risk_used_pct: float = 0.0,
) -> ScanResult:
    """管线 ④ 组合层约束: 每日上限 / 同票排他 / 行业上限 / 组合风险。"""
    result = ScanResult(as_of=detected.as_of, params_revision=detected.params_revision,
                        scanned=detected.scanned, gate=detected.gate,
                        rejected=list(detected.rejected))
    pf = params["portfolio"]
    gate = params["market_gate"]
    daily_cap = pf["daily_new_cards_max"] if result.gate == "green" else gate["caution_card_max"]
    risk_cap = pf["max_portfolio_risk_pct"] if result.gate == "green" else gate["caution_risk_pct"]

    active_symbols = set(active_symbols or ())
    sector_map = sector_map or {}
    active_sectors: dict[str, int] = {}
    for s in active_symbols:
        sec = sector_map.get(s, "?")
        active_sectors[sec] = active_sectors.get(sec, 0) + 1

    risk_used = portfolio_risk_used_pct
    from dataclasses import replace
    for card in detected.candidates:
        card = replace(card, evidence={**card.evidence, "sector": sector_map.get(card.symbol), "params_snapshot": params})
        if result.gate == "unavailable":
            result.watch.append(card.with_state(CardState.WATCH, "SPY基准数据不足"))
            continue
        if not sector_map.get(card.symbol):
            result.watch.append(card.with_state(CardState.WATCH, "行业尚未核实"))
            continue
        if len(result.cards) >= daily_cap:
            result.watch.append(card.with_state(CardState.WATCH, "超出每日新卡上限"))
            continue
        sym = card.symbol
        if sym in active_symbols:
            result.watch.append(card.with_state(CardState.WATCH, "同 symbol 已有活跃卡"))
            continue
        sec = sector_map.get(sym, "?")
        if active_sectors.get(sec, 0) >= pf["sector_max_active"]:
            result.watch.append(card.with_state(CardState.WATCH, f"行业 {sec} 已达上限"))
            continue
        # 组合敞口粗算: 假设 1% 单笔风险, 累加比对
        est_risk = 1.0
        if risk_used + est_risk > risk_cap:
            result.watch.append(card.with_state(CardState.WATCH, "组合风险超限"))
            continue
        result.cards.append(card.with_state(CardState.OPEN, "管线通过"))
        active_symbols.add(sym)  # 同轮双setup同票也排他
        active_sectors[sec] = active_sectors.get(sec, 0) + 1
        risk_used += est_risk

    return result


def scan(
    bars_by_symbol: dict[str, pl.DataFrame],
    spy_bars: pl.DataFrame,
    params: dict,
    as_of: date,
    *,
    active_symbols: set[str] | None = None,   # 已有活跃卡的 symbol (排他)
    sector_map: dict[str, str] | None = None, # symbol → GICS sector
    portfolio_risk_used_pct: float = 0.0,
) -> ScanResult:
    """每日扫描管线 (纯函数)。bars 全部只含 as_of 及之前。= 检测 + 组合约束。"""
    return apply_portfolio_constraints(
        detect_candidates(bars_by_symbol, spy_bars, params, as_of), params,
        active_symbols=active_symbols, sector_map=sector_map,
        portfolio_risk_used_pct=portfolio_risk_used_pct,
    )
