"""Paper coordinator production contract; fake quotes + temp ledger only."""
from datetime import date,datetime,timedelta
from types import SimpleNamespace
import polars as pl
import pytest
from app.market_clock import us_clock
from app.ledger.core import Account
from app.opportunity.card import OpportunityCard,CardState
from app.opportunity.engine import ScanResult
from app.services.trading_loop import TradingLoop

DAY=date(2026,9,22)
class Repo:
    def __init__(self,p):self.store=SimpleNamespace(data_dir=p)
    def get_instruments(self):return pl.DataFrame({'symbol':['TEST'],'sector':['Technology']})
    def get_daily(self,s,start,end):return pl.DataFrame({'date':[end],'close':[100.],'open':[100.],'high':[102.],'low':[98.],'volume':[1e6]})

@pytest.fixture
def loop(tmp_path,monkeypatch):
    monkeypatch.setattr(us_clock,'_calendar_provider',None)
    x=TradingLoop(Repo(tmp_path),Account());x.boot()
    c=OpportunityCard('pending-TEST','TEST','breakout',date(2026,9,21),(100.,102.),95.,(110.,115.),risk_per_share=6.,reward_risk=2.,state=CardState.OPEN,evidence={'sector':'Technology'})
    x.opp_svc.save_scan(ScanResult(c.created_date,cards=[c],gate='green'))
    return x

def q(now,price=101):
    return {'TEST':{'symbol':'TEST','price':price,'timestamp':int(now.timestamp()*1000),'open':100.,'open_timestamp':int(now.replace(hour=9,minute=30,second=0,microsecond=0).timestamp()*1000),'feed':'iex'}}

def test_loop_discards_stale_and_unstamped_price(loop,monkeypatch):
    now=datetime(2026,9,22,10,tzinfo=us_clock.tz);monkeypatch.setattr(us_clock,'now',lambda:now)
    loop.intraday_fill_check({'TEST':101.})
    loop.intraday_fill_check(q(now-timedelta(minutes=16)))
    assert loop.ledger.open_positions==[]
    assert not any(e['kind']=='entry_intent' for e in loop.ledger.read_events())

def test_loop_lifecycle_next_quote_and_restart_dedup(loop,monkeypatch):
    now=datetime(2026,9,22,10,tzinfo=us_clock.tz);monkeypatch.setattr(us_clock,'now',lambda:now)
    loop.intraday_fill_check(q(now))
    assert loop.ledger.open_positions==[]
    now+=timedelta(seconds=15)
    loop.intraday_fill_check(q(now))
    assert len(loop.ledger.open_positions)==1
    loop.intraday_fill_check(q(now))
    restored=TradingLoop(loop.repo,Account());restored.boot()
    now+=timedelta(seconds=15)
    restored.intraday_fill_check(q(now))
    assert len([e for e in restored.ledger.read_events() if e['kind']=='fill'])==1
    assert restored.opp_svc.load_cards(date(2026,9,21))['cards'][0]['state']=='filled'

def test_no_price_requests_outside_session(loop,monkeypatch):
    now=datetime(2026,9,22,7,tzinfo=us_clock.tz);monkeypatch.setattr(us_clock,'now',lambda:now)
    class NoNetwork:
        def get_execution_quotes(self,symbols):raise AssertionError('outside session')
    loop.run_intraday(NoNetwork())
    assert loop.status()['last_error'] is None

def test_target_pool_contains_waiting_cards(loop):
    assert loop.execution_symbols()==['TEST']
