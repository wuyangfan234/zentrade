"""Frozen opportunity proposals + append-only lifecycle, offline."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import json
import polars as pl
import pytest
from app.ledger.core import Ledger, Account
from app.market_clock import us_clock
from app.opportunity.card import OpportunityCard,CardState
from app.opportunity.engine import ScanResult, load_params, scan, universe_filter
from app.services.opportunity_service import OpportunityService

DAY=date(2026,9,21)

def card(symbol='AAPL', state=CardState.OPEN):
    return OpportunityCard('test-'+symbol,symbol,'breakout',DAY,(100.,102.),95.,(116.,120.),risk_per_share=6.,reward_risk=3.,state=state,evidence={'sector':'Technology'})

@pytest.fixture
def svc(tmp_path,monkeypatch):
    monkeypatch.setattr(us_clock,'_calendar_provider',None)
    ledger=Ledger(tmp_path/'ledger');ledger.init_account(Account())
    service=OpportunityService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service.bind_ledger(ledger)
    service.save_scan(ScanResult(DAY,cards=[card()]))
    return service,ledger


def test_entry_window_expires_without_rewriting_proposal(svc):
    service,ledger=svc
    original=service._cards_path(DAY).read_bytes()
    service.advance_lifecycle(datetime(2026,9,29,10,tzinfo=us_clock.tz),{})
    got=service.load_cards(DAY)['cards'][0]
    assert got['state']=='expired'
    assert service._cards_path(DAY).read_bytes()==original
    assert ledger.read_events()[-1]['kind']=='opportunity'
    count=len(ledger.read_events())
    service.advance_lifecycle(datetime(2026,9,29,11,tzinfo=us_clock.tz),{})
    assert len(ledger.read_events())==count


def test_open_becomes_active_only_next_trading_session(svc):
    service,_=svc
    service.advance_lifecycle(datetime(2026,9,21,15,tzinfo=us_clock.tz),{'AAPL':{'price':101,'open':100}})
    assert service.load_cards(DAY)['cards'][0]['state']=='open'
    service.advance_lifecycle(datetime(2026,9,22,9,45,tzinfo=us_clock.tz),{'AAPL':{'price':101,'open':100}})
    assert service.load_cards(DAY)['cards'][0]['state']=='active'


def test_gap_up_expires_and_unknown_sector_does_not_activate(svc):
    service,_=svc
    service.advance_lifecycle(datetime(2026,9,22,10,tzinfo=us_clock.tz),{'AAPL':{'price':106,'open':105}})
    assert service.load_cards(DAY)['cards'][0]['state']=='expired'


def test_fills_and_closes_project_state_and_cooldown(svc):
    service,ledger=svc
    ledger.append('fill',{'symbol':'AAPL','side':'buy','qty':10,'price':101,'fee':1,'card_id':'test-AAPL','trade_date':'2026-09-22','stop_price':95},at='2026-09-22T10:00:00-04:00')
    assert service.load_cards(DAY)['cards'][0]['state']=='filled'
    ledger.append('close',{'symbol':'AAPL','qty':10,'price':115,'fee':1,'card_id':'test-AAPL','trade_date':'2026-09-23'},at='2026-09-23T10:00:00-04:00')
    assert service.load_cards(DAY)['cards'][0]['state']=='closed'
    assert 'AAPL' in service.cooldown_symbols(date(2026,9,25))
    assert 'AAPL' not in service.cooldown_symbols(date(2026,9,29))


def test_rescan_preserves_frozen_card_and_removes_duplicate_watch(svc):
    service,_=svc
    changed=OpportunityCard.from_dict({**card().to_dict(),'entry_zone':[99,100]})
    service.save_scan(ScanResult(DAY,cards=[changed],watch=[card().with_state(CardState.WATCH)]))
    payload=service.load_cards(DAY)
    assert payload['cards'][0]['entry_zone']==[100.,102.]
    assert payload['watch']==[]


def test_no_future_bars_enter_scan():
    params=load_params()
    future=pl.DataFrame({'date':[date(2026,10,1)],'close':[500.],'high':[600.],'low':[400.],'volume':[1e8]})
    result=scan({'AAPL':future},future,params,DAY)
    assert result.cards==[]
    assert result.gate!='green'


def test_pinned_low_atr_prices_excluded():
    params=load_params()
    n=150
    pinned=pl.DataFrame({'close':[100.]*n,'high':[100.1]*n,'low':[99.9]*n,'volume':[1e7]*n})
    assert not universe_filter(pinned,params)
