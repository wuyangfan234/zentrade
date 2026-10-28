"""Isolated synthetic paper lifecycle proof; never touches production ledger or broker."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from datetime import date, datetime, timedelta
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import polars as pl
from app.market_clock import us_clock
from app.ledger.core import Account
from app.opportunity.card import OpportunityCard,CardState
from app.opportunity.engine import ScanResult
from app.services.trading_loop import TradingLoop


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='zt-paper-proof-') as tmp:
        day=date(2026,9,22)
        current=[datetime(2026,9,22,10,tzinfo=us_clock.tz)]
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp)),get_instruments=lambda:pl.DataFrame({'symbol':['DEMO'],'sector':['Technology']}),get_daily=lambda *a:pl.DataFrame({'date':[day],'close':[96.],'raw_close':[96.]}))
        with patch.object(us_clock,'now',side_effect=lambda:current[0]),patch.object(us_clock,'_calendar_provider',None):
            loop=TradingLoop(repo,Account());loop.boot()
            c=OpportunityCard('proof-card','DEMO','breakout',date(2026,9,21),(100.,102.),95.,(110.,120.),risk_per_share=6.,reward_risk=2.,state=CardState.OPEN,evidence={'sector':'Technology'})
            loop.opp_svc.save_scan(ScanResult(c.created_date,cards=[c],gate='green'))
            def feed(price,open_price=100):
                now=current[0]
                return loop.intraday_fill_check({'DEMO':{'symbol':'DEMO','price':price,'timestamp':now.isoformat(),'feed':'iex','open':open_price,'open_timestamp':now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat()}})
            feed(101);assert not loop.ledger.open_positions
            current[0]+=timedelta(seconds=15);feed(101.2)
            assert len(loop.ledger.open_positions)==1
            start_qty=loop.ledger.open_positions[0].qty
            feed(101.2);assert loop.ledger.open_positions[0].qty==start_qty
            current[0]=current[0].replace(hour=13);feed(111)
            remaining=loop.ledger.open_positions[0].qty
            assert remaining<start_qty
            current[0]+=timedelta(seconds=15);feed(111)
            assert loop.ledger.open_positions[0].qty==remaining
            current[0]=current[0].replace(hour=16,minute=35)
            loop.post_close_manage(day,confirmed_official=True)
            assert loop.ledger.open_positions  # close invalidation is NOT a same-close fill
            before_restart=loop.ledger.snapshot()
            loop=TradingLoop(repo,Account());loop.boot()
            assert loop.ledger.snapshot()==before_restart
            current[0]=datetime(2026,9,23,9,45,tzinfo=us_clock.tz);feed(94,open_price=93)
            assert not loop.ledger.open_positions
            events=loop.ledger.read_events()
            assert len([e for e in events if e['kind']=='fill'])==1
            assert len([e for e in events if e['kind']=='partial_close'])==1
            close=[e for e in events if e['kind']=='close'];assert len(close)==1 and close[0]['price']<93
            result={'kind':'synthetic-isolated-code-execution','not_production_trades':True,'events':events,'final_snapshot':loop.ledger.snapshot()}
            (out/'paper-lifecycle-proof.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps({'kind':result['kind'],'sequence':[e['kind'] for e in events],'initial_qty':start_qty,'partial_remaining':remaining,'next_open_exit_price':close[0]['price'],'cash':loop.ledger.cash,'open_positions':len(loop.ledger.open_positions)},ensure_ascii=False))

if __name__=='__main__':main()
