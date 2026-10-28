"""Opening a new position must reserve against current short liabilities, not entry cost."""
from datetime import date, datetime, timedelta

import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.trader.trader import SwingTrader


def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, '_calendar_provider', None)
    account = Account(commission_per_share=0, min_commission=0, slippage_bps=0)
    ledger = Ledger(tmp_path)
    ledger.init_account(account)
    now = datetime(2026, 10, 5, 10, tzinfo=us_clock.tz)
    for sym, side, stop in [('SHORT', 'short', 120), ('A', 'buy', 99), ('B', 'buy', 99), ('C', 'buy', 99)]:
        ledger.append('fill', dict(symbol=sym, side=side, qty=200, price=100, fee=0,
                      stop_price=stop, card_id=sym, trade_date=str(now.date())), at=now.isoformat())
    card = OpportunityCard(id='new', symbol='NEW', setup_type='pullback', created_date=date(2026, 10, 2),
                           entry_zone=(99, 101), invalidation=94.5, target_zone=(115, 117),
                           risk_per_share=5.5, reward_risk=3, state=CardState.ACTIVE)
    return ledger, SwingTrader(ledger, account), card, now


def test_current_short_liability_sizes_new_position_from_marked_not_cost_buying_power(tmp_path, monkeypatch):
    # 2026-10-05 起现金不足缩量成交; 缩量依据必须是按现价估值的空头负债 (1.6 万), 不是按成本的 2 万。
    led, trader, card, now = rig(tmp_path, monkeypatch)
    marks = dict(SHORT=110., A=100., B=100., C=100., NEW=100.)
    assert led.buying_power() == 20_000
    assert led.buying_power(marks) == 16_000
    assert trader.try_fill_entry(card, price=100, trade_date=now.date(), at=now.isoformat(), marks=marks) is None
    result = trader.try_fill_entry(card, price=100, trade_date=now.date(), at=(now+timedelta(seconds=15)).isoformat(), marks=marks)
    assert result is not None and result['sizing']['reason'] == 'cash_downsized'
    assert result['qty'] == 160                       # 16,000 / $100 (本账户零佣金零滑点); 按成本口径会是 178
    assert result['sizing']['available_cash'] == 16_000
    assert result['sizing']['planned_qty'] == 178     # 净值 98,000 × 1% / 5.5
    assert led.buying_power(marks) == 0


def test_current_short_liability_blocks_when_marked_cash_under_quarter_of_plan(tmp_path, monkeypatch):
    led, trader, card, now = rig(tmp_path, monkeypatch)
    marks = dict(SHORT=195., A=100., B=100., C=100., NEW=100.)   # 空头浮亏: 可用资金 60,000 − 2×39,000 < 0
    assert led.buying_power(marks) < 0
    assert trader.try_fill_entry(card, price=100, trade_date=now.date(), at=now.isoformat(), marks=marks) is None
    assert trader.try_fill_entry(card, price=100, trade_date=now.date(), at=(now+timedelta(seconds=15)).isoformat(), marks=marks) is None
    assert led.position('NEW') is None and led.read_events()[-1]['reason'] == 'cash'


@pytest.mark.parametrize('bad', [None, {}, {'SHORT': float('nan')}, {'SHORT': 100., 'A': 100., 'B': 100.}])
def test_missing_or_invalid_current_valuation_never_opens_with_short_exposure(tmp_path, monkeypatch, bad):
    led, trader, card, now = rig(tmp_path, monkeypatch)
    for offset in (0, 15):
        assert trader.try_fill_entry(card, price=100, trade_date=now.date(), at=(now+timedelta(seconds=offset)).isoformat(), marks=bad) is None
    assert led.position('NEW') is None


def test_marks_do_not_change_exit_when_cash_is_insufficient(tmp_path, monkeypatch):
    led, trader, card, now = rig(tmp_path, monkeypatch)
    # A buyback is risk reducing and must proceed even if it costs more than available cash.
    pos = led.position('SHORT')
    plan = {'seq':1, 'risk_per_share':20., 'card_id':'SHORT'}
    ev = trader._close_all(pos, 400., now.date(), now.isoformat(), reason='time_stop', plan=plan)
    assert ev['position_side'] == 'short' and led.position('SHORT') is None
    assert led.cash < 0
