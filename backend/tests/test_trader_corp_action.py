"""拆股后交易员计划价位与台账同因子缩放（fill 冻结值不改，按 corp_action 累乘推导）。"""
from __future__ import annotations

from datetime import date

import pytest

from app.ledger.core import Account, Ledger
from app.opportunity.card import CardState, OpportunityCard
from app.review.review import build_round_trips
from app.trader.trader import SwingTrader

ACCOUNT = Account()
ENTRY_DAY = date(2026, 9, 24)
SPLIT_DAY = date(2026, 9, 25)


def _card(**kw):
    base = dict(id="opp-split-1", symbol="AAPL", setup_type="breakout", created_date=date(2026, 9, 23),
                entry_zone=(100., 102.), invalidation=95., target_zone=(112., 116.),
                max_holding_days=7, risk_per_share=6., reward_risk=2.3, evidence={}, state=CardState.ACTIVE)
    base.update(kw)
    return OpportunityCard(**base)


def _trader(tmp_path, card, factor=2.0):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(ACCOUNT)
    tr = SwingTrader(lg, ACCOUNT)
    tr.register_card(card)
    assert tr.try_fill_entry(card, price=101., trade_date=ENTRY_DAY, at=f"{ENTRY_DAY}T10:00:00") is None
    fill = tr.try_fill_entry(card, price=101., trade_date=ENTRY_DAY, at=f"{ENTRY_DAY}T10:00:01")
    assert fill is not None
    lg.append("corp_action", {"symbol": "AAPL", "factor": factor, "action_id": "split-1",
                              "action_type": "forward_split", "ex_date": SPLIT_DAY.isoformat(),
                              "trade_date": SPLIT_DAY.isoformat()}, at=f"{SPLIT_DAY}T08:00:00")
    return tr, fill


def test_split_close_above_scaled_invalidation_keeps_position(tmp_path):
    tr, _ = _trader(tmp_path, _card())
    # 拆前 100 == 拆后 50；拆后失效位 47.5，不能被旧 95 误判失效
    assert tr.manage_close("AAPL", close_price=50., trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T16:30:00") == []


def test_split_close_below_scaled_invalidation_then_next_open_exit(tmp_path):
    tr, fill = _trader(tmp_path, _card())
    ev = tr.manage_close("AAPL", close_price=47., trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T16:30:00")
    assert ev and ev[0]["kind"] == "exit_intent" and ev[0]["reason"] == "invalidation"
    nxt = date(2026, 9, 28)
    out = tr.execute_pending_exit("AAPL", open_price=46., trade_date=nxt, at=f"{nxt}T09:30:05")
    assert out["kind"] == "close" and out["qty"] == fill["qty"] * 2
    # R 以拆后每股风险 3 与拆后股数计，等价于拆前口径
    pre = ((46 * 2 * (1 - 5e-4) - fill["price"]) * fill["qty"]) / (fill["qty"] * 6)
    assert out["r_multiple"] == pytest.approx(pre, abs=0.02)


def test_split_target_lower_half_and_upper_use_scaled_zone(tmp_path):
    tr, fill = _trader(tmp_path, _card())
    ev = tr.manage_intraday("AAPL", price=56.5, trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T10:30:00")
    parts = [e for e in ev if e["kind"] == "partial_close"]
    assert parts and parts[0]["qty"] == (fill["qty"] * 2) // 2
    ev = tr.manage_intraday("AAPL", price=58.5, trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T10:31:00")
    assert ev and ev[0]["kind"] == "close" and ev[0]["reason"] == "target_zone_upper"


def test_split_trailing_stop_uses_scaled_risk(tmp_path):
    tr, _ = _trader(tmp_path, _card(target_zone=(130., 140.)))
    avg = tr.ledger.position("AAPL").avg_cost
    ev = tr.manage_intraday("AAPL", price=avg + 3.05, trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T10:30:00")
    assert ev and ev[0]["kind"] == "stop_move" and ev[0]["stop_price"] == pytest.approx(avg, abs=1e-3)
    ev = tr.manage_intraday("AAPL", price=avg + 6.1, trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T10:31:00")
    assert ev and ev[0]["stop_price"] == pytest.approx(avg + 3, abs=1e-3)


def test_reverse_split_scales_plan_up(tmp_path):
    tr, _ = _trader(tmp_path, _card(), factor=0.5)
    # 拆前 100 == 反拆后 200；失效位 190，200 收盘不失效，185 失效
    assert tr.manage_close("AAPL", close_price=200., trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T16:30:00") == []
    ev = tr.manage_close("AAPL", close_price=185., trade_date=date(2026, 9, 28), at="2026-09-28T16:30:00")
    assert ev and ev[0]["reason"] == "invalidation"


def test_split_round_trip_pnl_and_r_unchanged(tmp_path):
    tr, fill = _trader(tmp_path, _card())
    ev = tr.manage_intraday("AAPL", price=58.5, trade_date=SPLIT_DAY, at=f"{SPLIT_DAY}T10:31:00")
    close = ev[0]
    trips = build_round_trips(tr.ledger.read_events())
    assert len(trips) == 1
    t = trips[0]
    expected_pnl = close["qty"] * close["price"] - close["fee"] - (fill["qty"] * fill["price"] + fill["fee"])
    assert t.pnl == pytest.approx(round(expected_pnl, 2))
    assert t.planned_risk == pytest.approx(fill["qty"] * 6)
    assert t.r_multiple == pytest.approx(expected_pnl / (fill["qty"] * 6))
    assert close["r_multiple"] == pytest.approx(t.r_multiple, abs=0.01)
    # 原始 fill 事件保持 append-only，不被改写
    first = next(e for e in tr.ledger.read_events() if e["kind"] == "fill")
    assert first["invalidation"] == 95. and first["risk_per_share"] == 6.
