"""端到端: 空头卡经 TradingLoop.intraday_fill_check 开仓 → 盘中管理 → 收盘失效 → 次日开盘回补, 以及熔断净值口径。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.trading_loop import TradingLoop


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 5, 10, 0, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["XYZ"], "sector": ["Technology"]}),
                           get_daily=lambda sym, start, end, columns=None: pl.DataFrame({"date": [end], "close": [100.0], "raw_close": [100.0]}))
    loop = TradingLoop(repo, Account())
    loop.boot()
    monkeypatch.setattr(loop.earnings, "usable", lambda *a: True)
    monkeypatch.setattr(loop.earnings, "event_for", lambda s: None)
    monkeypatch.setattr(loop.corp_actions, "usable", lambda *a: True)
    monkeypatch.setattr(loop.corp_actions, "covers", lambda s: True)
    monkeypatch.setattr(loop.corp_actions, "actions", lambda: [])
    from app.services import decision_desk, strategy_evaluation, shortable
    decision_desk.SourceRegistry(tmp_path).set_status("phantom_4h_short", "enabled")
    strategy_evaluation.save_evaluation(tmp_path, {"sources": {
        "phantom_4h_short": {"n": 100, "mean": .3, "ci_low": .1}}})
    monkeypatch.setattr(shortable, "_query", lambda s: {"shortable": True, "easy_to_borrow": True})
    card = OpportunityCard(id="opp-2026-10-02-XYZ-short", symbol="XYZ", setup_type="strategy:phantom_4h_short",
                           created_date=date(2026, 10, 2), entry_zone=(99.0, 100.0), invalidation=104.0,
                           target_zone=(88.0, 89.76), risk_per_share=4.5, reward_risk=2.3, side="short", state=CardState.OPEN)
    loop.opp_svc.save_scan(ScanResult(as_of=date(2026, 10, 2), gate="green", params_revision=1, scanned=1, cards=[card], watch=[], rejected=[]))
    return loop, clock


def _q(at, price, open_=99.6):
    return {"XYZ": {"symbol": "XYZ", "price": price, "timestamp": at.isoformat(), "feed": "tickflow",
                    "open": open_, "open_timestamp": at.replace(hour=9, minute=30, second=0).isoformat()}}


def test_short_card_end_to_end_through_trading_loop(rig):
    loop, clock = rig
    for px in (99.5, 99.4):
        loop.intraday_fill_check(_q(clock[0], px))
        clock[0] += timedelta(seconds=15)
    pos = loop.ledger.position("XYZ")
    assert pos is not None and pos.qty < 0 and pos.stop_price == 104.0
    # 熔断: 空头上涨 = 亏损; 价格 99.4 附近 → 当日变化近 0, 不触发
    assert loop.status()["circuit_breaker"]["block_new_entries"] is False
    # 收盘站上失效点 → 记退出意向; 次日开盘回补
    clock[0] = datetime(2026, 10, 5, 16, 5, tzinfo=us_clock.tz)
    ev = loop.trader.manage_close("XYZ", close_price=104.5, trade_date=date(2026, 10, 5), at=clock[0].isoformat())
    assert ev and ev[0]["reason"] == "invalidation"
    clock[0] = datetime(2026, 10, 6, 9, 31, tzinfo=us_clock.tz)
    loop.intraday_fill_check(_q(clock[0], 105.2, open_=105.0))
    assert loop.ledger.position("XYZ") is None
    close = [e for e in loop.ledger.read_events() if e["kind"] == "close"][-1]
    assert close["position_side"] == "short" and close["price"] == pytest.approx(round(105.0 * 1.0005, 4)) and close["r_multiple"] < -1
    # 卡状态: 已成交 → 已平仓
    states = {c["id"]: c["state"] for c in (loop.opp_svc.load_cards(date(2026, 10, 2)) or {})["cards"]}
    assert states["opp-2026-10-02-XYZ-short"] == "closed"


def test_short_gap_down_open_expires_card(rig):
    loop, clock = rig
    loop.intraday_fill_check(_q(clock[0], 96.0, open_=96.5))  # 开盘 96.5 < 下沿 99 × 0.98
    payload = loop.opp_svc.load_cards(date(2026, 10, 2))
    c = (payload["cards"] + payload["watch"])[0]
    assert c["state"] == "expired" and "空单不追" in c["state_reason"]
    assert loop.ledger.position("XYZ") is None
