"""Independent-review minute edge cases, no network."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import polars as pl
import pytest
from app.market_clock import us_clock
from app.services import kline_sync, minute_refresh


def bars(symbol="AAPL", start=None, n=1):
    start = start or datetime(2026, 9, 25, 9, 30)
    return pl.DataFrame({"symbol": [symbol]*n, "datetime": [start+timedelta(minutes=i) for i in range(n)], "close": [100.]*n})


@pytest.mark.parametrize("hour", [17,18,19])
def test_explicit_epoch_afterhours_cannot_be_relabelled_as_intraday(hour):
    stamp = int(datetime(2026,9,25,hour,tzinfo=us_clock.tz).timestamp()*1000)
    df = pl.DataFrame({"symbol":["AAPL"],"timestamp":[stamp],"close":[999.]})
    assert kline_sync._normalize_minute(df).is_empty()


def test_naive_et_afterhours_never_guessed_as_utc(monkeypatch):
    provider = SimpleNamespace(get_minute=lambda *a,**kw: bars(start=datetime(2026,9,25,17)))
    monkeypatch.setattr(kline_sync.preferences,"get_minute_data_provider",lambda:"edge")
    monkeypatch.setattr(kline_sync,"_resolve_minute_provider",lambda name:(provider,False,None))
    df, fallback = kline_sync._try_custom_minute(["AAPL"],datetime(2026,9,25,9,30),datetime(2026,9,25,16),"stock")
    assert not fallback
    assert df.is_empty()


def test_existing_partition_requires_startup_full_repair(monkeypatch,tmp_path):
    now = datetime(2026,9,25,12,tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock,"now",lambda:now)
    calls=[]
    provider=SimpleNamespace(minute_delay_minutes=16,get_intraday_since=lambda *a: calls.append("inc") or bars("MSFT",datetime(2026,9,25,11,43),2),get_intraday_batch=lambda syms: calls.append("full") or bars("MSFT",n=135))
    repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),get_instruments=lambda:pl.DataFrame({"symbol":["AAPL","MSFT"]}))
    kline_sync._write_minute_partition(bars(n=135),tmp_path/"kline_minute")
    svc=minute_refresh.MinuteRefreshService(repo)
    monkeypatch.setattr(svc,"_resolve_custom",lambda:(provider,"edge"))
    svc._run_round()
    assert calls==["full"]
    df=pl.read_parquet(tmp_path/"kline_minute/date=2026-09-25/part.parquet")
    assert df.filter(pl.col("symbol")=="MSFT")["datetime"].min()==datetime(2026,9,25,9,30)


def test_slow_close_round_gets_a_final_catchup(monkeypatch,tmp_path):
    current=[datetime(2026,9,25,16,13,tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock,"now",lambda:current[0])
    monkeypatch.setattr(us_clock,"_calendar_provider",None)
    monkeypatch.setattr(minute_refresh.preferences,"get_minute_refresh_enabled",lambda:True)
    monkeypatch.setattr("app.services.trading_day.is_trading_day",lambda *a:True)
    def delayed(*a):
        current[0]=datetime(2026,9,25,16,19,tzinfo=us_clock.tz)
        return bars(n=388)
    provider=SimpleNamespace(minute_delay_minutes=16,get_intraday_batch=delayed)
    repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),get_instruments=lambda:pl.DataFrame({"symbol":["AAPL"]}))
    svc=minute_refresh.MinuteRefreshService(repo)
    monkeypatch.setattr(svc,"_resolve_custom",lambda:(provider,"edge"))
    monkeypatch.setattr(svc,"capability_ok",lambda:True)
    svc._run_round()
    assert svc._gate_reason() is None  # cannot stop with 15:57 tail


def test_successful_sparse_or_empty_queries_are_briefly_cached(monkeypatch):
    provider=SimpleNamespace(get_minute=MagicMock(return_value=pl.DataFrame()))
    monkeypatch.setattr(kline_sync.preferences,"get_minute_data_provider",lambda:"edge-cache")
    monkeypatch.setattr(kline_sync,"_resolve_minute_provider",lambda name:(provider,False,None))
    for _ in range(3):
        df,fallback=kline_sync._try_custom_minute(["AAPL"],datetime(2026,9,25,9,30),datetime(2026,9,25,16),"stock")
        assert not fallback and df.is_empty()
    assert provider.get_minute.call_count==1
