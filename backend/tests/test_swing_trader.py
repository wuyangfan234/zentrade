"""Paper trader regressions aligned with docs/architecture.md.

Old tests treated same-frame entry, weekend fills, close-price invalidation exits,
and surviving the upper target as correct. Fixtures now exercise two snapshots,
regular ET sessions, and a real next-session open without weakening cash/exit assertions.
"""
from datetime import date, timedelta
import pytest
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.trader.trader import SwingTrader

ACCOUNT = Account()


def _card(**kw):
    base = dict(id="opp-test-1", symbol="AAPL", setup_type="breakout", created_date=date(2026,9,24),
                entry_zone=(100.,102.), invalidation=95., target_zone=(112.,116.),
                max_holding_days=7, risk_per_share=6., reward_risk=2.3, evidence={}, state=CardState.ACTIVE)
    base.update(kw)
    return OpportunityCard(**base)


@pytest.fixture
def trader(tmp_path):
    lg=Ledger(data_dir=tmp_path/"ledger");lg.init_account(ACCOUNT)
    return SwingTrader(lg,ACCOUNT)


def _fill(trader,card,price=101.,day=date(2026,9,25)):
    trader.register_card(card)
    first=trader.try_fill_entry(card,price=price,trade_date=day,at=f"{day}T10:00:00")
    assert first is None
    return trader.try_fill_entry(card,price=price,trade_date=day,at=f"{day}T10:00:01")


def _open(trader,card=None,price=101.,day=date(2026,9,25)):
    card=card or _card()
    assert _fill(trader,card,price,day) is not None
    return card


def intraday(trader,price,day=date(2026,9,28),time="10:30:00"):
    return trader.manage_intraday("AAPL",price=price,trade_date=day,at=f"{day}T{time}")


def closed(trader,price,day=date(2026,9,28)):
    return trader.manage_close("AAPL",close_price=price,trade_date=day,at=f"{day}T16:30:00")


def test_entry_fills_inside_zone(trader):
    e=_fill(trader,_card())
    assert e is not None and e['qty']>0
    assert e['price']==pytest.approx(101*1.0005)
    assert e['stop_price']==95 and e['card_id']=='opp-test-1'
    assert trader.ledger.position('AAPL').qty==e['qty']


def test_entry_rejects_outside_zone(trader):
    assert _fill(trader,_card(),99) is None
    assert _fill(trader,_card(),103) is None


def test_entry_rejects_gap_beyond_2pct(trader):
    assert _fill(trader,_card(),102*1.021) is None


def test_entry_rejects_closed_card(trader):
    assert _fill(trader,_card(state=CardState.CLOSED)) is None


def test_entry_sizing_risk_budget(trader):
    e=_fill(trader,_card())
    # 单笔风险上限 (2026-10-12): 按实际成交价 (含 5bps 滑点) 到失效价的距离重算, 实际风险 ≤ 预算
    actual_rps = round(101*(1+ACCOUNT.slippage_bps/10000),4) - 95.
    assert e['qty']==min(int(100000*0.01/actual_rps),int(100000*0.25/101))
    assert e['qty']*actual_rps <= 100000*0.01 and e['risk_cap']['planned_qty']==166


def test_entry_respects_max_positions(trader):
    for i in range(ACCOUNT.max_positions):
        c=_card(id=f'c{i}',symbol=f'S{i}',risk_per_share=20.,entry_zone=(100.,101.),invalidation=90.,target_zone=(140.,145.))
        assert _fill(trader,c,100.5) is not None
    assert _fill(trader,_card(id='cx',symbol='XXX',risk_per_share=20.,entry_zone=(100.,101.),invalidation=90.,target_zone=(140.,145.)),100.5) is None


def test_entry_insufficient_cash(trader):
    trader.ledger._cash=1.
    big=_card(entry_zone=(9000.,9100.),invalidation=8900.,risk_per_share=100.,target_zone=(9400.,9500.))
    assert _fill(trader,big,9050.) is None


def test_manage_no_position_no_events(trader):
    assert trader.manage_close('NOPE',close_price=100.,trade_date=date(2026,9,28),at='2026-09-28T16:30:00')==[]


def test_time_stop_hard_exit_at_day_7(trader):
    _open(trader)
    d=date(2026,9,25)
    while us_clock.days_held(date(2026,9,25),d)<7:d+=timedelta(days=1)
    events=intraday(trader,105.,d,'15:30:00')
    assert len(events)==1 and events[0]['kind']=='close' and events[0]['reason']=='time_stop'
    assert trader.ledger.position('AAPL') is None


def test_invalidation_close_breaks(trader):
    _open(trader)
    events=closed(trader,94.)
    assert events[0]['kind']=='exit_intent' and events[0]['reason']=='invalidation'
    assert trader.ledger.position('AAPL') is not None
    event=trader.execute_pending_exit('AAPL',open_price=93.,trade_date=date(2026,9,29),at='2026-09-29T09:30:00')
    assert event['reason']=='invalidation' and trader.ledger.position('AAPL') is None


def test_target_zone_partial_and_breakeven_stop(trader):
    _open(trader)
    events=intraday(trader,112.5)
    assert 'partial_close' in [e['kind'] for e in events]
    stop=next(e for e in events if e['kind']=='stop_move')
    assert stop['stop_price']==pytest.approx(trader.ledger.position('AAPL').avg_cost)
    assert trader.ledger.position('AAPL') is not None


def test_trailing_stop_only_moves_up(trader):
    _open(trader);closed(trader,108.)
    pos=trader.ledger.position('AAPL');stop=pos.stop_price
    assert stop>=pos.avg_cost-0.001
    closed(trader,102.,date(2026,9,29))
    assert trader.ledger.position('AAPL').stop_price==stop


def test_trailing_stop_exit(trader):
    _open(trader);closed(trader,108.)
    stop=trader.ledger.position('AAPL').stop_price
    events=closed(trader,stop-.5,date(2026,9,29))
    assert any(e['reason']=='trailing_stop' for e in events)
    assert trader.ledger.position('AAPL') is not None
    event=trader.execute_pending_exit('AAPL',open_price=99.,trade_date=date(2026,9,30),at='2026-09-30T09:30:00')
    assert event['reason']=='trailing_stop' and trader.ledger.position('AAPL') is None


def test_gap_loss_recorded_honestly(trader):
    _open(trader);closed(trader,94.)
    event=trader.execute_pending_exit('AAPL',open_price=85.,trade_date=date(2026,9,29),at='2026-09-29T09:30:00')
    assert event['r_multiple'] < -1.5


def test_full_lifecycle_entry_to_target_close(trader):
    _open(trader);intraday(trader,113.)
    assert trader.ledger.position('AAPL') is not None
    events=intraday(trader,116.,date(2026,9,29))
    assert len(events)==1 and events[0]['kind']=='close' and events[0]['reason']=='target_zone_upper'
    assert trader.ledger.position('AAPL') is None


def test_ledger_replay_consistency_after_trades(trader,tmp_path):
    _open(trader);intraday(trader,112.5)
    other=Ledger(data_dir=tmp_path/'ledger');other.replay(ACCOUNT)
    assert other.cash==pytest.approx(trader.ledger.cash)
    assert [(p.symbol,p.qty) for p in other.open_positions]==[(p.symbol,p.qty) for p in trader.ledger.open_positions]
