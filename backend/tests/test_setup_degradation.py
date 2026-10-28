"""Four-week negative-expectancy degradation (user decision 2026-10-01):
rolling last 28 calendar days of closed round trips >= 10 with finite R, mean R < 0 -> setup degraded
to watch-only (no new entries) until manually restored."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import setup_status as ss
from app.services.trading_loop import TradingLoop

EVAL = date(2026, 10, 4)


def trip(setup, exit_day, r):
    return SimpleNamespace(setup_type=setup, exit_date=exit_day, r_multiple=r)


def trips(setup, n, r, last=EVAL):
    return [trip(setup, last - timedelta(days=i % 20), r) for i in range(n)]


@pytest.mark.parametrize("items,expected", [
    (trips("breakout", 9, -1.0), False),                       # 样本不足
    (trips("breakout", 10, -0.1), True),                       # 10 笔且均 R<0
    (trips("breakout", 10, 0.0), False),                       # 均 R = 0 不降级
    (trips("breakout", 10, -1.0) + trips("breakout", 3, None), True),
    (trips("breakout", 9, -1.0) + trips("breakout", 5, None), False),   # R=None 不计样本
])
def test_evaluation_threshold(items, expected):
    decision = ss.evaluate(items, "breakout", EVAL)
    assert decision["degrade"] is expected


def test_window_is_the_last_28_calendar_days_by_exit_date():
    inside = [trip("pullback", EVAL - timedelta(days=27), -1.0) for _ in range(10)]
    outside = [trip("pullback", EVAL - timedelta(days=28), -1.0) for _ in range(10)]
    assert ss.evaluate(inside, "pullback", EVAL)["degrade"] is True
    assert ss.evaluate(outside, "pullback", EVAL)["degrade"] is False


def test_state_persists_restores_and_audits(tmp_path):
    store = ss.SetupStatusStore(tmp_path)
    assert store.is_degraded("breakout") is False
    store.degrade("breakout", {"samples": 12, "avg_r": -0.4, "window_start": "2026-09-07", "window_end": "2026-10-04"},
                  at="2026-10-04T09:00:00-04:00")
    assert ss.SetupStatusStore(tmp_path).is_degraded("breakout") is True
    store.restore("breakout", note="复核后恢复", at="2026-10-05T10:00:00-04:00")
    reloaded = ss.SetupStatusStore(tmp_path)
    assert reloaded.is_degraded("breakout") is False
    kinds = [e["action"] for e in reloaded.audit()]
    assert kinds == ["degrade", "restore"]


def test_weekly_evaluation_degrades_but_never_auto_restores(tmp_path):
    store = ss.SetupStatusStore(tmp_path)
    changed = ss.run_weekly_evaluation(store, trips("breakout", 10, -1.0), EVAL)
    assert changed == ["breakout"] and store.is_degraded("breakout")
    changed = ss.run_weekly_evaluation(store, trips("breakout", 10, 2.0), EVAL + timedelta(days=7))
    assert changed == [] and store.is_degraded("breakout"), "restore is manual only"


class Repo:
    def __init__(self, p):
        self.store = SimpleNamespace(data_dir=p)

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["TEST"], "sector": ["Technology"]})

    def get_daily(self, s, start, end):
        return pl.DataFrame({"date": [end], "close": [100.0]})


def test_degraded_setup_blocks_entries_but_not_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    loop = TradingLoop(Repo(tmp_path), Account())
    loop.boot()
    card = OpportunityCard("c-TEST", "TEST", "breakout", date(2026, 9, 28), (100.0, 102.0), 95.0, (110.0, 115.0),
                           risk_per_share=6.0, reward_risk=2.0, state=CardState.OPEN, evidence={"sector": "Technology"})
    loop.opp_svc.save_scan(ScanResult(card.created_date, cards=[card], gate="green"))
    ss.SetupStatusStore(tmp_path).degrade("breakout", {"samples": 10, "avg_r": -1.0}, at="2026-09-28T09:00:00-04:00")
    now = datetime(2026, 9, 29, 10, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    q = lambda t: {"TEST": {"symbol": "TEST", "price": 101.0, "timestamp": t.isoformat(), "feed": "iex", "open": 100.0,
                            "open_timestamp": t.replace(hour=9, minute=30, second=0, microsecond=0).isoformat()}}
    loop.intraday_fill_check(q(now))
    loop.intraday_fill_check(q(now + timedelta(seconds=15)))
    assert loop.ledger.position("TEST") is None
    assert _has_blocker(loop.entry_blockers, "TEST", "setup_degraded:breakout")  # 0.26.0: 逐卡拦截另带 card_id
    assert next(c for c in loop.opp_svc.all_cards(now.date()) if c.id == "c-TEST").state == CardState.ACTIVE


def _has_blocker(blockers, symbol, reason):
    """逐卡拦截 (0.26.0 起带 card_id, 漏斗按它归属): 按 symbol+reason 匹配, 且逐卡拦截必须带 card_id。"""
    hit = [b for b in blockers if b.get("symbol") == symbol and b.get("reason") == reason]
    return bool(hit) and all(b.get("card_id") for b in hit if symbol != "*")
