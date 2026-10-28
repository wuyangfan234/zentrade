"""批A Trader 契约回归：真实事件落盘、离线时钟、重启与线程竞争。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.trader.trader import SwingTrader

DAY = date(2026, 9, 25)


def card(**kw):
    data = dict(id="c1", symbol="AAPL", setup_type="breakout", created_date=DAY,
                entry_zone=(100., 102.), invalidation=95., target_zone=(112., 116.),
                risk_per_share=6., reward_risk=2.3, max_holding_days=7, state=CardState.ACTIVE)
    data.update(kw)
    return OpportunityCard(**data)


@pytest.fixture
def trader(tmp_path):
    account = Account()
    ledger = Ledger(tmp_path / "ledger")
    ledger.init_account(account)
    return SwingTrader(ledger, account)


def tick(trader, c=None, price=101., ts="2026-09-25T10:00:00-04:00", at=None):
    c = c or card()
    at = at or ts
    return trader.try_fill_entry(c, price=price, trade_date=DAY, at=at, quote_ts=ts)


def open_position(trader, c=None):
    c = c or card()
    trader.register_card(c)
    assert tick(trader, c) is None
    result = tick(trader, c, ts="2026-09-25T10:01:00-04:00")
    assert result is not None and result["kind"] == "fill"
    return result


def restarted(trader):
    ledger = Ledger(trader.ledger.data_dir)
    ledger.replay(trader.account)
    # 故意不重新注册卡；退出必须能读 fill 的冻结提案。
    return SwingTrader(ledger, trader.account)


def manage(trader, price, at="2026-09-25T11:00:00-04:00"):
    return trader.manage_intraday("AAPL", price=price,
                                  trade_date=datetime.fromisoformat(at).date(), at=at, quote_ts=at)


def test_card_book_is_instance_local(trader, tmp_path):
    trader.register_card(card())
    other = SwingTrader(Ledger(tmp_path / "other"), trader.account)
    assert other._cards == {}


@pytest.mark.parametrize("state", [s for s in CardState if s != CardState.ACTIVE])
def test_only_active_can_record_intent(trader, state):
    assert tick(trader, card(state=state)) is None
    assert trader.ledger.read_events() == []


@pytest.mark.parametrize("ts", ["2026-09-25T09:44:59-04:00", "2026-09-25T15:30:01-04:00",
                               "2026-09-25T08:00:00-04:00", "2026-09-25T20:00:00-04:00"])
def test_entry_window_rejects_intent_and_fill(trader, ts):
    assert tick(trader, ts=ts) is None
    assert trader.ledger.read_events() == []


@pytest.mark.parametrize("ts", ["2026-09-25T09:45:00-04:00", "2026-09-25T15:30:00-04:00",
                               "2026-09-25T13:45:00Z"])
def test_entry_window_inclusive_et_boundaries(trader, ts):
    assert tick(trader, ts=ts) is None
    assert [e["kind"] for e in trader.ledger.read_events()] == ["entry_intent"]


def test_first_quote_records_only_persistent_intent(trader):
    before = trader.ledger.cash
    assert tick(trader) is None
    events = trader.ledger.read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "entry_intent"
    assert events[0]["card_id"] == "c1"
    assert events[0]["quote_ts"] == "2026-09-25T10:00:00-04:00"
    assert trader.ledger.cash == before
    assert trader.ledger.position("AAPL") is None


def test_next_newer_snapshot_fills_its_price_not_trigger_price(trader):
    assert tick(trader) is None
    # 0.16.1: 后续帧仍须在入场区内 (区外不成交, 意向保留); 成交用该帧价格, 不回填旧价。
    assert tick(trader, price=103., ts="2026-09-25T10:00:30-04:00", at="2026-09-25T10:00:35-04:00") is None
    event = tick(trader, price=101.5, ts="2026-09-25T10:01:00-04:00",
                 at="2026-09-25T10:01:05-04:00")
    assert event["kind"] == "fill"
    assert event["price"] == pytest.approx(round(101.5 * 1.0005, 4))
    assert event["at"] == "2026-09-25T10:01:05-04:00"
    assert event["quote_ts"] == "2026-09-25T10:01:00-04:00"
    assert event["risk_per_share"] == 6.
    assert event["max_holding_days"] == 7


@pytest.mark.parametrize("ts", ["2026-09-25T10:00:00-04:00", "2026-09-25T14:00:00Z",
                               "2026-09-25T09:59:00-04:00"])
def test_duplicate_or_out_of_order_source_timestamp_never_fills(trader, ts):
    tick(trader)
    assert tick(trader, ts=ts, at="2026-09-25T10:02:00-04:00") is None
    assert trader.ledger.position("AAPL") is None
    assert len(trader.ledger.read_events()) == 1


def test_optional_quote_ts_legacy_fallback_uses_at(trader):
    c = card()
    assert trader.try_fill_entry(c, price=101, trade_date=DAY, at="2026-09-25T10:00:00") is None
    assert trader.try_fill_entry(c, price=101, trade_date=DAY, at="2026-09-25T10:00:00") is None
    assert trader.try_fill_entry(c, price=101, trade_date=DAY, at="2026-09-25T10:01:00")["kind"] == "fill"


@pytest.mark.parametrize("price,ts,at", [
    (float("nan"), "2026-09-25T10:00:00-04:00", "2026-09-25T10:00:00-04:00"),
    (float("inf"), "2026-09-25T10:00:00-04:00", "2026-09-25T10:00:00-04:00"),
    (101, "invalid", "2026-09-25T10:00:00-04:00"),
    (101, "2026-09-25T10:02:00-04:00", "2026-09-25T10:00:00-04:00"),
    (101, "2026-09-24T10:00:00-04:00", "2026-09-25T10:00:00-04:00"),
])
def test_bad_or_future_or_other_day_quote_never_records_intent(trader, price, ts, at):
    assert tick(trader, price=price, ts=ts, at=at) is None
    assert trader.ledger.read_events() == []


def test_fill_is_also_gated_by_execution_time(trader):
    tick(trader)
    assert tick(trader, ts="2026-09-25T15:31:00-04:00") is None
    assert trader.ledger.position("AAPL") is None


def test_pending_and_filled_idempotency_survive_restart(trader):
    tick(trader)
    trader = restarted(trader)
    assert tick(trader) is None
    assert tick(trader, ts="2026-09-25T10:01:00-04:00")["kind"] == "fill"
    trader = restarted(trader)
    assert tick(trader, ts="2026-09-25T10:02:00-04:00") is None
    assert tick(trader, card(id="other"), ts="2026-09-25T10:03:00-04:00") is None
    assert len([e for e in trader.ledger.read_events() if e["kind"] == "fill"]) == 1


def test_same_card_cannot_reenter_after_closed_round(trader):
    open_position(trader)
    manage(trader, 120)
    trader = restarted(trader)
    assert tick(trader, ts="2026-09-25T12:00:00-04:00") is None
    assert trader.ledger.position("AAPL") is None


def test_concurrent_traders_share_atomic_check_and_append(trader):
    workers = [SwingTrader(trader.ledger, trader.account) for _ in range(8)]
    barrier = Barrier(len(workers))
    def submit(worker, ts):
        barrier.wait()
        return tick(worker, ts=ts)
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        first = list(pool.map(lambda w: submit(w, "2026-09-25T10:00:00-04:00"), workers))
        second = list(pool.map(lambda w: submit(w, "2026-09-25T10:01:00-04:00"), workers))
    assert all(x is None for x in first)
    assert sum(x is not None for x in second) == 1
    assert [e["kind"] for e in trader.ledger.read_events()] == ["entry_intent", "fill"]


@pytest.mark.parametrize("constraint", ["cash", "positions", "risk"])
def test_fill_rechecks_live_limits_not_only_trigger_limits(trader, constraint):
    tick(trader)
    if constraint == "cash":
        # 权益仍在，但可用现金不足；不是 size_position 的零权益分支。
        trader.ledger.append("fill", dict(symbol="OTHER", side="buy", qty=999, price=100,
                                          fee=0, stop_price=100), at="2026-09-25T10:00:30")
    elif constraint == "positions":
        trader.account = replace(trader.account, max_positions=1)
        trader.ledger.append("fill", dict(symbol="OTHER", side="buy", qty=1, price=100,
                                          fee=0, stop_price=100), at="2026-09-25T10:00:30")
    else:
        trader.account = replace(trader.account, max_portfolio_risk_pct=.015)
        trader.ledger.append("fill", dict(symbol="OTHER", side="buy", qty=100, price=100,
                                          fee=0, stop_price=90), at="2026-09-25T10:00:30")
    assert tick(trader, ts="2026-09-25T10:01:00-04:00") is None
    assert trader.ledger.position("AAPL") is None


def test_rejected_fill_snapshot_cannot_later_be_reused(trader):
    tick(trader)
    trader.account = replace(trader.account, max_portfolio_risk_pct=0)
    assert tick(trader, ts="2026-09-25T10:01:00-04:00") is None
    trader.account = Account()
    trader = restarted(trader)
    assert tick(trader, ts="2026-09-25T10:01:00-04:00") is None
    # 被拦下的意向已消费：必须重新有入区快照登记意向，再由下一帧成交。
    assert tick(trader, ts="2026-09-25T10:02:00-04:00") is None
    assert tick(trader, ts="2026-09-25T10:03:00-04:00")["kind"] == "fill"


def test_blocked_intent_does_not_chase_outside_entry_zone(trader):
    tick(trader)
    trader.account = replace(trader.account, max_portfolio_risk_pct=0)
    assert tick(trader, price=101.5, ts="2026-09-25T10:01:00-04:00") is None  # 区内但容量不足 → blocked
    trader.account = Account()
    # 容量恢复后价格仍在区外：不能拿旧入区信号追高成交。
    assert tick(trader, price=110., ts="2026-09-25T10:02:00-04:00") is None
    assert tick(trader, price=110., ts="2026-09-25T10:03:00-04:00") is None
    assert trader.ledger.position("AAPL") is None
    assert [e["kind"] for e in trader.ledger.read_events()] == ["entry_intent", "entry_intent"]


def test_current_stop_releases_portfolio_risk_capacity(trader):
    trader.account = replace(trader.account, max_portfolio_risk_pct=.015)
    trader.ledger.append("fill", dict(symbol="OTHER", side="buy", qty=100, price=100,
                                      fee=0, stop_price=90), at="2026-09-25T09:50:00")
    tick(trader)
    trader.ledger.append("stop_move", dict(symbol="OTHER", stop_price=100), at="2026-09-25T10:00:30")
    assert tick(trader, ts="2026-09-25T10:01:00-04:00")["kind"] == "fill"


def test_signal_close_only_records_one_exit_intent(trader):
    fill = open_position(trader)
    cash = trader.ledger.cash
    events = trader.manage_close("AAPL", close_price=94, trade_date=DAY, at="2026-09-25T16:30:00")
    assert len(events) == 1 and events[0]["kind"] == "exit_intent"
    assert events[0]["reason"] == "invalidation"
    assert trader.ledger.cash == cash
    assert trader.ledger.position("AAPL").qty == fill["qty"]
    trader = restarted(trader)
    assert trader.manage_close("AAPL", close_price=94, trade_date=DAY, at="2026-09-25T16:31:00") == []


def test_trailing_stop_close_records_intent_not_same_day_sale(trader):
    open_position(trader)
    manage(trader, 108)
    events = trader.manage_close("AAPL", close_price=100, trade_date=DAY, at="2026-09-25T16:30:00")
    assert [e["kind"] for e in events] == ["exit_intent"]
    assert events[0]["reason"] == "trailing_stop"
    assert trader.ledger.position("AAPL") is not None


def test_intraday_wick_below_stop_does_not_exit(trader):
    open_position(trader)
    assert manage(trader, 90) == []
    assert trader.ledger.position("AAPL") is not None


def test_next_open_uses_real_open_with_slippage_restart_and_repeat_safe(trader):
    open_position(trader)
    trader.manage_close("AAPL", close_price=94, trade_date=DAY, at="2026-09-25T16:30:00")
    trader = restarted(trader)
    for day, at in [(DAY, "2026-09-25T16:35:00"), (date(2026, 9, 26), "2026-09-26T09:30:00"),
                    (date(2026, 9, 28), "2026-09-28T09:29:59")]:
        assert trader.execute_pending_exit("AAPL", open_price=85, trade_date=day, at=at) is None
    event = trader.execute_pending_exit("AAPL", open_price=85, trade_date=date(2026, 9, 28), at="2026-09-28T09:30:00")
    assert event["kind"] == "close"
    assert event["price"] == pytest.approx(85 * .9995)
    assert event["reason"] == "invalidation"
    assert event["r_multiple"] < -1.5
    cash = trader.ledger.cash
    trader = restarted(trader)
    assert trader.execute_pending_exit("AAPL", open_price=85, trade_date=date(2026, 9, 28), at="2026-09-28T09:31:00") is None
    assert trader.ledger.cash == cash


def test_target_upper_full_exit_even_without_lower_partial(trader):
    original = open_position(trader)
    events = manage(trader, 116)
    assert len(events) == 1
    assert events[0]["kind"] == "close" and events[0]["reason"] == "target_zone_upper"
    assert events[0]["qty"] == original["qty"]
    assert trader.ledger.position("AAPL") is None


def test_lower_target_halves_once_per_round_even_after_restart(trader):
    original = open_position(trader)
    events = manage(trader, 112.5)
    assert sum(e["kind"] == "partial_close" for e in events) == 1
    qty = trader.ledger.position("AAPL").qty
    assert qty == original["qty"] - original["qty"] // 2
    trader = restarted(trader)
    assert manage(trader, 112.5, "2026-09-25T11:01:00-04:00") == []
    assert trader.ledger.position("AAPL").qty == qty
    assert manage(trader, 116, "2026-09-25T12:00:00-04:00")[0]["kind"] == "close"


def test_lower_target_never_lowers_already_raised_stop(trader):
    open_position(trader, card(target_zone=(114., 120.)))
    manage(trader, 113.5)  # >=2R but below lower target
    stop = trader.ledger.position("AAPL").stop_price
    assert stop > trader.ledger.position("AAPL").avg_cost
    events = manage(trader, 114, "2026-09-25T11:01:00-04:00")
    assert any(e["kind"] == "partial_close" for e in events)
    assert trader.ledger.position("AAPL").stop_price >= stop
    assert all(e["stop_price"] >= stop for e in events if e["kind"] == "stop_move")


def test_concurrent_target_processing_only_halves_once(trader):
    opened = open_position(trader)
    workers = [SwingTrader(trader.ledger, trader.account) for _ in range(8)]
    barrier = Barrier(len(workers))
    def submit(worker):
        barrier.wait()
        return manage(worker, 112.5)
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        list(pool.map(submit, workers))
    assert trader.ledger.position("AAPL").qty == opened["qty"] - opened["qty"] // 2
    assert len([e for e in trader.ledger.read_events() if e["kind"] == "partial_close"]) == 1


def test_seventh_trading_day_1530_forces_exit_with_no_card_book(trader, monkeypatch):
    # 9/28 自定义休市：必须从 calendar 计数，不是裸 weekday 或自然日。
    holiday = date(2026, 9, 28)
    def calendar(start, end):
        return [start + timedelta(days=i) for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) != holiday]
    monkeypatch.setattr(us_clock, "_calendar_provider", calendar)
    monkeypatch.setattr(us_clock, "_cal_cache", {})
    open_position(trader)
    trader = restarted(trader)
    seventh = date(2026, 10, 6)
    assert us_clock.days_held(DAY, seventh) == 7
    assert manage(trader, 102, f"{seventh}T15:29:59-04:00") == []
    events = manage(trader, 102, f"{seventh}T15:30:00-04:00")
    assert len(events) == 1 and events[0]["reason"] == "time_stop"
    assert trader.ledger.position("AAPL") is None


def test_exits_do_not_require_active_card_or_entry_gate(trader):
    open_position(trader)
    trader.register_card(card(state=CardState.CLOSED))
    events = manage(trader, 116, "2026-09-25T15:45:00-04:00")
    assert events[0]["reason"] == "target_zone_upper"


def test_fill_frozen_plan_survives_changed_registered_card(trader):
    open_position(trader)
    trader.register_card(card(risk_per_share=1000, max_holding_days=100, target_zone=(200, 250)))
    assert manage(trader, 116)[0]["reason"] == "target_zone_upper"
