"""swing_desk tight-v1: 4h 门 (事件或状态) + 3+3 打分 + 空头 SPY 门 + 紧几何。"""
import json
from datetime import date, datetime

import pytest

from app.services import swing_desk as sd


def _sig(rule, symbol, side, **kw):
    row = {"rule": rule, "symbol": symbol, "side": side, "rank": kw.get("rank", 1),
           "entry_zone": kw.get("entry_zone", [100.0, 101.0]),
           "invalidation": kw.get("invalidation", 94.0 if side == "long" else 106.0),
           "target_zone": kw.get("target_zone", [112.0, 115.0] if side == "long" else [85.0, 88.0]),
           "risk": kw.get("risk", 6.0)}
    row.update({k: v for k, v in kw.items() if k not in row})
    return row


DAY = date(2026, 10, 13)


def test_defaults_are_three_plus_three():
    assert len(sd.DEFAULT_LONG) == 3 and len(sd.DEFAULT_SHORT) == 3
    assert "phantom_4h_short" not in sd.DEFAULT_SHORT
    assert "evo" not in sd.DEFAULT_LONG


def test_geometry_long_formula_nvda_example():
    g = sd.geometry("long", 180.0, 6.0)
    assert g["entry_zone"] == [177.0, 178.5]
    assert g["stop"] == 172.5 and g["target"] == 190.5
    assert g["risk"] == pytest.approx(5.25)              # 区中值 177.75 - 172.5
    assert g["rr"] == pytest.approx(2.43, abs=0.01)       # (190.5-177.75)/5.25
    assert g["stop"] < g["entry_zone"][0] < g["entry_zone"][1] < g["target"]


def test_geometry_short_mirror():
    g = sd.geometry("short", 180.0, 6.0)
    assert g["entry_zone"] == [181.5, 183.0]
    assert g["stop"] == 187.5 and g["target"] == 169.5
    assert g["target"] < g["entry_zone"][0] < g["entry_zone"][1] < g["stop"]
    assert g["risk"] == pytest.approx(5.25)


@pytest.mark.parametrize("close,atr", [(None, 1.0), (100.0, 0.0), (100.0, float("nan")), (-1.0, 2.0)])
def test_geometry_rejects_bad_inputs(close, atr):
    assert sd.geometry("long", close, atr) is None


def test_card_is_tight_not_strategy_geometry():
    """卡价位来自 ATR 紧几何, 不再取命中策略的宽卡 (止损 2ATR+ / 目标 60 日高)。"""
    sig = [_sig("macd_golden", "LLY", "long", invalidation=50.0, target_zone=[300.0, 310.0]),
           _sig("phantom_4h", "LLY", "long", score=80)]
    out = sd.compose_boards(signals=sig, closes={"LLY": 100.0}, as_of=DAY, atr={"LLY": 2.0},
                            long_rules=("macd_golden",))
    c = out["long"][0]
    assert c["entry_zone"] == [99.0, 99.5] and c["stop"] == 97.5 and c["target"] == 103.5
    assert abs(c["stop_pct"]) < 3 and c["target_pct"] < 5


def test_compose_requires_phantom_gate():
    signals = [
        _sig("macd_golden", "NVDA", "long"),
        _sig("volume_price_surge", "NVDA", "long"),
        # no phantom_4h / no 4h state for NVDA → must not appear
        _sig("tv_halftrend", "AMD", "short"),
        _sig("phantom_4h_short", "AMD", "short", stop=-0.04),
        _sig("macd_golden", "LLY", "long"),
        _sig("phantom_4h", "LLY", "long", score=80),
    ]
    closes = {"NVDA": 180.0, "AMD": 200.0, "LLY": 900.0}
    out = sd.compose_boards(signals=signals, closes=closes, as_of=DAY, atr={s: 5.0 for s in closes}, short_ok=True)
    syms_l = {c["symbol"] for c in out["long"]}
    assert "NVDA" not in syms_l, "weak-tape NVDA long without 4h must be gated out"
    assert "LLY" in syms_l
    assert {c["symbol"] for c in out["short"]} == {"AMD"}


def test_gate_event_or_state():
    syms = ("EVT", "STA", "NONE", "OPP")
    sig = [_sig("macd_golden", s, "long") for s in syms]
    sig.append(_sig("phantom_4h", "EVT", "long"))
    out = sd.compose_boards(signals=sig, closes={s: 50.0 for s in syms}, as_of=DAY, atr={s: 1.0 for s in syms},
                            h4_state={"STA": "long", "OPP": "short"}, long_rules=("macd_golden",))
    assert {c["symbol"]: c["gate"] for c in out["long"]} == {"EVT": "event", "STA": "state"}


def test_short_needs_spy_gate_and_fail_closed():
    sig = [_sig("tv_halftrend", "AMD", "short"), _sig("phantom_4h_short", "AMD", "short")]
    kw = dict(signals=sig, closes={"AMD": 200.0}, as_of=DAY, atr={"AMD": 5.0})
    assert sd.compose_boards(**kw, short_ok=True)["short"][0]["symbol"] == "AMD"
    assert sd.compose_boards(**kw, short_ok=False)["short"] == []
    assert sd.compose_boards(**kw, short_ok=None)["short"] == []


def test_missing_atr_no_card():
    sig = [_sig("macd_golden", "X", "long"), _sig("phantom_4h", "X", "long")]
    out = sd.compose_boards(signals=sig, closes={"X": 10.0}, as_of=DAY, long_rules=("macd_golden",))
    assert out["long"] == []
    assert out["meta"]["skipped"][0]["reason"] == "no_atr_or_close"


def test_direction_conflict_drops_both():
    sig = [_sig("macd_golden", "X", "long"), _sig("phantom_4h", "X", "long"),
           _sig("tv_halftrend", "X", "short"), _sig("phantom_4h_short", "X", "short")]
    out = sd.compose_boards(signals=sig, closes={"X": 10.0}, as_of=DAY, atr={"X": 0.5},
                            long_rules=("macd_golden",), short_ok=True)
    assert out["long"] == [] and out["short"] == []


def test_score_counts_strategy_hits_and_caps():
    signals = []
    for i, sym in enumerate(["A", "B", "C", "D", "E", "F"]):
        signals.append(_sig("tv_smc_structure", sym, "long"))
        signals.append(_sig("volume_price_surge", sym, "long"))
        signals.append(_sig("macd_golden", sym, "long"))
        signals.append(_sig("phantom_4h", sym, "long", score=90 - i))
    out = sd.compose_boards(signals=signals, closes={s: 10.0 for s in "ABCDEF"}, as_of=DAY,
                            atr={s: 0.5 for s in "ABCDEF"}, max_per_side=5)
    assert len(out["long"]) == 5
    assert out["long"][0]["hit_count"] == 3
    assert out["long"][0]["score"] >= 3.0


@pytest.mark.full_edition
def test_build_empty_without_snapshot(tmp_path):
    out = sd.build(tmp_path)
    assert out["available"] is False
    assert out["long"] == [] and out["short"] == []
    assert out["long_rules"] == list(sd.DEFAULT_LONG)


def test_swing_json_overrides_defaults(tmp_path):
    sd.save_strategies(tmp_path, ["macd_golden", "volume_price_surge"], ["tv_halftrend", "tv_chandelier_exit"])
    long_ids, short_ids = sd.strategies_from_ruleset(tmp_path)
    assert long_ids == ("macd_golden", "volume_price_surge")
    assert short_ids == ("tv_halftrend", "tv_chandelier_exit")


def _write_snapshot(tmp_path, day, **extra):
    d = tmp_path / "opportunities" / "watchlist"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"as_of": day.isoformat(), "watchlist": ["LLY"], "closes": {"LLY": 100.0},
               "signals": [_sig("macd_golden", "LLY", "long"), _sig("phantom_4h", "LLY", "long")], **extra}
    (d / f"{day.isoformat()}.json").write_text(json.dumps(payload))


def test_build_reads_atr_from_snapshot(tmp_path):
    _write_snapshot(tmp_path, DAY, atr={"LLY": 2.0})
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    out = sd.build(tmp_path)
    assert out["available"] is True
    assert out["long"][0]["entry_zone"] == [99.0, 99.5]


def test_old_snapshot_without_atr_gives_empty_board(tmp_path):
    _write_snapshot(tmp_path, DAY)
    out = sd.build(tmp_path)
    assert out["long"] == [] and out["meta"]["reason"] == "snapshot_without_atr"


def test_window_over():
    from app.market_clock import us_clock
    d = date(2026, 10, 15)

    def at(h, m):
        return datetime(2026, 10, 15, h, m, tzinfo=us_clock.tz)
    assert sd.window_over(1, d, at(15, 0)) is False
    assert sd.window_over(2, d, at(15, 59)) is False
    assert sd.window_over(2, d, at(16, 0)) is True
    assert sd.window_over(3, d, at(10, 0)) is True
