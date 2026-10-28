"""交易编排单测 (P3)。合成 repo + 台账 + 交易员联动。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from app.market_clock import us_clock
from app.opportunity.engine import ScanResult
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.opportunity.card import CardState, OpportunityCard
from app.services.trading_loop import TradingLoop

ACCOUNT = Account()
DAY = date(2026, 9, 25)


def _bars(n: int = 160) -> pl.DataFrame:
    rows = []
    base = 100.0
    d = DAY - timedelta(days=n + 60)
    for _ in range(n):
        base *= 1.002
        rows.append({"date": d, "open": base, "high": base * 1.01, "low": base * 0.99,
                     "close": base, "volume": 1_000_000.0, "amount": base * 1e6})
        d += timedelta(days=1)
    return pl.DataFrame(rows)


class _FakeRepo:
    def __init__(self, tmp_path, close_price: float = 100.0):
        self.store = SimpleNamespace(data_dir=tmp_path)
        self._close = close_price

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["TEST"], "name": ["Test"]})

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return _bars()

    def get_daily_batch(self, symbols, start, end, columns=None):
        spy = pl.DataFrame([{"date": DAY - timedelta(days=i), "open": 499.0, "high": 501.0,
                         "low": 498.0, "close": 500.0, "volume": 1_000_000_000.0,
                         "amount": 5e11} for i in range(210)]).with_columns(pl.lit("SPY").alias("symbol"))
        return pl.concat([_bars().with_columns(pl.lit("TEST").alias("symbol")), spy]).sort(["symbol", "date"])

    def get_daily(self, symbol, start, end, columns=None):
        return pl.DataFrame({"date": [end], "close": [self._close]})


def _card(**kw) -> OpportunityCard:
    base = dict(
        id="opp-loop-1", symbol="TEST", setup_type="breakout", created_date=DAY,
        entry_zone=(99.0, 101.0), invalidation=94.0, target_zone=(110.0, 112.0),
        max_holding_days=7, risk_per_share=5.5, reward_risk=2.0, evidence={},
        state=CardState.ACTIVE,
    )
    base.update(kw)
    return OpportunityCard(**base)


def test_boot_initializes_ledger(tmp_path):
    loop = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop.boot()
    assert loop.status()["cash"] == 100_000.0
    assert loop.status()["booted"] is True


def test_boot_replays_existing_ledger(tmp_path):
    loop = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop.boot()
    loop.ledger.append("fill", {"symbol": "TEST", "side": "buy", "qty": 10, "price": 100.0,
                                "fee": 1.0, "trade_date": DAY.isoformat()}, at=f"{DAY.isoformat()}T10:00:00")
    loop2 = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop2.boot()
    assert loop2.ledger.cash < 100_000.0
    assert loop2.ledger.position("TEST") is not None


def test_intraday_fill_check_fills_in_zone_on_next_source_snapshot(tmp_path, monkeypatch):
    now = datetime(2026,9,25,10,tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock,'now',lambda:now)
    loop = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop.boot()
    card = _card(evidence={'sector':'Technology'})
    loop.opp_svc.save_scan(ScanResult(DAY,cards=[card],gate='green'))
    first = loop.intraday_fill_check({'TEST':{'price':100.,'timestamp':now.isoformat(),'feed':'iex','open':100.,'open_timestamp':now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat()}})
    assert first == []
    now += timedelta(seconds=15)
    filled = loop.intraday_fill_check({'TEST':{'price':100.,'timestamp':now.isoformat(),'feed':'iex','open':100.,'open_timestamp':now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat()}})
    assert len(filled) == 1
    assert loop.ledger.position("TEST") is not None


def test_intraday_fill_check_ignores_outside_zone(tmp_path):
    loop = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop.boot()
    loop.trader.register_card(_card())
    assert loop.intraday_fill_check({"TEST": 95.0}) == []


def test_intraday_seventh_day_time_stop_before_close(tmp_path,monkeypatch):
    """第7交易日15:30在盘中强退；不等到盘后按旧收盘价伪成交。"""
    loop = TradingLoop(_FakeRepo(tmp_path), ACCOUNT)
    loop.boot()
    card = _card()
    loop.opp_svc.save_scan(ScanResult(DAY,cards=[card],gate='green'))
    loop.trader.register_card(card)
    loop.trader.try_fill_entry(card, price=100.0, trade_date=DAY, at=f"{DAY.isoformat()}T10:00:00-04:00")
    loop.trader.try_fill_entry(card, price=100.0, trade_date=DAY, at=f"{DAY.isoformat()}T10:00:15-04:00")
    # 推进到第 7 个交易日
    from app.market_clock import us_clock
    d = DAY
    while us_clock.days_held(DAY, d) < 7:
        d += timedelta(days=1)
    now = datetime.combine(d,datetime.min.time(),tzinfo=us_clock.tz).replace(hour=15,minute=30)
    monkeypatch.setattr(us_clock,'now',lambda:now)
    result = loop.intraday_fill_check({'TEST':{'price':100.,'timestamp':now.isoformat(),'feed':'iex','open':100.,'open_timestamp':now.replace(hour=9,minute=30,second=0,microsecond=0).isoformat()}})
    assert any(e['kind']=='close' and e['reason']=='time_stop' for e in result)
    assert loop.ledger.position("TEST") is None
    now = now.replace(hour=16,minute=35)
    assert loop.post_close_manage(d,confirmed_official=True)['nav']['nav'] > 0
