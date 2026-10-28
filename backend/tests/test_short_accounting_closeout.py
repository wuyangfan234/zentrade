"""Offline real-ledger regressions for review / portfolio cash-flow accounting."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import _round_trips, router
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.review.review import build_round_trips, weekly_report
from app.trader.trader import SwingTrader


@pytest.fixture
def ledger(tmp_path):
    account = Account(slippage_bps=0, commission_per_share=0, min_commission=0)
    led = Ledger(tmp_path / "ledger")
    led.init_account(account)
    return led, account


def append(led, kind, day="2026-10-05", **kwargs):
    return led.append(kind, {"symbol": "XYZ", "card_id": "c1", "trade_date": day, **kwargs},
                      at=day + "T10:00:00-04:00")


def opening(led, side, **kwargs):
    return append(led, "fill", **{
        "side": side, "qty": 100, "price": 100, "fee": 0,
        "stop_price": 105 if side == "short" else 95,
        "invalidation": 105 if side == "short" else 95,
        "target_zone": [80, 90] if side == "short" else [110, 120],
        "risk_per_share": 5, "setup_type": "breakout", "max_holding_days": 7,
        **kwargs,
    })


def client_for(led, account, monkeypatch, *, mark=45, booted=False):
    now = datetime(2026, 10, 7, 10, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "today", lambda: now.date())
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=led.data_dir.parent),
                                    get_daily=lambda *args: pl.DataFrame({"close": [float(mark)]}))
    if booted:
        app.state.trading_loop = SimpleNamespace(_boot=True, ledger=led, account=account)
    return TestClient(app)


def disk_state(led):
    return {p.relative_to(led.data_dir).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in led.data_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize("side,dividend", [("short", -100), ("buy", 100)])
def test_signed_dividend_matches_real_cash_without_changing_trade_prices(ledger, side, dividend):
    led, account = ledger
    opening(led, side)
    append(led, "corp_action", cash_dividend=dividend, day="2026-10-06")
    append(led, "close", qty=100, price=99, fee=0, day="2026-10-07", r_multiple=99)
    events = led.read_events()
    before = deepcopy(events)
    (trip,) = build_round_trips(events)
    plain = build_round_trips([e for e in events if e["kind"] != "corp_action"])[0]
    (api,), plans = _round_trips(events)
    assert trip.pnl == pytest.approx(led.cash - account.initial_equity) == 0
    assert api["pnl"] == trip.pnl
    assert api["r_multiple"] == trip.r_multiple == 0
    assert api["return_pct"] == 0
    assert trip.avg_entry == plain.avg_entry == 100
    assert trip.avg_exit == plain.avg_exit == 99
    assert trip.planned_risk == plain.planned_risk == 500
    assert api["entry_price"] == 100 and api["exit_price"] == 99
    assert trip.exit_reason_contributions["cash_dividend"]["pnl"] == dividend
    assert sum(x["pnl"] for x in trip.exit_reason_contributions.values()) == trip.pnl
    assert not plans and events == before


@pytest.mark.parametrize("side,partial,final", [("short", 90, 105), ("buy", 110, 95)])
def test_partial_round_r_does_not_use_final_exit_r(ledger, side, partial, final):
    led, account = ledger
    opening(led, side)
    append(led, "partial_close", qty=50, price=partial, fee=0, reason="target_zone_lower")
    trader = SwingTrader(led, account)
    pos = led.position("XYZ")
    plan, _ = trader._round_context(pos)
    event = trader._close_all(pos, final, date(2026, 10, 6), "2026-10-06T10:00:00-04:00",
                              reason="invalidation", plan=plan)
    assert event["r_multiple"] == -1
    (api,), plans = _round_trips(led.read_events())
    (trip,) = build_round_trips(led.read_events())
    assert api["pnl"] == trip.pnl == led.cash - account.initial_equity == 250
    assert api["r_multiple"] == trip.r_multiple == 0.5
    assert api["partial_exits"] == 1 and not plans


@pytest.mark.parametrize("side,mark,target", [("short", 45, [40, 45]), ("buy", 55, [55, 60])])
def test_split_live_plan_matches_trader_and_r(ledger, monkeypatch, side, mark, target):
    led, account = ledger
    opening(led, side)
    append(led, "corp_action", factor=2, day="2026-10-06")
    trader_plan, _ = SwingTrader(led, account)._round_context(led.position("XYZ"))
    _, plans = _round_trips(led.read_events())
    plan = plans["XYZ"]
    assert plan["entry_price"] == 50 and plan["qty"] == 200
    assert plan["target_zone"] == trader_plan["target_zone"] == target
    assert plan["risk_per_share"] == trader_plan["risk_per_share"] == 2.5
    assert plan["invalidation"] == trader_plan["invalidation"]
    before = disk_state(led)
    body = client_for(led, account, monkeypatch, mark=mark).get("/api/portfolio/live").json()
    (pos,) = body["positions"]
    assert pos["target_zone"] == target and pos["entry_price"] == 50
    assert pos["r_now"] == 2 and pos["target_distance_pct"] == 0
    assert disk_state(led) == before
    append(led, "close", qty=200, price=mark, fee=0, day="2026-10-07")
    (api,), _ = _round_trips(led.read_events())
    assert api["entry_price"] == 50 and api["exit_price"] == mark and api["qty"] == 200
    assert api["r_multiple"] == 2 and api["pnl"] == 1000


@pytest.mark.parametrize("side,direction", [("short", -1), ("buy", 1)])
def test_partial_dividends_cumulative_splits_fifo_and_net_r(ledger, monkeypatch, side, direction):
    led, account = ledger
    opening(led, side, qty=40, fee=4)
    opening(led, side, qty=60, price=102, fee=12)
    append(led, "partial_close", qty=50, price=100 + direction * 10, fee=5, reason="first")
    append(led, "corp_action", cash_dividend=direction * 50, day="2026-10-06")
    append(led, "corp_action", factor=2, day="2026-10-06")
    append(led, "stop_move", stop_price=51 if side == "short" else 49)
    append(led, "partial_close", qty=40, price=50 + direction * 5, fee=4, reason="second")
    # Reverse split follows a partial exit: first-entry units, not the current
    # remaining position size, must remain the denominator for the whole round.
    append(led, "corp_action", factor=0.5, cash_dividend=direction * 12, day="2026-10-07")
    _, plans = _round_trips(led.read_events())
    assert plans["XYZ"]["entry_price"] == pytest.approx(101.2)
    assert plans["XYZ"]["risk_per_share"] == 5
    assert plans["XYZ"]["stop_price"] == (102 if side == "short" else 98)
    append(led, "close", qty=30, price=100 - direction * 5, fee=3,
           reason="last", r_multiple=-123, day="2026-10-07")
    events = led.read_events()
    original = deepcopy(events)
    (trip,) = build_round_trips(events)
    (api,), plans = _round_trips(events)
    # Independent cash-flow calculation; no implementation-derived oracle.
    entry_notional = 40 * 100 + 60 * 102
    exit_notional = 50 * (100 + direction * 10) + 40 * (50 + direction * 5) + 30 * (100 - direction * 5)
    expected = direction * (exit_notional - entry_notional) - (4 + 12 + 5 + 4 + 3) + direction * (50 + 12)
    assert led.cash - account.initial_equity == pytest.approx(expected)
    assert trip.pnl == api["pnl"] == pytest.approx(expected)
    assert trip.planned_risk == 500 and trip.total_fees == api["fees"] == 28
    assert api["r_multiple"] == trip.r_multiple == pytest.approx(expected / 500)
    assert api["qty"] == 100 and api["entry_price"] == pytest.approx(101.2)
    assert api["exit_price"] == pytest.approx(exit_notional / 100)
    assert api["return_pct"] == pytest.approx(round(expected / (entry_notional + 16) * 100, 3))
    contributions = trip.exit_reason_contributions
    # First exit consumes all 40 shares / $4 fee of lot 1 plus 10 / $2 of lot 2.
    first_gross = direction * (50 * (100 + direction * 10) - (40 * 100 + 10 * 102))
    assert contributions["first"]["pnl"] == pytest.approx(first_gross - 4 - 2 - 5)
    assert contributions["first"]["fees"] == 11
    assert contributions["second"]["fees"] == 8
    assert contributions["last"]["fees"] == 9
    assert contributions["cash_dividend"]["pnl"] == direction * 62
    assert sum(c["pnl"] for c in contributions.values()) == pytest.approx(expected)
    assert sum(c["r_multiple"] for c in contributions.values()) == pytest.approx(expected / 500)
    assert not plans and original == events
    before = disk_state(led)
    body = client_for(led, account, monkeypatch).get("/api/portfolio/live").json()
    assert body["summary"]["realized_pnl"] == pytest.approx(expected)
    assert body["summary"]["avg_r"] == round(expected / 500, 3)
    assert weekly_report(led.data_dir.parent, date(2026, 10, 11))["trips"] == 1
    assert disk_state(led) == before


@pytest.mark.parametrize("side", ["short", "buy"])
@pytest.mark.parametrize("risk", [None, 0, -1])
def test_history_without_valid_frozen_risk_is_none_despite_exit_r(ledger, side, risk):
    led, _ = ledger
    opening(led, side, risk_per_share=risk)
    append(led, "close", qty=100, price=99, fee=0, r_multiple=17)
    (api,), _ = _round_trips(led.read_events())
    (trip,) = build_round_trips(led.read_events())
    assert api["r_multiple"] is None and trip.r_multiple is None


@pytest.mark.parametrize("first_kind,last_kind", [("close", "partial_close"), ("partial_close", "close")])
def test_only_actual_flat_quantity_closes_a_round(ledger, first_kind, last_kind):
    led, _ = ledger
    opening(led, "short")
    append(led, first_kind, qty=50, price=90, fee=0)
    closed, plans = _round_trips(led.read_events())
    assert not closed and "XYZ" in plans
    append(led, "corp_action", cash_dividend=-50)
    append(led, last_kind, qty=50, price=90, fee=0)
    (api,), plans = _round_trips(led.read_events())
    assert api["pnl"] == 950 and api["r_multiple"] == 1.9 and not plans


@pytest.mark.parametrize("side", ["short", "buy"])
def test_reopen_resets_split_and_dividend_context(ledger, side):
    led, _ = ledger
    opening(led, side)
    append(led, "corp_action", factor=2, cash_dividend=-100 if side == "short" else 100)
    append(led, "close", qty=200, price=50, fee=0)
    opening(led, side, day="2026-10-06")
    _, plans = _round_trips(led.read_events())
    assert plans["XYZ"]["entry_price"] == 100 and plans["XYZ"]["risk_per_share"] == 5
    append(led, "close", qty=100, price=100, fee=0, day="2026-10-07")
    trips = build_round_trips(led.read_events())
    closed, plans = _round_trips(led.read_events())
    assert len(closed) == len(trips) == 2 and not plans
    assert closed[1]["pnl"] == trips[1].pnl == 0
    assert closed[1]["r_multiple"] == trips[1].r_multiple == 0


@pytest.mark.parametrize("booted,materialized", [(False, True), (False, False), (True, True)])
def test_live_and_review_never_write_ledger(ledger, monkeypatch, booted, materialized):
    led, account = ledger
    opening(led, "short")
    append(led, "corp_action", factor=2, cash_dividend=-100)
    append(led, "close", qty=200, price=45, fee=0)
    if not materialized:
        (led.data_dir / "positions.json").unlink()
    before = disk_state(led)
    client = client_for(led, account, monkeypatch, booted=booted)
    for _ in range(2):
        assert client.get("/api/portfolio/live").status_code == 200
        build_round_trips(led.read_events())
        weekly_report(led.data_dir.parent, date(2026, 10, 11))
    assert disk_state(led) == before


def test_legacy_cardless_api_round_keeps_shape_but_unknown_r(ledger):
    led, _ = ledger
    opening(led, "buy", card_id=None, risk_per_share=None)
    append(led, "corp_action", card_id=None, cash_dividend=100)
    append(led, "close", qty=100, price=99, fee=0, card_id=None, r_multiple=123)
    (api,), plans = _round_trips(led.read_events())
    assert api["card_id"] is None and api["side"] == "long" and not plans
    assert api["r_multiple"] is None and api["pnl"] == 0
    assert set(api) == {"symbol", "side", "card_id", "setup_type", "entry_date", "entry_price", "exit_date",
                        "exit_price", "qty", "partial_exits", "exit_reason", "exit_reason_label", "days_held",
                        "fees", "pnl", "return_pct", "r_multiple"}


@pytest.mark.parametrize("side,mark", [("short", 90), ("buy", 110)])
def test_multiple_split_factors_accumulate_in_plan_and_prices(ledger, side, mark):
    led, account = ledger
    opening(led, side)
    for factor in (2, 3, 0.5):
        append(led, "corp_action", factor=factor)
    trader_plan, _ = SwingTrader(led, account)._round_context(led.position("XYZ"))
    _, plans = _round_trips(led.read_events())
    plan = plans["XYZ"]
    assert plan["qty"] == 300
    assert plan["entry_price"] == pytest.approx(100 / 3)
    assert plan["risk_per_share"] == trader_plan["risk_per_share"] == pytest.approx(5 / 3)
    assert plan["target_zone"] == trader_plan["target_zone"]
    append(led, "close", qty=300, price=mark / 3, fee=0)
    (api,), _ = _round_trips(led.read_events())
    (trip,) = build_round_trips(led.read_events())
    assert api["pnl"] == trip.pnl == pytest.approx(1000)
    assert api["r_multiple"] == trip.r_multiple == pytest.approx(2)
    assert trip.planned_risk == 500


@pytest.mark.parametrize("side,direction", [("short", -1), ("buy", 1)])
def test_fill_after_split_keeps_risk_in_first_fill_units(ledger, side, direction):
    led, account = ledger
    opening(led, side, qty=50)
    append(led, "corp_action", factor=2)
    opening(led, side, qty=100, price=50, risk_per_share=2.5)
    append(led, "partial_close", qty=100, price=50 + direction * 5, fee=0)
    append(led, "corp_action", cash_dividend=direction * 50)
    append(led, "close", qty=100, price=50 + direction * 5, fee=0)
    (api,), _ = _round_trips(led.read_events())
    (trip,) = build_round_trips(led.read_events())
    expected = 1000 + direction * 50
    assert trip.planned_risk == 50 * 5 + 100 * 2.5
    assert api["pnl"] == trip.pnl == led.cash - account.initial_equity == expected
    assert api["r_multiple"] == trip.r_multiple == expected / 500
    assert api["qty"] == 200 and api["entry_price"] == 50


@pytest.mark.parametrize("side,direction", [("short", -1), ("buy", 1)])
def test_return_percentage_does_not_round_cash_flow_before_division(ledger, side, direction):
    led, _ = ledger
    opening(led, side, qty=1, price=1, risk_per_share=0.1)
    append(led, "corp_action", cash_dividend=direction * 0.001)
    append(led, "close", qty=1, price=1 + direction * 0.001, fee=0)
    (api,), _ = _round_trips(led.read_events())
    assert api["pnl"] == 0  # cents are presentation-only, not accounting precision
    assert api["return_pct"] == pytest.approx(0.1 + direction * 0.1)
    assert api["r_multiple"] == pytest.approx((0.001 + direction * 0.001) / 0.1)


def test_weekly_degradation_uses_short_dividend_adjusted_rounds(ledger):
    led, _ = ledger
    for _ in range(5):
        opening(led, "short")
        append(led, "corp_action", cash_dividend=-200)
        append(led, "close", qty=100, price=99, fee=0)
    before = disk_state(led)
    report = weekly_report(led.data_dir.parent, date(2026, 10, 11))
    assert report["trips"] == 5
    assert report["by_setup"][0]["avg_r"] == -0.2
    assert report["degrade_candidates"] == ["breakout"]
    assert disk_state(led) == before
