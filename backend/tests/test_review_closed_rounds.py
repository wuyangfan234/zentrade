"""闭合回合、分批费用归因、只读周报回归（纯本地数据）。"""
from copy import deepcopy
from datetime import date
import json

import pytest

from app.review.review import build_round_trips, setup_stats, weekly_report


def fill(qty=10, price=100, fee=1, day="2026-09-21", **extra):
    return dict(kind="fill", card_id="c1", symbol="AAPL", side="buy",
                setup_type="breakout", qty=qty, price=price, fee=fee,
                risk_per_share=5, trade_date=day, **extra)


def close(qty=10, price=110, fee=1, kind="close", reason="target", day="2026-09-25", **extra):
    return dict(kind=kind, card_id="c1", symbol="AAPL", qty=qty,
                price=price, fee=fee, reason=reason, trade_date=day, **extra)


@pytest.mark.parametrize("kind", ["partial_close", "close"])
def test_non_flat_round_never_counts_as_closed(kind):
    assert build_round_trips([fill(), close(qty=5, kind=kind)]) == []


def test_partial_exits_aggregate_fees_and_frozen_risk_once():
    events = [fill(), close(qty=4, kind="partial_close", reason="target_low", fee=0.4),
              {"kind": "stop_move", "card_id": "c1", "stop_price": 102, "risk_per_share": 1},
              close(qty=6, price=98, fee=0.6, reason="stop", risk_per_share=1)]
    trips = build_round_trips(events)
    assert len(trips) == 1
    trip = trips[0]
    assert trip.planned_risk == 10 * 5
    assert trip.pnl == pytest.approx(4 * 110 + 6 * 98 - 10 * 100 - 1 - 0.4 - 0.6)
    assert trip.r_multiple == pytest.approx(trip.pnl / trip.planned_risk)
    assert trip.total_fees == pytest.approx(2)
    assert trip.exit_reason == "stop"
    contributions = trip.exit_reason_contributions
    assert set(contributions) == {"target_low", "stop"}
    assert contributions["target_low"]["pnl"] == pytest.approx(4 * (110 - 100) - 0.4 - 0.4)
    assert sum(c["pnl"] for c in contributions.values()) == pytest.approx(trip.pnl)
    assert sum(c["r_multiple"] for c in contributions.values()) == pytest.approx(trip.r_multiple)
    assert setup_stats(trips)[0].count == 1
    assert setup_stats(trips)[0].wins == 1


def test_unknown_events_ignored_without_mutating_input():
    events = [fill(), {"card_id": "c1", "unrelated": True},
              {"kind": "future_event", "card_id": "c1", "qty": "not a trade"}, close()]
    original = deepcopy(events)
    assert len(build_round_trips(events)) == 1
    assert events == original


def test_fractional_reverse_split_closes_only_after_remaining_quantity_exits():
    events = [fill(qty=1, price=100),
              {"kind": "corp_action", "symbol": "AAPL", "factor": 0.5},
              close(qty=0.25, price=220, kind="partial_close", fee=0.25)]
    assert build_round_trips(events) == []
    trips = build_round_trips([*events, close(qty=0.25, price=180, fee=0.25, reason="stop")])
    assert len(trips) == 1
    assert trips[0].planned_risk == 5
    assert trips[0].pnl == pytest.approx(0.25 * 220 + 0.25 * 180 - 100 - 1.5)


def test_tiny_fractional_position_does_not_close_early():
    assert build_round_trips([fill(qty=0.000002), close(qty=0.000001)]) == []
    assert len(build_round_trips([fill(qty=0.3), close(qty=0.1, kind="partial_close"), close(qty=0.2)])) == 1


def test_reopened_card_is_a_separate_round_not_one_merged_win():
    trips = build_round_trips([fill(), close(), fill(day="2026-09-28"),
                              close(price=90, day="2026-09-29")])
    assert len(trips) == 2
    assert setup_stats(trips)[0].wins == 1


def test_missing_frozen_risk_is_unknown_not_zero_r():
    event = fill()
    del event["risk_per_share"]
    trip = build_round_trips([event, close()])[0]
    assert trip.r_multiple is None
    stat = setup_stats([trip])
    assert stat[0].count == 1
    assert stat[0].wins == 1
    assert stat[0].r_count == 0
    assert stat[0].avg_r is None


def test_weekly_report_is_read_only_and_excludes_future_and_prior_week(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    events = [fill(day="2026-09-18"), close(day="2026-09-20"),
              fill(), close(), fill(day="2026-09-28"), close(day="2026-09-29")]
    (ledger / "events.jsonl").write_text("".join(json.dumps({"seq": i, "at": e["trade_date"] + "T12:00:00", **e}) + "\n"
                                                for i, e in enumerate(events, 1)))
    (ledger / "account.json").write_text('{"initial_equity": 123456}')
    (ledger / "positions.json").write_text('{"do_not_rewrite": true}')
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in ledger.iterdir()}
    report = weekly_report(tmp_path, date(2026, 9, 27))
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in ledger.iterdir()}
    assert report["trips"] == 1
    assert before == after
    # 周报可以序列化为严格 JSON，不能把无穷盈亏比当数字返回。
    json.dumps(report, allow_nan=False)


def test_weekly_report_with_no_account_has_consistent_shape_and_no_writes(tmp_path):
    report = weekly_report(tmp_path, date(2026, 9, 27))
    assert report["trips"] == 0
    assert report["by_setup"] == []
    assert not (tmp_path / "ledger").exists()
