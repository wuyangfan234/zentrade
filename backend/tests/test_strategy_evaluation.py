"""Unified-discipline evaluation of every strategy (requirements 2026-10-02: same 7-day rules for all sources,
backtest universe = top-1000 by prior 60-day median dollar volume, report by top-100 / 101-1000 / watchlist)."""
import numpy as np
import pytest

from app.services import strategy_evaluation as se


def path(o, h, l, c):
    return np.array(o, float), np.array(h, float), np.array(l, float), np.array(c, float)


def test_entry_next_open_inside_zone_target_hit_intraday():
    # signal day close 100; zone 100..101; inval 96; target 108 (2R on mid 100.5, risk 4.5 → 109.5? use explicit)
    o, h, l, c = path([100.5, 104, 107], [101, 106, 110], [99.5, 103, 106], [100.8, 105, 109])
    r = se.simulate_7d(o, h, l, c, entry_zone=(100.0, 101.0), invalidation=96.0, target=(108.0, 110.0), risk=4.5)
    assert r["status"] == "closed" and r["exit_reason"] == "target_zone_lower" and r["days"] == 3
    assert r["r"] == pytest.approx((108.0 * (1 - se.SLIP) - 100.5 * (1 + se.SLIP)) / 4.5 - se.FEE_R(4.5, 100.5), abs=0.02)


def test_gap_over_zone_does_not_fill():
    o, h, l, c = path([103.5, 104], [104, 105], [103, 103], [104, 104])
    assert se.simulate_7d(o, h, l, c, entry_zone=(100.0, 101.0), invalidation=96.0, target=(108, 110), risk=4.5)["status"] == "gap_expired"


def test_close_below_invalidation_exits_next_open():
    o, h, l, c = path([100.5, 99, 95, 94], [101, 99.5, 96, 95], [100, 95.5, 94, 93], [100.6, 95.5, 94.5, 94])
    r = se.simulate_7d(o, h, l, c, entry_zone=(100.0, 101.0), invalidation=96.0, target=(108, 110), risk=4.5)
    assert r["exit_reason"] == "invalidation" and r["exit_price"] == pytest.approx(95 * (1 - se.SLIP))
    assert r["days"] == 3


def test_time_stop_on_day_7_close():
    o = [100.5] + [101.0] * 9
    h = [101] * 10
    l = [100] * 10
    c = [100.6] + [101.0] * 9
    r = se.simulate_7d(*path(o, h, l, c), entry_zone=(100.0, 101.0), invalidation=96.0, target=(108, 110), risk=4.5)
    assert r["exit_reason"] == "time_stop" and r["days"] == 7


def test_insufficient_future_is_open_not_counted():
    r = se.simulate_7d(*path([100.5, 101], [101, 102], [100, 100], [100.6, 101.5]), entry_zone=(100.0, 101.0),
                       invalidation=96.0, target=(108, 110), risk=4.5)
    assert r["status"] == "open_at_end" and r["r"] is None


def test_summary_cluster_ci_and_buckets():
    trades = [{"r": 0.5, "entry_day": i // 2, "bucket": "top100" if i % 3 == 0 else "top1000", "watchlist": i % 5 == 0}
              for i in range(60)] + [{"r": -1.0, "entry_day": 100 + i, "bucket": "top1000", "watchlist": False} for i in range(20)]
    s = se.summarize(trades, seed=1)
    assert s["n"] == 80 and s["mean"] == pytest.approx((60 * 0.5 - 20) / 80, abs=1e-6)
    assert s["ci_low"] < s["mean"] < s["ci_high"]
    assert set(s["by_bucket"]) == {"top100", "top1000", "watchlist"}
    assert s["by_bucket"]["watchlist"]["n"] == 12


def test_liquidity_rank_is_point_in_time():
    # 3 symbols × 70 days; symbol 2 becomes most liquid only on the last day → must not rank #1 on that day
    dv = np.ones((70, 3))
    dv[:, 0] = 5.0
    dv[:, 1] = 3.0
    dv[-1, 2] = 1000.0
    ranks = se.prior_dollar_volume_rank(dv, window=60)
    assert ranks[-1, 0] == 1 and ranks[-1, 2] == 3
    assert np.all(ranks[:60] == 0) and ranks[60, 0] == 1  # first 60 days lack 60 prior days → unranked (0)


def test_gap_above_target_fills_at_open_not_target():
    o, h, l, c = path([100.5, 112.0], [101, 113], [100, 111], [100.6, 112.5])
    r = se.simulate_7d(o, h, l, c, entry_zone=(100.0, 101.0), invalidation=96.0, target=(108.0, 110.0), risk=4.5)
    assert r["exit_reason"] == "target_zone_lower" and r["exit_price"] == pytest.approx(112.0 * (1 - se.SLIP))


def test_stop_hit_and_target_same_day_counts_as_target_only_if_open_reached_first_conservative():
    # day 2 opens below stop then rallies through target: conservative = trailing/invalidation not intraday, target fills
    o, h, l, c = path([100.5, 95.0], [101, 109], [100, 94], [100.6, 105])
    r = se.simulate_7d(o, h, l, c, entry_zone=(100.0, 101.0), invalidation=96.0, target=(108.0, 110.0), risk=4.5)
    assert r["exit_reason"] == "target_zone_lower"  # production rule: invalidation is a close rule, target is intraday


def test_runner_engine_loads_builtin_strategies(tmp_path):
    """Moved from scripts/ into app/: the builtin strategy dir must still resolve (else 0 strategies, silent no-op)."""
    from app.services import strategy_evaluation_runner as runner
    ids = {m["id"] for m in runner._strategy_engine(tmp_path).list_strategies()}
    assert {"bullish_alignment", "long_lower_shadow_reversal", "trend_breakout"} <= ids


def test_runner_does_not_report_stale_sources_as_fresh(tmp_path, monkeypatch):
    from app.services import strategy_evaluation_runner as runner
    se.save_evaluation(tmp_path, {"sources": {"bullish_alignment": {"n": 1, "mean": 9.9}}, "trades": {}})
    monkeypatch.setattr(runner, "evaluate_strategy", lambda sid, *a, **k: ({"n": 5, "mean": 0.1, "ci_low": None, "ci_high": None,
                                                                          "elapsed_s": 0}, []))
    from datetime import date
    p = runner.run_all(tmp_path, date(2025, 1, 2), date(2025, 2, 28), only=["bullish_alignment"])
    assert p["sources"]["bullish_alignment"]["n"] == 5 and p["evaluated"] == ["bullish_alignment"]
