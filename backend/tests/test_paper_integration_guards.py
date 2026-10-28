"""Production integration guards; all data and ledgers are temporary fixtures."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import polars as pl
import pytest
from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult, load_params
from app.services.trading_loop import TradingLoop
from app.services.opportunity_service import OpportunityService
from app.jobs import daily_pipeline

DAY = date(2026, 9, 29)


class Repo:
    def __init__(self, p): self.store = SimpleNamespace(data_dir=p)
    def get_instruments(self): return pl.DataFrame({'symbol':['TEST'],'sector':['Technology']})
    def get_daily(self,s,start,end): return pl.DataFrame({'date':[end],'close':[100.], 'raw_close':[99.]})
    def get_daily_batch(self,symbols,start,end):
        n=200
        return pl.DataFrame({'symbol':['TEST']*n,'date':[end-timedelta(days=n-1-i) for i in range(n)],'open':[20.]*n,'high':[20.001]*n,'low':[19.999]*n,'close':[20.]*n,'volume':[2_000_000.]*n})


@pytest.fixture
def loop(tmp_path,monkeypatch):
    monkeypatch.setattr(us_clock,'now',lambda:datetime(2026,9,29,16,40,tzinfo=us_clock.tz))
    x=TradingLoop(Repo(tmp_path),Account());x.boot()
    x.ledger.append('fill',{'symbol':'TEST','side':'buy','qty':10,'price':100.,'fee':1.,'trade_date':DAY.isoformat(),'card_id':'held'},at='2026-09-29T10:00:00-04:00')
    return x


def test_unconfirmed_daily_cannot_finalize_nav(loop):
    result=loop.post_close_manage(DAY)
    assert result.get('skipped')=='official_close_not_confirmed'
    assert not (loop.ledger.data_dir/'nav/daily.jsonl').exists()


def test_missing_mark_cannot_finalize_nav_with_cost_fallback(loop):
    loop.repo.get_daily=lambda *a:pl.DataFrame()
    result=loop.post_close_manage(DAY,confirmed_official=True)
    # 0.16.1: 缺价不再整日跳过, 只跳过该只止损判断; 净值仍不按成本兜底定版
    assert not result.get('skipped') and result['missing_symbols']==['TEST'] and result['nav'] is None
    assert not (loop.ledger.data_dir/'nav/daily.jsonl').exists()


def test_official_nav_uses_raw_close_not_adjusted_close(loop):
    result=loop.post_close_manage(DAY,confirmed_official=True)
    assert result['nav']['market_value']==990.


def test_pipeline_paper_stage_does_not_scan_during_session(loop,monkeypatch):
    monkeypatch.setattr(us_clock,'now',lambda:datetime(2026,9,29,12,tzinfo=us_clock.tz))
    scan=MagicMock(side_effect=AssertionError('intraday scan forbidden'))
    loop.opp_svc.run_daily_scan=scan
    monkeypatch.setattr(daily_pipeline,'_get_app_state_attr',lambda name:loop if name=='trading_loop' else None)
    result=daily_pipeline._run_zen_post_close(loop.repo,DAY)
    assert result['status']=='skipped'
    scan.assert_not_called()


def test_legacy_low_atr_open_card_expires_before_execution(tmp_path,monkeypatch):
    now=datetime(2026,9,29,10,tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock,'now',lambda:now)
    x=TradingLoop(Repo(tmp_path),Account());x.boot()
    card=OpportunityCard('legacy','TEST','breakout',date(2026,9,28),(19.9,20.1),19.8,(25.,26.),risk_per_share=.2,reward_risk=20.,state=CardState.OPEN,evidence={'sector':'Technology'})
    x.opp_svc.save_scan(ScanResult(card.created_date,cards=[card],gate='green'))
    provider=SimpleNamespace(get_execution_quotes=lambda symbols:[{'symbol':'TEST','price':20.,'timestamp':now.isoformat(),'feed':'iex'}])
    x.run_intraday(provider)
    states=x.opp_svc.load_cards(card.created_date)['cards']
    assert states[0]['state']=='expired'
    assert not any(e['kind']=='entry_intent' for e in x.ledger.read_events())


def test_universe_uses_real_security_type_flags(tmp_path):
    repo=Repo(tmp_path)
    repo.get_instruments=lambda:pl.DataFrame({'symbol':['GOOD','ETF','PREF'],'instrument_type':['common_stock','etf','preferred'],'sector':['Technology']*3})
    requested=[]
    def daily(symbols,*a):
        requested.extend(symbols);return pl.DataFrame()
    repo.get_daily_batch=daily
    OpportunityService(repo)._load_universe(load_params(),DAY)
    assert 'GOOD' in requested and 'ETF' not in requested and 'PREF' not in requested
