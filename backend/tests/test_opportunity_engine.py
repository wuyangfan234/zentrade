"""机会引擎单测 (P2-2)。

合成数据验证: 完整突破形态成卡、各否决路径、参数即数据、组合约束、
大盘门、回放安全 (as_of 锚定)。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import load_params, market_gate, scan, universe_filter
from app.opportunity.setups.breakout import detect_breakout

PARAMS = load_params()
AS_OF = date(2026, 9, 25)


def _make_bars(leg_gain: float = 35.0, cons_days: int = 15, history: int = 160) -> pl.DataFrame:
    """合成: 平缓历史 + 上涨腿 + 窄幅整理。"""
    rows = []
    base = 100.0
    start = AS_OF - timedelta(days=history + 60)
    d = start
    # 前段平缓
    for i in range(history):
        rows.append({"date": d, "open": base, "high": base * 1.005, "low": base * 0.995,
                     "close": base, "volume": 1_000_000.0})
        d += timedelta(days=1)
    # 上涨腿 (10 天涨 leg_gain%)
    leg_days = 10
    leg_target = base * (1 + leg_gain / 100)
    for i in range(leg_days):
        c = base + (leg_target - base) * (i + 1) / leg_days
        rows.append({"date": d, "open": c * 0.998, "high": c * 1.01, "low": c * 0.995,
                     "close": c, "volume": 2_000_000.0})
        d += timedelta(days=1)
    # 整理 (窄幅 ±1%)
    for i in range(cons_days):
        c = leg_target * (1 + 0.003 * ((i % 4) - 1.5))
        rows.append({"date": d, "open": c * 0.999, "high": c * 1.004, "low": c * 0.996,
                     "close": c, "volume": 800_000.0})
        d += timedelta(days=1)
    return pl.DataFrame(rows)


def _spy_bars(above_ma: bool = True) -> pl.DataFrame:
    ma_days = PARAMS["market_gate"]["ma_days"]
    close = 500.0 if above_ma else 400.0
    n = ma_days + 5
    base = 450.0
    rows = [{"date": AS_OF - timedelta(days=n - i), "close": base, "volume": 1.0} for i in range(n)]
    rows[-1]["close"] = close  # 最后一根决定门
    return pl.DataFrame(rows)


# ── universe 过滤 ─────────────────────────────────────────

def test_universe_rejects_short_history():
    bars = _make_bars()[:50]
    assert universe_filter(bars, PARAMS) is False


def test_universe_rejects_low_price():
    bars = _make_bars().with_columns((pl.col("close") * 0.01).alias("close"))
    bars = bars.with_columns((pl.col("close") * 0.5).alias("high"), (pl.col("close") * 0.99).alias("low"))
    assert universe_filter(bars, PARAMS) is False


def test_universe_rejects_reverse_split_spike():
    bars = _make_bars()
    closes = bars["close"].to_list()
    closes[-3] = closes[-4] * 3  # 单日 +200% 疑似合股
    bars = bars.with_columns(pl.Series("close", closes))
    assert universe_filter(bars, PARAMS) is False


def test_universe_accepts_healthy():
    assert universe_filter(_make_bars(), PARAMS) is True


# ── setup 识别 ────────────────────────────────────────────

def test_breakout_detected_on_clean_pattern():
    hit = detect_breakout("TEST", _make_bars(), PARAMS, AS_OF)
    assert hit is not None
    assert hit.cons_days >= PARAMS["breakout"]["cons_min_days"]
    assert hit.leg_gain_pct >= PARAMS["breakout"]["leg_min_gain_pct"]


def test_breakout_rejected_without_leg():
    bars = _make_bars().filter(pl.col("date") < AS_OF - timedelta(days=80))
    # 纯平缓, 无腿
    flat = bars.filter(pl.col("date") <= bars["date"][0] + timedelta(days=100))
    hit = detect_breakout("FLAT", flat, PARAMS, AS_OF - timedelta(days=59))
    assert hit is None


def test_breakout_rejected_deep_retrace():
    """整理区跌破腿幅 50% → 拒绝。"""
    bars = _make_bars()
    rows = bars.to_dicts()
    # 把整理区下沿砸穿
    for r in rows[-8:]:
        r["low"] = r["close"] * 0.85
        r["close"] = r["close"] * 0.86
    hit = detect_breakout("DEEP", pl.DataFrame(rows), PARAMS, AS_OF)
    assert hit is None


# ── 管线 ─────────────────────────────────────────────────

def test_scan_produces_valid_card():
    bars = _make_bars()
    result = scan({"TEST": bars}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"})
    assert result.gate == "green"
    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.state == CardState.OPEN
    assert card.validate(PARAMS["breakout"]["min_reward_risk"]) == []
    assert card.entry_zone[0] > card.invalidation
    assert card.target_zone[0] > card.entry_zone[1]
    assert card.evidence["params_revision"] == PARAMS["revision"]


def test_scan_gate_yellow_halves_cap():
    bars = _make_bars()
    result = scan({f"S{i}": _make_bars() for i in range(6)}, _spy_bars(above_ma=False), PARAMS, AS_OF)
    assert result.gate == "yellow"
    assert len(result.cards) <= PARAMS["market_gate"]["caution_card_max"]
    assert len(result.watch) >= 1


def test_scan_symbol_exclusion():
    bars = _make_bars()
    result = scan({"TEST": bars}, _spy_bars(), PARAMS, AS_OF, active_symbols={"TEST"}, sector_map={"TEST":"Technology"})
    assert len(result.cards) == 0
    assert result.watch[0].state == CardState.WATCH


def test_scan_daily_cap():
    result = scan({f"S{i}": _make_bars() for i in range(8)}, _spy_bars(), PARAMS, AS_OF)
    assert len(result.cards) <= PARAMS["portfolio"]["daily_new_cards_max"]
    assert len(result.cards) + len(result.watch) == 8


def test_scan_sector_concentration():
    result = scan(
        {f"S{i}": _make_bars() for i in range(4)}, _spy_bars(), PARAMS, AS_OF,
        sector_map={f"S{i}": "TECH" for i in range(4)},
    )
    assert len(result.cards) <= PARAMS["portfolio"]["sector_max_active"]


def test_scan_portfolio_risk_cap():
    bars = _make_bars()
    result = scan({"TEST": bars}, _spy_bars(), PARAMS, AS_OF, portfolio_risk_used_pct=6.0, sector_map={"TEST":"Technology"})
    assert len(result.cards) == 0


def test_scan_replay_safe():
    """as_of 锚定: 只用 as_of 前的数据, 加未来 bar 不改变结论。"""
    bars = _make_bars()
    r1 = scan({"TEST": bars}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"})
    # 拼未来 5 天数据 (as_of 之后) — 与未来拼接前把 int 列转 float 对齐 schema
    future = [{"date": AS_OF + timedelta(days=i), "open": 1.0, "high": 1.0, "low": 1.0,
               "close": 1.0, "volume": 1.0} for i in range(1, 6)]
    bars2 = pl.concat([bars, pl.DataFrame(future)])
    r2 = scan({"TEST": bars2}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"})
    assert len(r1.cards) == len(r2.cards)
    if r1.cards:
        assert r1.cards[0].id == r2.cards[0].id


# ── 机会卡 ────────────────────────────────────────────────

def test_card_roundtrip():
    bars = _make_bars()
    result = scan({"TEST": bars}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"})
    card = result.cards[0]
    restored = OpportunityCard.from_dict(card.to_dict())
    assert restored == card


def test_card_frozen():
    result = scan({"TEST": _make_bars()}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"})
    card = result.cards[0]
    with pytest.raises(Exception):
        card.invalidation = 1.0  # frozen


def test_card_id_deterministic():
    a = scan({"TEST": _make_bars()}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"}).cards[0].id
    b = scan({"TEST": _make_bars()}, _spy_bars(), PARAMS, AS_OF, sector_map={"TEST":"Technology"}).cards[0].id
    assert a == b


# ── 批1：检测/组合约束拆分 + 复权后合股识别 ─────────────────────

def test_detect_candidates_matches_scan_before_portfolio_constraints():
    from app.opportunity.engine import apply_portfolio_constraints, detect_candidates
    bars = {f"S{i}": _make_bars(leg_gain=35 + i) for i in range(3)}
    detected = detect_candidates(bars, _spy_bars(), PARAMS, AS_OF)
    assert detected.gate == "green" and len(detected.candidates) == 3
    sectors = {s: "Technology" for s in bars}
    via_split = apply_portfolio_constraints(detected, PARAMS, sector_map=sectors)
    via_scan = scan(bars, _spy_bars(), PARAMS, AS_OF, sector_map=sectors)
    assert [c.id for c in via_split.cards] == [c.id for c in via_scan.cards]
    assert [c.id for c in via_split.watch] == [c.id for c in via_scan.watch]


def test_universe_rejects_reverse_split_on_adjusted_series():
    """复权后 close 连续，但 raw_close/close 比值骤升 = 近一年合股，仍须排除。"""
    bars = _make_bars()
    n = bars.height
    raw = [c * (1.0 if i >= n - 30 else 0.1) for i, c in enumerate(bars["close"].to_list())]
    bars = bars.with_columns(pl.Series("raw_close", raw))
    assert universe_filter(bars.drop("raw_close"), PARAMS) is True
    assert universe_filter(bars, PARAMS) is False


def test_universe_keeps_forward_split_on_adjusted_series():
    """正向拆股 raw_close/close 比值骤降，不能误伤。"""
    bars = _make_bars()
    n = bars.height
    raw = [c * (4.0 if i < n - 30 else 1.0) for i, c in enumerate(bars["close"].to_list())]
    assert universe_filter(bars.with_columns(pl.Series("raw_close", raw)), PARAMS) is True


# ── rev3: 整理期 <15 天时 ATR 收缩检查不得被静默跳过 (D2) ──────────

def _make_bars_custom(cons_days: int, cons_swing: float) -> pl.DataFrame:
    """上涨腿日内振幅约 1.5%；整理期振幅由 cons_swing 控制。"""
    rows, base, d = [], 100.0, AS_OF - timedelta(days=260)
    for _ in range(160):
        rows.append({"date": d, "open": base, "high": base * 1.005, "low": base * 0.995, "close": base, "volume": 1e6})
        d += timedelta(days=1)
    top = base * 1.35
    for i in range(10):
        c = base + (top - base) * (i + 1) / 10
        rows.append({"date": d, "open": c * 0.998, "high": c * 1.01, "low": c * 0.995, "close": c, "volume": 2e6})
        d += timedelta(days=1)
    for i in range(cons_days):
        c = top * (1 - 0.004 * (1 + i % 3))  # 收盘始终略低于腿顶, 腿终点不漂移
        rows.append({"date": d, "open": c, "high": c * (1 + cons_swing), "low": c * (1 - cons_swing), "close": c, "volume": 8e5})
        d += timedelta(days=1)
    return pl.DataFrame(rows)


def test_short_consolidation_atr_ratio_is_computed_not_zero():
    hit = detect_breakout("TEST", _make_bars_custom(12, 0.004), PARAMS, AS_OF)
    assert hit is not None
    assert 0 < hit.atr_ratio <= PARAMS["breakout"]["cons_atr_shrink"]


def test_short_consolidation_without_contraction_is_rejected():
    # 12 天整理, 日内振幅 ±2.5% (比上涨腿还大) —— rev2 会因 ATR=0 跳过检查而成卡
    assert detect_breakout("TEST", _make_bars_custom(12, 0.025), PARAMS, AS_OF) is None


def test_setups_revision_marks_atr_fix():
    assert PARAMS["revision"] >= 3
