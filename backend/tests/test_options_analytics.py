"""期权链摘要: 用 2026-10-01 NVDA 真实链子集 (付费实测数据) 验证口径。"""
import json
from datetime import date
from pathlib import Path

from app.services import options_analytics as oa

FX = json.loads((Path(__file__).parent / "fixtures" / "qveris_nvda_chain_20261001.json").read_text())
ROWS, SPOT = FX["data"], FX["spot"]
TODAY = date(2026, 10, 2)


def test_term_structure_atm_and_expected_move():
    out = oa.analyze(ROWS, SPOT, today=TODAY)
    assert out["ok"] and out["as_of"] == "2026-10-01"
    t = {x["expiration"]: x for x in out["term"]}
    w = t["2026-10-09"]
    assert w["atm_strike"] == 235.0 and w["dte"] == 7
    # 235C mark 2.35 + 235P mark 6.17 = 8.52 → 8.52/234.805 ≈ 3.63%
    assert w["straddle"] == 8.52 and abs(w["expected_move_pct"] - 0.0363) < 0.0005
    assert 0.27 < w["atm_iv"] < 0.29
    assert [x["expiration"] for x in out["term"]] == sorted(t)  # 按到期日升序


def test_skew_uses_25_delta_on_7_to_45_day_expiry():
    s = oa.analyze(ROWS, SPOT, today=TODAY)["skew"]
    assert s["expiration"] == "2026-10-09"
    assert s["put25_strike"] < SPOT < s["call25_strike"]
    assert s["risk_reversal"] == round(s["put25_iv"] - s["call25_iv"], 4)


def test_pcr_and_oi_walls_sides():
    out = oa.analyze(ROWS, SPOT, today=TODAY)
    pcr = out["pcr"]["all"]
    assert pcr["volume"] == round(pcr["put_volume"] / pcr["call_volume"], 3)
    walls = out["oi_walls"]
    assert all(w["strike"] >= SPOT for w in walls["calls_above"])
    assert all(w["strike"] <= SPOT for w in walls["puts_below"])
    oi = [w["open_interest"] for w in walls["calls_above"]]
    assert oi == sorted(oi, reverse=True)


def test_unusual_activity_thresholds():
    for u in oa.analyze(ROWS, SPOT, today=TODAY)["unusual"]:
        assert u["volume"] >= oa.UNUSUAL_MIN_VOLUME and u["ratio"] >= oa.UNUSUAL_RATIO


def test_earnings_move_picks_first_expiry_on_or_after_earnings():
    em = oa.analyze(ROWS, SPOT, today=TODAY, earnings_date=date(2026, 11, 18))["earnings_move"]
    assert em["expiration"] == "2026-11-20" and 0.05 < em["expected_move_pct"] < 0.2


def test_expired_contracts_and_bad_iv_are_excluded():
    rows = ROWS + [dict(ROWS[0], expiration="2026-09-25", contractID="OLD")]
    out = oa.analyze(rows, SPOT, today=TODAY)
    assert "2026-09-25" not in [t["expiration"] for t in out["term"]]
    bad = [dict(r, implied_volatility="9.99", bid="0") for r in ROWS if r["expiration"] == "2026-10-09"]
    t = {x["expiration"]: x for x in oa.analyze(bad, SPOT, today=TODAY)["term"]}
    assert t["2026-10-09"]["atm_iv"] is None   # 零买价 / 异常 IV 不参与 IV 统计


def test_empty_or_missing_spot_fails_closed():
    assert oa.analyze([], SPOT, today=TODAY)["ok"] is False
    assert oa.analyze(ROWS, 0, today=TODAY)["ok"] is False


def test_heatmap_bias_walls_and_stance():
    from datetime import date
    from app.services import options_analytics as oa
    rows = []
    for k, c_oi, p_oi in [(90, 100, 900), (100, 500, 500), (110, 3000, 100), (120, 1000, 0)]:
        rows.append({"strike": k, "expiration": "2026-10-23", "type": "call", "open_interest": c_oi, "volume": 1})
        rows.append({"strike": k, "expiration": "2026-10-23", "type": "put", "open_interest": p_oi, "volume": 1})
    h = oa.heatmap(oa.normalize(rows), 101.0, ["2026-10-23"])
    assert [r["strike"] for r in h["rows"]] == [120, 110, 100, 90]
    assert h["rows"][1]["cells"][0]["bias"] > 0.9 and h["rows"][3]["cells"][0]["bias"] < -0.7
    assert h["resistance"] == 110 and h["support"] == 90 and h["stance"] == "偏多"
    assert oa.analyze(rows, 101.0, today=date(2026, 10, 15))["heatmap"]["stance"] == "偏多"


def test_heatmap_wall_requires_open_interest():
    from app.services import options_analytics as oa
    rows = [{"strike": 110, "expiration": "2026-10-23", "type": "put", "open_interest": 50, "volume": 0},
            {"strike": 90, "expiration": "2026-10-23", "type": "call", "open_interest": 50, "volume": 0}]
    h = oa.heatmap(oa.normalize(rows), 100.0, ["2026-10-23"])
    assert h["resistance"] is None and h["support"] is None
