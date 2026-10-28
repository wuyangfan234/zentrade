"""现金不足按剩余可用资金缩量成交 (无限 2026-10-05 拍板「按剩余现金缩小仓位成交」)。"""
from dataclasses import replace
from datetime import date

import pytest

from app.ledger.core import Account, Ledger
from app.opportunity.card import CardState, OpportunityCard
from app.review.review import build_round_states
from app.trader.sizing import MIN_CASH_DOWNSIZE_RATIO, downsize_for_cash, fee_for
from app.trader.trader import SwingTrader

DAY = date(2026, 9, 25)


def card(**kw):
    data = dict(id="c1", symbol="AAPL", setup_type="breakout", created_date=DAY, entry_zone=(100., 102.),
                invalidation=95., target_zone=(112., 116.), risk_per_share=6., reward_risk=2.3, max_holding_days=7,
                state=CardState.ACTIVE)
    data.update(kw)
    return OpportunityCard(**data)


@pytest.fixture
def trader(tmp_path):
    account = Account()
    ledger = Ledger(tmp_path / "ledger")
    ledger.init_account(account)
    ledger.replay(account)
    return SwingTrader(ledger, account)


def tick(trader, c, price=101., ts="2026-09-25T10:00:00-04:00"):
    return trader.try_fill_entry(c, price=price, trade_date=DAY, at=ts, quote_ts=ts)


def spend(trader, cash_left):
    """用一笔无关持仓把现金压到 cash_left (止损=成本, 不占组合风险)。"""
    qty = int((trader.ledger.cash - cash_left) // 100)
    trader.ledger.append("fill", dict(symbol="OTHER", side="buy", qty=qty, price=100, fee=0, stop_price=100),
                         at="2026-09-25T09:50:00")


def test_downsize_helper_fits_fee_and_never_exceeds_plan():
    acc = Account()
    assert downsize_for_cash(166, 101.05, 10_000., acc) == 98  # 98×101.05 + $1 ≤ 10000
    assert downsize_for_cash(50, 101.05, 1e9, acc) == 50       # 现金足够时不放大
    assert downsize_for_cash(166, 101.05, 50., acc) == 0
    n = downsize_for_cash(5000, 3.0, 9_000., acc)
    assert n * 3.0 + fee_for(n, acc) <= 9_000. < (n + 1) * 3.0 + fee_for(n + 1, acc)


def test_insufficient_cash_fills_downsized_and_records_plan(trader):
    spend(trader, 10_000.)
    c = card()
    assert tick(trader, c) is None
    fill = tick(trader, c, ts="2026-09-25T10:01:00-04:00")
    assert fill is not None and fill["kind"] == "fill"
    # 单笔风险上限 (2026-10-12): 按成交价 101.0505 到失效 95 重算, $1000 / 6.0505 = 165 股 (原按 $6 为 166)
    planned = 165
    assert fill["qty"] < planned and fill["qty"] >= planned * MIN_CASH_DOWNSIZE_RATIO
    assert fill["qty"] * fill["price"] + fill["fee"] <= 10_000. + 1e-6
    assert fill["sizing"] == {"planned_qty": planned, "qty": fill["qty"], "reason": "cash_downsized",
                              "available_cash": pytest.approx(10_000., abs=1), "ratio": round(fill["qty"] / planned, 4)}
    # 冻结的每股风险不变 → 回合 R 仍按实际股数 × 每股风险计算, 与满额单可比
    assert fill["risk_per_share"] == 6.0


def test_downsize_below_quarter_of_plan_stays_blocked(trader):
    spend(trader, 3_000.)  # 只够 29 股 < 166×25%=41.5
    c = card()
    assert tick(trader, c) is None
    assert tick(trader, c, ts="2026-09-25T10:01:00-04:00") is None
    last = trader.ledger.read_events()[-1]
    assert last["kind"] == "entry_intent" and last["status"] == "blocked" and last["reason"] == "cash"
    assert trader.ledger.position("AAPL") is None


def test_full_cash_keeps_full_size_without_sizing_note(trader):
    c = card()
    tick(trader, c)
    fill = tick(trader, c, ts="2026-09-25T10:01:00-04:00")
    assert fill["qty"] == 165 and "sizing" not in fill          # 165 = 风险上限压缩, 不是现金缩量
    assert fill["risk_cap"]["planned_qty"] == 166


def test_downsized_short_uses_same_net_buying_power(trader):
    spend(trader, 10_000.)
    c = card(id="s1", symbol="XYZ", side="short", setup_type="strategy:phantom_4h_short", entry_zone=(99., 100.),
             invalidation=106., target_zone=(86., 88.))
    assert tick(trader, c, price=99.5) is None
    fill = tick(trader, c, price=99.5, ts="2026-09-25T10:01:00-04:00")
    assert fill is not None and fill["side"] == "short" and fill["sizing"]["reason"] == "cash_downsized"
    assert fill["qty"] * fill["price"] + fill["fee"] <= 10_000. + 1e-6


def test_portfolio_risk_checked_on_downsized_quantity(trader):
    spend(trader, 10_000.)
    trader.account = replace(trader.account, max_portfolio_risk_pct=0.006)  # 满额 166×6≈$1000 超 0.6%, 缩量 98×6≈$590 不超
    c = card()
    tick(trader, c)
    fill = tick(trader, c, ts="2026-09-25T10:01:00-04:00")
    assert fill is not None and fill["sizing"]["qty"] == fill["qty"]


def test_downsized_round_trip_r_uses_actual_quantity(trader):
    spend(trader, 10_000.)
    c = card()
    trader.register_card(c)
    tick(trader, c)
    fill = tick(trader, c, ts="2026-09-25T10:01:00-04:00")
    trader.ledger.append("close", dict(symbol="AAPL", qty=fill["qty"], price=fill["price"] + 6.0, fee=1.0,
                                       reason="target_zone_upper", card_id="c1"), at="2026-09-26T11:00:00")
    closed, _ = build_round_states(trader.ledger.read_events())
    trip = [s for s in closed if s.first["symbol"] == "AAPL"][0].closed(trader.ledger.read_events()[-1])
    assert trip.planned_risk == pytest.approx(fill["qty"] * 6.0)
    assert 0.9 < trip.r_multiple < 1.0  # +1R 毛利减去两次佣金
