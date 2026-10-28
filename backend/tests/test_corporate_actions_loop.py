"""Corporate actions wired into the TradingLoop: apply splits/dividends, invalidate pending cards,
block entries on non-automatic actions, pause entries when the feed is unavailable."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.corporate_actions import CorporateActionService
from app.services.trading_loop import TradingLoop

pytestmark = pytest.mark.real_corporate_actions

CREATED = date(2026, 9, 28)


class Repo:
    def __init__(self, p):
        self.store = SimpleNamespace(data_dir=p)

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["TEST", "MRG"], "sector": ["Technology", "Energy"]})

    def get_daily(self, s, start, end):
        return pl.DataFrame({"date": [end], "close": [100.0]})


def seed(tmp_path, now, actions, symbols=("TEST", "MRG")):
    svc = CorporateActionService(tmp_path)
    svc._atomic_write(svc.cache_path, {"fetched_at": now.isoformat(), "symbols": list(symbols),
                                       "window": {}, "actions": actions})
    svc._atomic_write(svc.state_path, {"last_attempt_at": now.isoformat(), "ok": True, "error": None})


def split(sym, ex, factor, action_id="s1"):
    return {"id": action_id, "type": "forward_split" if factor > 1 else "reverse_split", "symbol": sym,
            "ex_date": ex, "auto": True, "factor": factor}


def dividend(sym, ex, rate, action_id="d1"):
    return {"id": action_id, "type": "cash_dividend", "symbol": sym, "ex_date": ex, "payable_date": ex,
            "auto": True, "rate": rate, "special": False}


def quote(now, sym="TEST", price=101.0):
    open_ts = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return {sym: {"symbol": sym, "price": price, "timestamp": now.isoformat(), "feed": "iex",
                  "open": 100.0, "open_timestamp": open_ts.isoformat()}}


def at(monkeypatch, iso):
    now = datetime.fromisoformat(iso).replace(tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    return now


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    x = TradingLoop(Repo(tmp_path), Account())
    x.boot()
    cards = [OpportunityCard(f"c-{s}", s, "breakout", CREATED, (100.0, 102.0), 95.0, (110.0, 115.0),
                             risk_per_share=6.0, reward_risk=2.0, state=CardState.OPEN, evidence={"sector": "Technology"})
             for s in ("TEST", "MRG")]
    x.opp_svc.save_scan(ScanResult(CREATED, cards=cards, gate="green"))
    return x


def fill_test(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed(tmp_path, now, [])
    loop.intraday_fill_check(quote(now))
    now = at(monkeypatch, "2026-09-29T10:00:15")
    loop.intraday_fill_check(quote(now))
    pos = loop.ledger.position("TEST")
    assert pos is not None
    return pos.qty


def test_split_on_ex_date_is_applied_once_to_an_open_position(loop, monkeypatch, tmp_path):
    qty = fill_test(loop, monkeypatch, tmp_path)
    now = at(monkeypatch, "2026-09-30T09:31:00")
    seed(tmp_path, now, [split("TEST", "2026-09-30", 2.0)])
    loop.apply_corporate_actions(now)
    loop.apply_corporate_actions(now)
    events = [e for e in loop.ledger.read_events() if e["kind"] == "corp_action"]
    assert len(events) == 1 and events[0]["factor"] == 2.0
    assert loop.ledger.position("TEST").qty == qty * 2


def test_cash_dividend_credits_shares_held_times_rate(loop, monkeypatch, tmp_path):
    qty = fill_test(loop, monkeypatch, tmp_path)
    cash_before = loop.ledger.cash
    now = at(monkeypatch, "2026-09-30T09:31:00")
    seed(tmp_path, now, [dividend("TEST", "2026-09-30", 0.25)])
    loop.apply_corporate_actions(now)
    assert loop.ledger.cash == pytest.approx(cash_before + qty * 0.25)


def test_future_or_unheld_actions_are_not_applied(loop, monkeypatch, tmp_path):
    fill_test(loop, monkeypatch, tmp_path)
    now = at(monkeypatch, "2026-09-30T09:31:00")
    seed(tmp_path, now, [split("TEST", "2026-10-05", 2.0), split("ZZZ", "2026-09-30", 2.0, "s2")])
    loop.apply_corporate_actions(now)
    assert not [e for e in loop.ledger.read_events() if e["kind"] == "corp_action"]


def test_split_invalidates_an_unfilled_card_whose_frozen_prices_no_longer_apply(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T09:31:00")
    seed(tmp_path, now, [split("MRG", "2026-09-29", 4.0)])
    loop.apply_corporate_actions(now)
    card = next(c for c in loop.opp_svc.all_cards(now.date()) if c.symbol == "MRG")
    assert card.state == CardState.EXPIRED


def test_merger_blocks_entry_without_touching_positions(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed(tmp_path, now, [{"id": "m1", "type": "cash_merger", "symbol": "TEST", "ex_date": "2026-10-20", "auto": False}])
    loop.intraday_fill_check(quote(now))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15)))
    assert loop.ledger.position("TEST") is None
    assert _has_blocker(loop.entry_blockers, "TEST", "corporate_action_pending")  # 0.26.0: 逐卡拦截另带 card_id


def test_unavailable_feed_pauses_new_entries(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed(tmp_path, now, [])
    status = loop.corp_actions.refresh(["TEST"], now, http_get=lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    assert status["status"] == "unavailable"
    loop.intraday_fill_check(quote(now))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15)))
    assert loop.ledger.position("TEST") is None
    assert {"symbol": "*", "reason": "corporate_actions_unavailable"} in loop.entry_blockers


def test_card_symbol_not_covered_by_the_last_fetch_cannot_be_entered(loop, monkeypatch, tmp_path):
    now = at(monkeypatch, "2026-09-29T10:00:00")
    seed(tmp_path, now, [], symbols=("MRG",))
    loop.intraday_fill_check(quote(now))
    loop.intraday_fill_check(quote(now + timedelta(seconds=15)))
    assert loop.ledger.position("TEST") is None
    assert _has_blocker(loop.entry_blockers, "TEST", "corporate_actions_not_covered")  # 0.26.0: 逐卡拦截另带 card_id


def test_loop_writes_entry_blockers_to_funnel(loop, monkeypatch, tmp_path):
    """准入漏斗接线 (2026-10-12): 公司行动数据不可用时的全局拦截原因落到 funnel/<日>.jsonl, 只记一次。"""
    import json as _json
    now = at(monkeypatch, "2026-09-29T10:00:00")
    loop.intraday_fill_check(quote(now))                      # 未 seed 公司行动 → 不可用
    now = at(monkeypatch, "2026-09-29T10:00:15")
    loop.intraday_fill_check(quote(now))
    f = tmp_path / "funnel" / "2026-09-29.jsonl"
    rows = [_json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    reasons = [(r["symbol"], r["reason"]) for r in rows]
    assert ("*", "corporate_actions_unavailable") in reasons
    assert len(reasons) == len(set(reasons))


def _has_blocker(blockers, symbol, reason):
    """逐卡拦截 (0.26.0 起带 card_id, 漏斗按它归属): 按 symbol+reason 匹配, 且逐卡拦截必须带 card_id。"""
    hit = [b for b in blockers if b.get("symbol") == symbol and b.get("reason") == reason]
    return bool(hit) and all(b.get("card_id") for b in hit if symbol != "*")
