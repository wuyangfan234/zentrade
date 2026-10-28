"""TickFlow as execution primary (approved 2026-10-02: switch on deploy), IEX cross-check + fallback,
and research-volume contract for the consolidated real-time feed."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.quote_contract import normalize_record
from app.services.trading_loop import TradingLoop

NOW = datetime(2026, 9, 30, 11, tzinfo=us_clock.tz)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["AAPL"], "sector": ["Technology"]}),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [end], "close": [100.], "raw_close": [100.]}))
    loop = TradingLoop(repo, Account())
    loop.boot()
    for name in ("usable",):
        monkeypatch.setattr(loop.earnings, name, lambda *a: True)
    monkeypatch.setattr(loop.earnings, "event_for", lambda *a: None)
    monkeypatch.setattr(loop.corp_actions, "usable", lambda *a: True)
    monkeypatch.setattr(loop.corp_actions, "covers", lambda *a: True)
    monkeypatch.setattr(loop.opp_svc, "revalidate_unfilled", lambda now: None)  # 复验需要完整日K, 本测试只测报价源
    card = OpportunityCard("tf-AAPL", "AAPL", "breakout", date(2026, 9, 29), (99., 102.), 94., (110., 120.),
                           risk_per_share=6., reward_risk=2., evidence={"sector": "Technology"}, state=CardState.OPEN)
    loop.opp_svc.save_scan(ScanResult(card.created_date, cards=[card], gate="green"))
    return loop, clock


def tq(at, price=100.0, feed="tickflow"):
    return {"symbol": "AAPL", "price": price, "timestamp": int(at.timestamp() * 1000), "feed": feed,
            "open": 100.0, "open_timestamp": int(at.replace(hour=9, minute=30, second=0).timestamp() * 1000)}


class FakeTF:
    def __init__(self, clock, price=100.0, fail=False):
        self.clock, self.price, self.fail, self.calls = clock, price, fail, 0

    def get_execution_quotes(self, symbols, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("TickFlow HTTP 503")
        return [tq(self.clock[0] - timedelta(seconds=6), self.price)]


class FakeIEX:
    def __init__(self, clock, price=100.0, age=5):
        self.clock, self.price, self.age = clock, price, age

    def get_execution_quotes(self, symbols, **kw):
        return [tq(self.clock[0] - timedelta(seconds=self.age), self.price, feed="iex")]


def test_tickflow_feed_is_accepted_for_execution(rig):
    loop, clock = rig
    out = loop._fresh_quotes({"AAPL": tq(clock[0] - timedelta(seconds=6))}, clock[0])
    assert "AAPL" in out and out["AAPL"]["feed"] == "tickflow"


def test_tickflow_primary_fills_with_tickflow_price(rig, monkeypatch):
    loop, clock = rig
    monkeypatch.setattr(loop, "_iex_provider", lambda: FakeIEX(clock, 100.02))
    tf = FakeTF(clock, 100.0)
    for _ in range(3):
        loop.run_intraday(provider=tf)
        clock[0] += timedelta(seconds=15)
    fills = [e for e in loop.ledger.read_events() if e["kind"] == "fill"]
    assert len(fills) == 1 and fills[0].get("quote_feed") == "tickflow"
    assert loop.status()["execution_source"]["active"] == "tickflow"


def test_cross_source_deviation_over_50bps_rejects_quote(rig, monkeypatch):
    loop, clock = rig
    monkeypatch.setattr(loop, "_iex_provider", lambda: FakeIEX(clock, 101.0))  # 100 bps away, fresh
    loop.run_intraday(provider=FakeTF(clock, 100.0))
    assert any(r["reason"].startswith("cross_source_deviation") for r in loop.rejected_quotes)
    assert not any(e["kind"] == "entry_intent" for e in loop.ledger.read_events())


def test_stale_iex_does_not_veto_tickflow(rig, monkeypatch):
    loop, clock = rig
    monkeypatch.setattr(loop, "_iex_provider", lambda: FakeIEX(clock, 101.0, age=90))  # thin IEX print 90 s old
    loop.run_intraday(provider=FakeTF(clock, 100.0))
    assert not loop.rejected_quotes


def test_tickflow_failure_falls_back_to_iex(rig, monkeypatch):
    loop, clock = rig
    monkeypatch.setattr(loop, "_iex_provider", lambda: FakeIEX(clock, 100.0))
    loop.run_intraday(provider=FakeTF(clock, fail=True))
    st = loop.status()["execution_source"]
    assert st["active"] == "iex_fallback" and "503" in (st["primary_error"] or "")
    assert loop.last_error is None


def test_contract_treats_tickflow_as_realtime_consolidated_volume():
    ms = int(NOW.timestamp() * 1000) - 6000
    row = normalize_record({"symbol": "AAPL", "last_price": 100.0, "timestamp": ms, "volume": 1e6, "amount": 1e8,
                            "volume_ts": ms, "feed": "tickflow", "quote_feed": "tickflow", "volume_feed": "tickflow",
                            "historical_volume_feed": "sip"}, NOW)
    assert row["volume_status"] == "valid" and row["data_delay"] == 0 and row["volume"] == 1e6


def test_contract_rejects_stale_tickflow_quote():
    ms = int(NOW.timestamp() * 1000) - 300_000
    assert normalize_record({"symbol": "AAPL", "last_price": 100.0, "timestamp": ms, "feed": "tickflow"}, NOW) is None
