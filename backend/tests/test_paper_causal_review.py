"""Reproduce independent review P1 findings using real temp ledgers (no network)."""
from datetime import date,datetime,timedelta
from types import SimpleNamespace
import polars as pl
import pytest
from app.ledger.core import Account,Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState,OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.trading_loop import TradingLoop
from app.trader.trader import SwingTrader


@pytest.fixture
def rig(tmp_path,monkeypatch):
    clock=[datetime(2026,9,30,11,tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock,'now',lambda:clock[0])
    monkeypatch.setattr(us_clock,'_calendar_provider',None)
    repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda:pl.DataFrame({'symbol':['AAPL'],'sector':['Technology']}),
        get_daily=lambda sym,start,end:pl.DataFrame({'date':[end],'close':[80.],'raw_close':[80.]}))
    loop=TradingLoop(repo,Account());loop.boot()
    card=OpportunityCard('review-AAPL','AAPL','breakout',date(2026,9,29),
        (99.,102.),94.,(110.,120.),risk_per_share=6.,reward_risk=2.,
        evidence={'sector':'Technology'},state=CardState.OPEN)
    loop.opp_svc.save_scan(ScanResult(card.created_date,cards=[card],gate='green'))
    return loop,card,clock


def quote(at,price=100.,opening=100.):
    row={'symbol':'AAPL','price':price,'timestamp':at.isoformat(),'feed':'iex'}
    if opening is not None:
        row.update(open=opening,open_timestamp=at.replace(hour=9,minute=30,second=0).isoformat())
    return {'AAPL':row}


@pytest.mark.parametrize('state',[CardState.OPEN,CardState.ACTIVE,CardState.WATCH])
def test_missing_open_never_allows_entry_then_gap_expires(rig,state):
    loop,card,clock=rig
    if state!=CardState.OPEN:
        loop.ledger.append('opportunity',{'card_id':card.id,'symbol':card.symbol,'state':str(state),'reason':'fixture'},at=clock[0].isoformat())
    for _ in range(2):
        loop.intraday_fill_check(quote(clock[0],opening=None))
        clock[0]+=timedelta(seconds=15)
    assert not loop.ledger.open_positions
    assert not any(e['kind']=='entry_intent' for e in loop.ledger.read_events())
    # True open arrives late and proves the opening gap violated the proposal.
    loop.intraday_fill_check(quote(clock[0],opening=110.))
    assert loop.opp_svc.load_cards(card.created_date)['cards'][0]['state']=='expired'
    assert not loop.ledger.open_positions


@pytest.mark.parametrize('opening',[0.,-1.,float('nan'),float('inf')])
def test_invalid_open_cannot_bypass_entry_guard(rig,opening):
    loop,_,clock=rig
    for _ in range(2):
        loop.intraday_fill_check(quote(clock[0],opening=opening))
        clock[0]+=timedelta(seconds=15)
    assert not loop.ledger.open_positions


@pytest.mark.parametrize('restore',[False,True])
def test_next_quote_must_follow_intent_decision_not_only_previous_source_frame(rig,restore):
    loop,card,clock=rig
    active=card.with_state(CardState.ACTIVE)
    trader=loop.trader
    trader.try_fill_entry(active,price=100.,trade_date=clock[0].date(),at=clock[0].isoformat(),quote_ts=(clock[0]-timedelta(seconds=60)).isoformat())
    if restore:
        ledger=Ledger(loop.ledger.data_dir);ledger.replay(loop.account)
        trader=SwingTrader(ledger,loop.account)
    decision=clock[0]
    for seconds_before in [30,0]:
        clock[0]+=timedelta(seconds=10)
        event=trader.try_fill_entry(active,price=101.,trade_date=clock[0].date(),at=clock[0].isoformat(),quote_ts=(decision-timedelta(seconds=seconds_before)).isoformat())
        assert event is None
    assert not trader.ledger.open_positions
    clock[0]+=timedelta(seconds=10)
    event=trader.try_fill_entry(active,price=101.,trade_date=clock[0].date(),at=clock[0].isoformat(),quote_ts=(decision+timedelta(seconds=1)).isoformat())
    assert event['kind']=='fill'


def filled_rig(rig):
    loop,card,clock=rig
    loop.intraday_fill_check(quote(clock[0]))
    clock[0]+=timedelta(seconds=15)
    loop.intraday_fill_check(quote(clock[0]))
    assert loop.ledger.open_positions
    return loop,card,clock


def test_past_close_cannot_touch_later_position_or_write_historical_nav(rig):
    loop,card,clock=filled_rig(rig)
    snapshot=loop.ledger.snapshot()
    result=loop.post_close_manage(date(2026,9,29),confirmed_official=True)
    assert result['events']==0
    assert result['nav'] is None
    assert loop.ledger.snapshot()==snapshot
    clock[0]+=timedelta(seconds=15)
    loop.intraday_fill_check(quote(clock[0],price=100.,opening=90.))
    assert loop.ledger.open_positions


def test_trader_itself_ignores_close_before_entry(rig):
    loop,card,clock=filled_rig(rig)
    prior=date(2026,9,29)
    assert loop.trader.manage_close(card.symbol,close_price=80.,trade_date=prior,at=datetime(2026,9,29,16,tzinfo=us_clock.tz).isoformat())==[]


def test_persisted_bad_exit_intent_cannot_sell_at_open_before_entry(rig):
    loop,card,clock=filled_rig(rig)
    loop.ledger.append('exit_intent',{'symbol':card.symbol,'card_id':card.id,'trade_date':'2026-09-29','reason':'invalidation'},at='2026-09-29T16:00:00-04:00')
    assert loop.trader.execute_pending_exit(card.symbol,open_price=90.,trade_date=clock[0].date(),at=clock[0].isoformat()) is None
    assert loop.ledger.open_positions


def test_missing_open_does_not_block_existing_position_exit(rig):
    loop,card,clock=filled_rig(rig)
    clock[0]+=timedelta(seconds=15)
    events=loop.intraday_fill_check(quote(clock[0],price=121.,opening=None))
    assert any(e['kind']=='close' and e['reason']=='target_zone_upper' for e in events)
    assert not loop.ledger.open_positions


def test_older_bad_intent_does_not_poison_a_valid_future_close(rig):
    loop,card,clock=filled_rig(rig)
    loop.ledger.append('exit_intent',{'symbol':card.symbol,'card_id':card.id,'trade_date':'2026-09-29','reason':'invalidation'},at='2026-09-29T16:00:00-04:00')
    clock[0]=clock[0].replace(hour=16,minute=35,second=0)
    result=loop.post_close_manage(clock[0].date(),confirmed_official=True)
    assert result['events']==1
    clock[0]+=timedelta(days=1)
    clock[0]=clock[0].replace(hour=9,minute=45)
    loop.intraday_fill_check(quote(clock[0],price=91.,opening=90.))
    assert not loop.ledger.open_positions


def test_old_close_cannot_lower_decision_time_after_newer_stop_move(rig):
    loop,card,clock=filled_rig(rig)
    clock[0]=datetime(2026,10,1,11,tzinfo=us_clock.tz)
    loop.intraday_fill_check(quote(clock[0],price=111.,opening=105.))
    before=loop.ledger.snapshot()
    result=loop.post_close_manage(date(2026,9,30),confirmed_official=True)
    assert result['nav'] is None
    assert loop.ledger.snapshot()==before
