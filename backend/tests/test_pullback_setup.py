"""回踩 setup 单测 (P2-4)。合成数据: 趋势/回踩/缩量/企稳/失效/RR。

夹具口径 (数值先验已验证): 温和上行 (0.5%/日) → V 形回踩 8-10% →
锤子线企稳 (贴近低点, 不收 MA20 — 否则离前高太近 RR<2)。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.opportunity.engine import load_params, scan
from app.opportunity.setups.pullback import detect_pullback

PARAMS = load_params()
AS_OF = date(2026, 9, 25)


def _make_uptrend_pullback(retrace_pct: float = 10.0, dry: bool = True,
                           history: int = 160) -> pl.DataFrame:
    """温和上行 (0.5%/日) → V 形回踩 → 锤子线企稳 (贴近低点, RR≥2)。"""
    rows = []
    base = 100.0
    start = AS_OF - timedelta(days=history + 30)
    d = start
    for _ in range(history):
        base *= 1.005
        rows.append({"date": d, "open": base * 0.9995, "high": base * 1.004,
                     "low": base * 0.998, "close": base, "volume": 1_000_000.0})
        d += timedelta(days=1)
    peak = base
    pull = peak * (1 - retrace_pct / 100)
    dip = pull * 0.997
    mid = (peak + pull) / 2
    vol = 400_000.0 if dry else 1_200_000.0
    # V 形回踩: 中段见低点, 窄幅保 ATR
    for c in [mid, pull, dip]:
        rows.append({"date": d, "open": c * 1.0005, "high": c * 1.001, "low": c * 0.999,
                     "close": c, "volume": vol})
        d += timedelta(days=1)
    # 企稳日: 锤子线 (下影线深探后收回, 开/收在当日上部)
    settle_close = dip * 1.012
    settle_open = dip * 1.011
    rows.append({"date": d, "open": settle_open, "high": settle_close * 1.001,
                 "low": dip * 0.995, "close": settle_close, "volume": vol * 0.85})
    return pl.DataFrame(rows)


def test_pullback_detected_clean():
    hit = detect_pullback("TEST", _make_uptrend_pullback(), PARAMS, AS_OF)
    assert hit is not None
    assert hit.entry_zone[0] < hit.target
    assert hit.invalidation < hit.entry_zone[0]


def test_pullback_rejects_no_dryup():
    assert detect_pullback("TEST", _make_uptrend_pullback(dry=False), PARAMS, AS_OF) is None


def test_pullback_rejects_deep_retrace():
    assert detect_pullback("TEST", _make_uptrend_pullback(retrace_pct=20.0), PARAMS, AS_OF) is None


def test_pullback_rejects_downtrend():
    rows = []
    base = 100.0
    d = AS_OF - timedelta(days=180)
    for _ in range(160):
        base *= 0.997
        rows.append({"date": d, "open": base, "high": base * 1.01, "low": base * 0.99,
                     "close": base, "volume": 1_000_000.0})
        d += timedelta(days=1)
    assert detect_pullback("DOWN", pl.DataFrame(rows), PARAMS, AS_OF) is None


def test_pullback_card_via_scan():
    """经引擎成卡: 单票横截面 RS=100 (唯一即最强)。"""
    bars = _make_uptrend_pullback()
    spy = pl.DataFrame([{"date": AS_OF - timedelta(days=i), "close": 500.0, "volume": 1.0}
                        for i in range(210)]).sort("date")
    result = scan({"TEST": bars}, spy, PARAMS, AS_OF, sector_map={"TEST": "Technology"})
    pb_cards = [c for c in result.cards if c.setup_type == "pullback"]
    assert len(pb_cards) == 1
    card = pb_cards[0]
    assert card.validate(PARAMS["pullback"]["min_reward_risk"]) == []
    assert card.evidence["rs_pctile"] == pytest.approx(100.0)
