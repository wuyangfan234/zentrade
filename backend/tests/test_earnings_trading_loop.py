"""Earnings protection wired into the production TradingLoop (fake quotes, temp ledger)."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

pytestmark = pytest.mark.real_earnings_calendar

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.earnings_calendar import EarningsCalendar
from app.services.trading_loop import TradingLoop

CREATED = date(2026, 9, 28)


class Repo:
    def __init__(self, p):
        self.store = SimpleNamespace(data_dir=p)

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["TEST"], "sector": ["Technology"]})

    def get_daily(self, s, start, end):
        return pl.DataFrame({"date": [end], "close": [100.0]})


def seed_calendar(tmp_path, now, events: dict):
    cal = EarningsCalendar(tmp_path)
    cal._write(cal.cache_path, {"fetched_at": now.isoformat(), "source": "nasdaq", "window": [], "events": events})
    cal._write(cal.state_path, {"last_attempt_at": now.isoformat(), "ok": True, "error": None})


def quote(now, price=101.0):
    open_ts = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return {"TEST": {"symbol": "TEST", "price": price, "timestamp": now.isoformat(), "feed": "iex",
                     "open": 100.0, "open_timestamp": open_ts.isoformat()}}


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    x = TradingLoop(Repo(tmp_path), Account())
    x.boot()
    card = OpportunityCard("c-TEST", "TEST", "breakout", CREATED, (100.0, 102.0), 95.0, (110.0, 115.0),
                           risk_per_share=6.0, reward_risk=2.0, state=CardState.OPEN, evidence={"sector": "Technology"})
    x.opp_svc.save_scan(ScanResult(CREATED, cards=[card], gate="green"))
    return x


def at(monkeypatch, iso):
    now = datetime.fromisoformat(iso).replace(tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    return now


def fills(loop):
    return [e for e in loop.ledger.read_events() if e["kind"] == "fill"]


def test_entry_is_blocked_when_earnings_are_tomorrow(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed_calendar(tmp_path, now, {"TEST": {"date": "2026-09-30", "timing": "bmo"}})
    loop.intraday_fill_check(quote(now))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15)))
    assert fills(loop) == []
    assert _has_blocker(loop.entry_blockers, "TEST", "earnings_window")  # 0.26.0: 逐卡拦截另带 card_id


def test_entry_is_allowed_on_the_earnings_day_itself(loop, monkeypatch, tmp_path):
    """用户拍板原文: 仅财报前 1 个交易日禁开 → 财报当天不在禁开窗口内。"""
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed_calendar(tmp_path, now, {"TEST": {"date": "2026-09-29", "timing": "amc"}})
    loop.intraday_fill_check(quote(now))
    now = at(monkeypatch, "2026-09-29T10:00:15")
    loop.intraday_fill_check(quote(now))
    assert len(fills(loop)) == 1
    assert not _has_blocker(loop.entry_blockers, "TEST", "earnings_window")


def test_entry_is_allowed_two_trading_days_before_earnings(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed_calendar(tmp_path, now, {"TEST": {"date": "2026-10-01", "timing": "bmo"}})
    loop.intraday_fill_check(quote(now))
    now = at(monkeypatch, "2026-09-29T10:00:15")
    loop.intraday_fill_check(quote(now))
    assert len(fills(loop)) == 1


def test_unavailable_calendar_pauses_new_entries_but_not_exits(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")      # no calendar written → unavailable
    loop.intraday_fill_check(quote(now))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15)))
    assert fills(loop) == []
    assert {"symbol": "*", "reason": "earnings_calendar_unavailable"} in loop.entry_blockers


def _open_position(loop, monkeypatch, tmp_path, earnings):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed_calendar(tmp_path, now, {"TEST": earnings})
    loop.intraday_fill_check(quote(now))
    now = at(monkeypatch, "2026-09-29T10:00:15")
    loop.intraday_fill_check(quote(now))
    assert len(fills(loop)) == 1
    return loop.ledger.position("TEST").qty


def test_position_is_halved_in_the_pre_close_window_before_earnings(loop, monkeypatch, tmp_path):
    qty = _open_position(loop, monkeypatch, tmp_path, {"date": "2026-10-02", "timing": "bmo"})
    now = at(monkeypatch, "2026-10-01T15:31:00")
    seed_calendar(tmp_path, now, {"TEST": {"date": "2026-10-02", "timing": "bmo"}})
    loop.intraday_fill_check(quote(now, 103.0))
    reduce = [e for e in loop.ledger.read_events() if e["kind"] == "partial_close" and e.get("reason") == "earnings_reduce"]
    assert len(reduce) == 1 and reduce[0]["qty"] == qty // 2
    loop.intraday_fill_check(quote(now + timedelta(seconds=30), 103.0))
    assert len([e for e in loop.ledger.read_events() if e.get("reason") == "earnings_reduce"]) == 1


def test_missed_window_is_recorded_once_and_never_made_up_the_next_day(loop, monkeypatch, tmp_path):
    qty = _open_position(loop, monkeypatch, tmp_path, {"date": "2026-10-02", "timing": "bmo"})
    now = at(monkeypatch, "2026-10-02T09:45:00")         # window (10-01 15:30-16:00) passed with no quote
    seed_calendar(tmp_path, now, {"TEST": {"date": "2026-10-02", "timing": "bmo"}})
    loop.intraday_fill_check(quote(now, 103.0))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15), 103.0))
    events = loop.ledger.read_events()
    assert not any(e.get("reason") == "earnings_reduce" for e in events)
    missed = [e for e in events if e["kind"] == "risk_note" and e.get("reason") == "earnings_reduce_missed"]
    assert len(missed) == 1
    assert loop.ledger.position("TEST").qty == qty


def _has_blocker(blockers, symbol, reason):
    """逐卡拦截 (0.26.0 起带 card_id, 漏斗按它归属): 按 symbol+reason 匹配, 且逐卡拦截必须带 card_id。"""
    hit = [b for b in blockers if b.get("symbol") == symbol and b.get("reason") == reason]
    return bool(hit) and all(b.get("card_id") for b in hit if symbol != "*")
