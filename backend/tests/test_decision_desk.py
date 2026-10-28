"""Decision desk (requirements 2026-10-02): all strategies are candidate sources, unified 7-day card discipline,
evidence-based weights (cluster-CI lower bound, shrinkage, caps), exploration quota for thin evidence,
zero weight for negative evidence, resonance bonus, manual source management, two tabs (pool / watchlist)."""
from datetime import date

import polars as pl
import pytest

from app.opportunity.card import CardState
from app.services import decision_desk as dd


def bars(n=80, base=100.0, step=0.2, atr=2.0):
    rows = []
    for i in range(n):
        c = base + step * i
        rows.append({"date": date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + i), "open": c - 0.3,
                     "high": c + atr / 2, "low": c - atr / 2, "close": c, "volume": 1e6})
    return pl.DataFrame(rows)


# ── unified card from any strategy pick ──

def test_strategy_pick_becomes_7_day_card_with_atr_invalidation_and_2r_target():
    b = bars()
    last = float(b["close"][-1])
    card = dd.card_from_pick("bullish_alignment", "AAPL", b, as_of=b["date"][-1], stop_loss=-0.06)
    assert card is not None
    assert card.setup_type == "strategy:bullish_alignment" and card.max_holding_days == 7
    lo, hi = card.entry_zone
    assert lo < last * 1.0051 and hi > last  # entry zone around last close (0~+1%)
    entry_mid = (lo + hi) / 2
    # invalidation = tighter of strategy stop (-6%) and 2×ATR below entry
    assert card.invalidation == pytest.approx(max(entry_mid * 0.94, entry_mid - 2 * dd.atr(b)), rel=1e-3)
    assert card.reward_risk >= 2.0
    assert card.evidence["source"] == "bullish_alignment"


def test_pick_without_room_for_2r_is_rejected():
    b = bars(atr=0.0001, step=0.0)
    assert dd.card_from_pick("x", "FLAT", b, as_of=b["date"][-1], stop_loss=None) is None


# ── evidence → weight ──

def test_weight_negative_evidence_is_zero_and_shown():
    w = dd.source_weight({"n": 1395, "ci_low": -0.152, "mean": -0.077})
    assert w["weight"] == 0 and w["tier"] == "negative" and "置信下界" in w["reason"]


def test_weight_thin_evidence_gets_exploration_quota():
    w = dd.source_weight({"n": 12, "ci_low": -0.3, "mean": 0.2})
    assert w["tier"] == "explore" and w["risk_pct"] == pytest.approx(0.0025) and w["daily_cap"] == 1


def test_weight_positive_evidence_full_risk_and_shrunk():
    strong = dd.source_weight({"n": 400, "ci_low": 0.05, "mean": 0.12})
    thinner = dd.source_weight({"n": 60, "ci_low": 0.05, "mean": 0.12})
    assert strong["tier"] == "proven" and strong["risk_pct"] == pytest.approx(0.01)
    assert 0 < thinner["weight"] < strong["weight"]  # fewer trades → shrunk toward neutral


def test_weekly_update_caps_change_at_50_percent():
    assert dd.capped_update(previous=1.0, target=3.0) == pytest.approx(1.5)
    assert dd.capped_update(previous=1.0, target=0.2) == pytest.approx(0.5)
    assert dd.capped_update(previous=None, target=3.0) == pytest.approx(3.0)
    assert dd.capped_update(previous=1.0, target=0.0) == 0.0  # negative evidence drops to zero immediately


def test_manual_disable_and_cap_override_evidence(tmp_path):
    reg = dd.SourceRegistry(tmp_path, builtin={"bullish_alignment", "trend_breakout"})
    reg.set_status("bullish_alignment", "disabled", note="手动停用")
    reg.set_cap("trend_breakout", 0.5)
    assert reg.effective("bullish_alignment", {"weight": 2.0, "tier": "proven"})["weight"] == 0
    assert reg.effective("trend_breakout", {"weight": 2.0, "tier": "proven"})["weight"] == 0.5
    log = reg.audit()
    assert [e["action"] for e in log] == ["status", "cap"] and log[0]["note"] == "手动停用"


def test_new_source_defaults_to_observe_until_user_enables(tmp_path):
    reg = dd.SourceRegistry(tmp_path)
    assert reg.status("mined_abc") == "observe"
    assert reg.effective("mined_abc", {"weight": 2.0, "tier": "proven"})["weight"] == 0
    reg.set_status("mined_abc", "enabled")
    assert reg.effective("mined_abc", {"weight": 2.0, "tier": "proven"})["weight"] == 2.0


def test_builtin_sources_start_enabled(tmp_path):
    reg = dd.SourceRegistry(tmp_path, builtin={"breakout", "pullback", "bullish_alignment"})
    assert reg.status("bullish_alignment") == "enabled"


# ── ranking & tabs ──

def test_rank_combines_weights_resonance_and_marks_tabs():
    picks = {"NVDA": ["bullish_alignment", "trend_breakout"], "AAPL": ["bullish_alignment"], "XYZ": ["neg_source"]}
    weights = {"bullish_alignment": {"weight": 1.0, "tier": "proven", "risk_pct": 0.01},
               "trend_breakout": {"weight": 0.5, "tier": "explore", "risk_pct": 0.0025},
               "neg_source": {"weight": 0.0, "tier": "negative", "risk_pct": 0.0}}
    ranked = dd.rank(picks, weights, watchlist={"AAPL"})
    assert [r["symbol"] for r in ranked] == ["NVDA", "AAPL", "XYZ"]
    nv = ranked[0]
    assert nv["resonance"] == 2 and nv["score"] == pytest.approx((1.0 + 0.5) * dd.RESONANCE_BONUS)
    assert nv["risk_pct"] == pytest.approx(0.01)  # best tier among its tradable sources
    assert ranked[1]["tab"] == "watchlist" and ranked[0]["tab"] == "pool"
    assert ranked[2]["tradable"] is False and ranked[2]["score"] == 0


def test_explore_quota_one_per_day():
    ranked = [{"symbol": s, "tradable": True, "tier": "explore", "score": 1.0, "risk_pct": 0.0025} for s in ("A", "B")]
    kept, held = dd.apply_explore_quota(ranked)
    assert [r["symbol"] for r in kept] == ["A"] and [r["symbol"] for r in held] == ["B"]


def test_seed_enables_go_live_sources_once_later_sources_default_observe(tmp_path):
    reg = dd.SourceRegistry(tmp_path)
    reg.seed({"bullish_alignment", "trend_breakout"})
    assert reg.status("bullish_alignment") == "enabled"
    reg.seed({"bullish_alignment", "trend_breakout", "mined_new"})  # second seed is a no-op
    assert reg.status("mined_new") == "observe"
    assert [e["action"] for e in reg.audit()] == ["seed"]
    reg.set_status("mined_new", "enabled", note="人工启用")
    assert reg.status("mined_new") == "enabled"
