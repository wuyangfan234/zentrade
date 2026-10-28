"""corp_action 台账事件：追加边界校验 + 物化数值（拆股/反拆/现金分红）。"""
from __future__ import annotations

import math

import pytest

from app.ledger.core import Account, Ledger


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    lg.append("fill", {"symbol": "AVGO", "side": "buy", "qty": 15, "price": 1600.0, "fee": 1.0,
                       "trade_date": "2024-07-12", "stop_price": 1500.0}, at="2024-07-12T10:00:00-04:00")
    return lg


@pytest.mark.parametrize("payload", [
    {"symbol": "AVGO", "factor": 0},
    {"symbol": "AVGO", "factor": -2},
    {"symbol": "AVGO", "factor": math.nan},
    {"symbol": "AVGO", "factor": "x"},
    {"symbol": "AVGO", "cash_dividend": -5.0},
    {"symbol": "AVGO", "cash_dividend": math.inf},
    {"symbol": "", "factor": 10},
])
def test_corp_action_rejects_invalid_numbers_before_write(ledger, payload):
    before = ledger.read_events()
    with pytest.raises(ValueError):
        ledger.append("corp_action", payload, at="2024-07-15T08:00:00-04:00")
    assert ledger.read_events() == before


def test_corp_action_duplicate_action_id_rejected(ledger):
    ledger.append("corp_action", {"symbol": "AVGO", "factor": 10, "action_id": "a1"}, at="2024-07-15T08:00:00-04:00")
    with pytest.raises(ValueError):
        ledger.append("corp_action", {"symbol": "AVGO", "factor": 10, "action_id": "a1"}, at="2024-07-15T08:01:00-04:00")
    assert ledger.position("AVGO").qty == 150


def test_forward_split_scales_qty_cost_stop_and_keeps_nav(ledger):
    nav_before = ledger.nav({"AVGO": 1700.0})
    ledger.append("corp_action", {"symbol": "AVGO", "factor": 10, "action_id": "s1"}, at="2024-07-15T08:00:00-04:00")
    pos = ledger.position("AVGO")
    assert pos.qty == 150
    assert pos.stop_price == pytest.approx(150.0)
    assert ledger.nav({"AVGO": 170.0}) == pytest.approx(nav_before)


def test_reverse_split_scales_down(ledger):
    ledger.append("corp_action", {"symbol": "AVGO", "factor": 0.05, "action_id": "r1"}, at="2024-07-15T08:00:00-04:00")
    pos = ledger.position("AVGO")
    assert pos.qty == pytest.approx(0.75)
    assert pos.avg_cost == pytest.approx((15 * 1600 + 1) / 15 / 0.05)


def test_cash_dividend_credits_cash_only(ledger):
    cash = ledger.cash
    ledger.append("corp_action", {"symbol": "AVGO", "cash_dividend": 78.75, "action_id": "d1"}, at="2024-06-24T08:00:00-04:00")
    assert ledger.cash == pytest.approx(cash + 78.75)
    assert ledger.position("AVGO").qty == 15
