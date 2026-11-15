"""Boundary cases borrowed from QuantConnect LEAN, replayed against the paper trader.

Only the *cases* come from LEAN (master @ 82d2604, Apache-2.0); no LEAN code is used. Each block
names the LEAN source it was derived from and the ZenTrade rule it checks:

- stale / off-session quotes never reach the matcher      Common/Orders/Fills/EquityFillModel.cs ("Do not fill on stale data")
- paper costs == replay costs                              Common/Orders/Fees/InteractiveBrokersFeeModel.cs (USD equity: $0.005/share, min $1)
- a split never changes money, plan or R (long / short)    Common/Securities/SecurityPortfolioManager.cs ApplySplit, EquitySplitHoldings*RegressionAlgorithm
- dividends: longs receive, shorts pay, held-before-ex     SecurityPortfolioManager.cs ApplyDividend, DividendRegressionAlgorithm
- split day: every stale price is rescaled to share basis  SecurityPortfolioManager.cs ApplySplit (holdings and reference price move together)
- held position hit by a non-automatic action is reported  DelistingEventsAlgorithm / HistoryWithSymbolChangesRegressionAlgorithm
- same bar: target first, stops are close-confirmed        EquityFillModel.cs StopLimitFill ("no way to be sure minimum wasn't before the stop")
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import OpportunityCard
from app.services import card_replay as cr
from app.services import notify_opportunity as notify
from app.services import strategy_evaluation as se
from app.services import trading_loop as tl
from app.services.corporate_actions import CorporateActionService
from app.services.trading_loop import TradingLoop
from app.trader.sizing import fee_for, size_position
from app.trader.trader import SwingTrader

ET = us_clock.tz
FREE = Account(commission_per_share=0.0, min_commission=0.0, slippage_bps=0.0)   # isolates price/quantity effects


def _at(day: str, hhmm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET).isoformat()


# ── 1. stale / off-session quotes ────────────────────────────────────────────

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=ET)


def _quote(**kw):
    base = {"symbol": "AAA", "price": 100.0, "timestamp": (NOW - timedelta(seconds=5)).isoformat(), "feed": "iex"}
    base.update(kw)
    return base


def _fresh(q, now=NOW):
    loop = object.__new__(TradingLoop)
    out = loop._fresh_quotes({"AAA": q}, now)
    return out, loop.rejected_quotes


@pytest.mark.parametrize("change,reason", [
    ({"timestamp": (NOW - timedelta(seconds=tl.MAX_QUOTE_AGE_SECONDS + 1)).isoformat()}, "stale_or_future"),
    ({"timestamp": (NOW + timedelta(seconds=1)).isoformat()}, "stale_or_future"),
    ({"timestamp": (NOW - timedelta(days=1)).replace(hour=15, minute=59).isoformat()}, "stale_or_future"),
    ({"feed": "delayed_sip"}, "non_execution_feed"),
    ({"feed": None}, "non_execution_feed"),
    ({"price": True}, "invalid_price"),
    ({"price": float("nan")}, "invalid_price"),
    ({"price": 0}, "invalid_price"),
    ({"price": -1.0}, "invalid_price"),
    ({"timestamp": "2026-09-29T09:59:55"}, "source_timestamp_needs_timezone"),
    ({"timestamp": None}, "source_timestamp_needs_timezone"),
])
def test_unusable_quote_never_reaches_the_matcher(change, reason):
    out, rejected = _fresh(_quote(**change))
    assert out == {} and rejected == [{"symbol": "AAA", "reason": reason}]


def test_quote_age_limit_is_inclusive_and_epoch_ms_is_accepted():
    edge = NOW - timedelta(seconds=tl.MAX_QUOTE_AGE_SECONDS)
    out, rejected = _fresh(_quote(timestamp=edge.isoformat()))
    assert list(out) == ["AAA"] and rejected == []
    out, _ = _fresh(_quote(timestamp=int((NOW - timedelta(seconds=3)).timestamp() * 1000)))
    assert out["AAA"]["price"] == 100.0


def test_last_trade_before_the_open_is_not_a_session_quote():
    now = datetime(2026, 9, 29, 9, 30, 20, tzinfo=ET)
    out, rejected = _fresh(_quote(timestamp=now.replace(minute=29, second=59).isoformat()), now)
    assert out == {} and rejected[0]["reason"] == "outside_session"


@pytest.mark.parametrize("open_ts", [
    NOW.replace(hour=9, minute=31, second=0).isoformat(),                       # first print a minute late
    (NOW - timedelta(days=1)).replace(hour=9, minute=30, second=0).isoformat(),  # yesterday's open
    None,
])
def test_only_todays_0930_print_counts_as_the_official_open(open_ts):
    """Next-open exits and entries need the real opening print; anything else leaves `open` unset."""
    out, rejected = _fresh(_quote(open=101.0, open_timestamp=open_ts))
    assert rejected == [] and "open" not in out["AAA"]
    out, _ = _fresh(_quote(open=101.0, open_timestamp=NOW.replace(hour=9, minute=30, second=0).isoformat()))
    assert out["AAA"]["open"] == 101.0


# ── 2. paper costs == replay costs ───────────────────────────────────────────

def test_replay_uses_the_paper_accounts_slippage_fee_and_sizing():
    acct = Account()
    assert pytest.approx(acct.slippage_bps / 10_000) == se.SLIP
    assert acct.initial_equity == se.EQUITY and acct.max_holding_days == se.MAX_HOLD
    for risk, entry in ((2.0, 50.0), (20.0, 400.0)):          # per-share fee binds / $1 minimum binds
        qty = min(int(se.EQUITY * se.RISK_PCT / risk), int(se.EQUITY * se.NOTIONAL_CAP / entry))
        assert se.FEE_R(risk, entry) == pytest.approx(2 * fee_for(qty, acct) / (qty * risk))
    card = OpportunityCard("c", "AAA", "breakout", date(2026, 9, 28), (99.0, 101.0), 99.9, (110.0, 112.0),
                           risk_per_share=0.1)
    assert size_position(card, se.EQUITY, acct) == int(se.EQUITY * se.NOTIONAL_CAP / 100.0)   # notional cap binds


def test_commission_is_per_share_with_a_one_dollar_floor():
    acct = Account()
    assert fee_for(1, acct) == 1.0 and fee_for(200, acct) == 1.0
    assert fee_for(400, acct) == 2.0 and fee_for(1590, acct) == 7.95


# ── 3. a split never changes money, plan or R ────────────────────────────────

ENTRY, SPLIT = "2026-09-24", "2026-09-25"
PLAN = {"long": dict(fill="buy", inv=95.0, zone=[112.0, 116.0], far=116.0),
        "short": dict(fill="short", inv=106.0, zone=[86.0, 88.0], far=86.0)}


def _round(tmp_path, name, side, contract, factor):
    p = PLAN[side]
    lg = Ledger(data_dir=tmp_path / name)
    lg.init_account(FREE)
    lg.append("fill", {"symbol": "AAA", "side": p["fill"], "qty": 100, "price": 100.0, "fee": 0.0, "trade_date": ENTRY,
                       "card_id": "c1", "setup_type": "breakout", "stop_price": p["inv"], "invalidation": p["inv"],
                       "target_zone": p["zone"], "risk_per_share": abs(100.0 - p["inv"]), "max_holding_days": 7,
                       "quote_ts": _at(ENTRY, "10:00"), **({"accounting": "contract"} if contract else {})},
              at=_at(ENTRY, "10:00"))
    nav_before = lg.nav({"AAA": 103.0})
    if factor != 1.0:
        lg.append("corp_action", {"symbol": "AAA", "factor": factor, "action_id": "s1", "action_type": "forward_split",
                                  "ex_date": SPLIT, "trade_date": SPLIT}, at=_at(SPLIT, "08:00"))
    assert lg.nav({"AAA": 103.0 / factor}) == pytest.approx(nav_before, abs=0.01)
    return lg, SwingTrader(lg, FREE), p


@pytest.mark.parametrize("factor", [2.0, 8.0, 1.5, 0.1])
@pytest.mark.parametrize("contract", [False, True])
@pytest.mark.parametrize("side", ["long", "short"])
def test_split_leaves_cash_plan_and_r_unchanged(tmp_path, side, contract, factor):
    day = date.fromisoformat(SPLIT)
    base, base_tr, p = _round(tmp_path, "base", side, contract, 1.0)
    lg, tr, _ = _round(tmp_path, "split", side, contract, factor)
    pos = lg.position("AAA")
    assert abs(pos.qty) == pytest.approx(100 * factor) and pos.stop_price == pytest.approx(p["inv"] / factor)

    # the frozen stop scales with the price: one dollar inside keeps the position, one dollar outside ends it
    inside, outside = (p["inv"] + 1, p["inv"] - 1) if side == "long" else (p["inv"] - 1, p["inv"] + 1)
    assert tr.manage_close("AAA", close_price=inside / factor, trade_date=day, at=_at(SPLIT, "16:00")) == []
    _probe, probe_tr, _ = _round(tmp_path, "probe", side, contract, factor)
    (intent,) = probe_tr.manage_close("AAA", close_price=outside / factor, trade_date=day, at=_at(SPLIT, "16:00"))
    assert intent["kind"] == "exit_intent" and intent["reason"] == "invalidation"

    # reaching the far target closes the round with the same money and the same R as without the split
    (a,) = base_tr.manage_intraday("AAA", price=p["far"], trade_date=day, at=_at(SPLIT, "10:30"))
    (b,) = tr.manage_intraday("AAA", price=p["far"] / factor, trade_date=day, at=_at(SPLIT, "10:30"))
    assert a["kind"] == b["kind"] == "close" and a["reason"] == b["reason"] == "target_zone_upper"
    assert b["qty"] == pytest.approx(100 * factor) and lg.position("AAA") is None
    assert lg.cash == pytest.approx(base.cash, abs=0.02)
    assert b["r_multiple"] == pytest.approx(a["r_multiple"], abs=0.01)


# ── shared rig: a real TradingLoop over a temp data dir ──────────────────────

class _Repo:
    def __init__(self, path, closes=None):
        self.store = SimpleNamespace(data_dir=path)
        self.closes = closes or {}

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["AAA"], "sector": ["Technology"]})

    def get_daily(self, sym, start, end):
        v = self.closes.get(sym)
        return pl.DataFrame({"date": [end], "close": [v], "raw_close": [v]}) if v is not None else pl.DataFrame()


def _loop(tmp_path, monkeypatch, now_iso, closes=None, account=None):
    now = datetime.fromisoformat(now_iso).replace(tzinfo=ET)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(notify.feishu_app, "is_configured", lambda: False)
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_url", lambda: "")
    loop = TradingLoop(_Repo(tmp_path, closes), account or Account())
    loop.boot()
    return loop, now


def _seed(tmp_path, now, actions):
    svc = CorporateActionService(tmp_path)
    svc._atomic_write(svc.cache_path, {"fetched_at": now.isoformat(), "symbols": ["AAA"], "window": {}, "actions": actions})
    svc._atomic_write(svc.state_path, {"last_attempt_at": now.isoformat(), "ok": True, "error": None})


def _open(loop, day, *, side="buy", qty=100, contract=True, symbol="AAA"):
    stop, zone = (90.0, [112.0, 116.0]) if side == "buy" else (110.0, [86.0, 88.0])
    return loop.ledger.append("fill", {
        "symbol": symbol, "side": side, "qty": qty, "price": 100.0, "fee": 0.0, "trade_date": day, "card_id": f"c-{symbol}",
        "stop_price": stop, "invalidation": stop, "target_zone": zone, "risk_per_share": 10.0, "max_holding_days": 7,
        **({"accounting": "contract"} if contract else {})}, at=_at(day, "10:00"))


def _outbox(tmp_path, prefix):
    db = sqlite3.connect(tmp_path / "notifications" / "outbox.sqlite3")
    try:
        return [r for r in db.execute("select key, payload from outbox").fetchall() if r[0].startswith(prefix)]
    finally:
        db.close()


# ── 4. dividends: longs receive, shorts pay, only shares held before the ex-date ──

def _dividend(ex, rate, action_id="d1"):
    return {"id": action_id, "type": "cash_dividend", "symbol": "AAA", "ex_date": ex, "payable_date": ex,
            "auto": True, "rate": rate, "special": False}


@pytest.mark.real_corporate_actions
@pytest.mark.parametrize("side,contract,expected", [
    ("buy", True, 50.0), ("buy", False, 50.0), ("short", True, -50.0), ("short", False, -50.0),
])
def test_dividend_is_received_by_longs_and_paid_by_shorts(tmp_path, monkeypatch, side, contract, expected):
    loop, now = _loop(tmp_path, monkeypatch, "2026-09-29T08:00:00")
    _open(loop, "2026-09-28", side=side, contract=contract)
    cash = loop.ledger.cash
    _seed(tmp_path, now, [_dividend("2026-09-29", 0.5)])
    (event,) = loop.apply_corporate_actions(now)
    assert event["cash_dividend"] == expected and loop.ledger.cash == pytest.approx(cash + expected)
    assert loop.apply_corporate_actions(now) == []                      # booked once


@pytest.mark.real_corporate_actions
def test_dividend_ignores_shares_bought_on_the_ex_date_or_sold_before_it(tmp_path, monkeypatch):
    loop, now = _loop(tmp_path, monkeypatch, "2026-09-29T15:00:00")
    _seed(tmp_path, now, [_dividend("2026-09-29", 0.5)])
    _open(loop, "2026-09-29")                                           # bought on the ex-date: price already ex-dividend
    assert loop.apply_corporate_actions(now) == []

    other, now2 = _loop(tmp_path / "b", monkeypatch, "2026-09-29T08:00:00")
    _open(other, "2026-09-25")
    other.ledger.append("partial_close", {"symbol": "AAA", "qty": 40, "price": 104.0, "fee": 0.0, "reason": "target_zone_lower",
                                          "trade_date": "2026-09-28"}, at=_at("2026-09-28", "11:00"))
    _seed(tmp_path / "b", now2, [_dividend("2026-09-29", 0.5)])
    (event,) = other.apply_corporate_actions(now2)
    assert event["cash_dividend"] == 30.0                               # 60 shares still held at the ex-date


# ── 5. split day: a price from before the split must be rescaled to the share basis ──

def _split_event(loop, day, factor):
    loop.ledger.append("corp_action", {"symbol": "AAA", "factor": factor, "action_id": "s1", "action_type": "forward_split",
                                       "ex_date": day, "trade_date": day}, at=_at(day, "08:00"))


@pytest.mark.parametrize("side", ["buy", "short"])
@pytest.mark.parametrize("contract", [True, False])
def test_breaker_values_an_unquoted_split_position_on_the_ledgers_share_basis(tmp_path, monkeypatch, side, contract):
    """Ex-date round with no quote for the holding: yesterday's close is a pre-split price while the ledger
    already holds post-split shares. Unscaled, a 2:1 split reads as +/-12% on the day and trips (or hides) the halt."""
    loop, now = _loop(tmp_path, monkeypatch, "2026-10-02T09:31:00", closes={"AAA": 120.0})
    _open(loop, "2026-10-01", side=side, contract=contract)
    baseline = loop.ledger.nav({"AAA": 120.0})                          # 10-01 official close, pre-split shares
    nav_dir = tmp_path / "ledger" / "nav"
    nav_dir.mkdir(parents=True, exist_ok=True)
    (nav_dir / "daily.jsonl").write_text(json.dumps({"date": "2026-10-01", "nav": baseline}) + "\n")
    _split_event(loop, "2026-10-02", 2.0)

    if side == "short" and not contract:
        assert loop._breaker_inputs(now, {}) == (None, None, None)      # stock-style shorts need a live quote
        return
    nav_now, prev_close, peak = loop._breaker_inputs(now, {})
    assert nav_now == pytest.approx(prev_close)
    state = loop.breaker.update(day=now.date(), nav_now=nav_now, prev_close_nav=prev_close, peak_nav=peak)
    assert state["block_new_entries"] is False and state["risk_multiplier"] == 1.0


def test_breaker_basis_is_untouched_by_earlier_splits(tmp_path, monkeypatch):
    loop, now = _loop(tmp_path, monkeypatch, "2026-10-02T09:31:00", closes={"AAA": 60.0})
    _open(loop, "2026-09-30")
    _split_event(loop, "2026-10-01", 2.0)                               # yesterday's close is already post-split
    nav_now, _, _ = loop._breaker_inputs(now, {})
    assert nav_now == pytest.approx(loop.ledger.nav({"AAA": 60.0}))


@pytest.fixture
def split_morning(tmp_path, monkeypatch):
    """08:30 ET on the ex-date of a 2:1 split: the only prices available are from before the split."""
    from app.api.portfolio import router
    now = datetime(2026, 10, 2, 8, 30, tzinfo=ET)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "today", lambda: now.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10.0, "fee": 0.0, "trade_date": "2026-09-30",
                        "card_id": "c1", "setup_type": "breakout", "stop_price": 9.0, "invalidation": 9.0,
                        "target_zone": [12.0, 13.0], "risk_per_share": 1.0, "max_holding_days": 7}, at=_at("2026-09-30", "10:00"))
    led.append("corp_action", {"symbol": "AAA", "factor": 2.0, "action_id": "s1", "action_type": "forward_split",
                               "ex_date": "2026-10-02", "trade_date": "2026-10-02"}, at=_at("2026-10-02", "08:00"))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [11.0]}))
    return TestClient(app), monkeypatch


def test_portfolio_page_rescales_yesterdays_close_on_the_split_morning(split_morning):
    client, monkeypatch = split_morning
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    (p,) = client.get("/api/portfolio/live").json()["positions"]
    assert p["qty"] == 200 and p["last_price"] == 5.5 and p["market_value"] == 1100.0
    assert p["unrealized_pnl"] == pytest.approx(100.0)                  # 11 vs 10 before the split, not +1200
    assert client.get("/api/portfolio/overview").json()["nav"] == pytest.approx(100_000 - 1000 + 1100)


def test_portfolio_page_rescales_the_last_tick_left_from_before_the_split(split_morning):
    client, monkeypatch = split_morning
    tick = int(datetime(2026, 10, 1, 15, 59, 55, tzinfo=ET).timestamp() * 1000)
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {"AAA": (11.0, tick, "tickflow")})
    (p,) = client.get("/api/portfolio/live").json()["positions"]
    assert p["last_price"] == 5.5 and p["market_value"] == 1100.0


def test_opportunity_page_capacity_values_holdings_on_the_same_share_basis(tmp_path, monkeypatch):
    from app.services import desk_overview as ov
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 10, 2))
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10.0, "fee": 0.0, "trade_date": "2026-09-30",
                        "card_id": "c1", "stop_price": 9.0}, at=_at("2026-09-30", "10:00"))
    led.append("corp_action", {"symbol": "AAA", "factor": 2.0, "action_id": "s1", "action_type": "forward_split",
                               "ex_date": "2026-10-02", "trade_date": "2026-10-02"}, at=_at("2026-10-02", "08:00"))
    repo = SimpleNamespace(get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [11.0]}))
    marks, origin = ov._marks(repo, led)
    assert marks == {"AAA": 5.5} and origin["AAA"]["source"] == "close"
    assert led.nav(marks) == pytest.approx(100_000 - 1000 + 1100)


def test_portfolio_page_keeps_prices_printed_after_the_split(split_morning):
    client, monkeypatch = split_morning
    tick = int(datetime(2026, 10, 2, 8, 29, 0, tzinfo=ET).timestamp() * 1000)
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {"AAA": (5.6, tick, "tickflow")})
    (p,) = client.get("/api/portfolio/live").json()["positions"]
    assert p["last_price"] == 5.6


# ── 6. a holding hit by an action the system does not apply is reported once ──

def _manual(kind, ex):
    return {"id": f"m-{kind}", "type": kind, "symbol": "AAA", "ex_date": ex, "auto": False}


@pytest.mark.real_corporate_actions
@pytest.mark.parametrize("kind,label", [("name_change", "更名"), ("cash_merger", "现金并购"), ("spin_off", "分拆"),
                                        ("worthless_removal", "摘牌"), ("stock_dividend", "股票分红")])
def test_holding_hit_by_an_unapplied_corporate_action_is_reported_once(tmp_path, monkeypatch, kind, label):
    loop, now = _loop(tmp_path, monkeypatch, "2026-09-29T08:00:00")
    _open(loop, "2026-09-28", qty=137)
    _seed(tmp_path, now, [_manual(kind, "2026-09-30")])
    loop._enqueue_events()
    loop._enqueue_events()
    restarted, _ = _loop(tmp_path, monkeypatch, "2026-09-29T09:31:00")
    restarted._enqueue_events()
    rows = _outbox(tmp_path, "corp:held:")
    assert len(rows) == 1
    assert "AAA" in rows[0][1] and label in rows[0][1] and "137" in rows[0][1] and "2026-09-30" in rows[0][1]
    assert not [e for e in loop.ledger.read_events() if e["kind"] not in ("fill",)]      # reported, ledger untouched


@pytest.mark.real_corporate_actions
@pytest.mark.parametrize("entry,action", [
    ("2026-09-28", _manual("name_change", "2026-10-20")),        # takes effect after the 7-day holding limit
    ("2026-09-30", _manual("name_change", "2026-09-30")),        # bought on the effective date
    ("2026-09-28", _dividend("2026-09-30", 0.5)),                # applied automatically: has its own message
    ("2026-09-28", {**_manual("name_change", "2026-09-30"), "symbol": "ZZZ"}),   # not held
])
def test_no_report_when_the_action_cannot_touch_the_holding(tmp_path, monkeypatch, entry, action):
    loop, now = _loop(tmp_path, monkeypatch, "2026-09-30T15:00:00" if entry == "2026-09-30" else "2026-09-29T08:00:00")
    _open(loop, entry)
    _seed(tmp_path, now, [action])
    loop._enqueue_events()
    assert _outbox(tmp_path, "corp:held:") == []


# ── 7. same bar: target first, stops are close-confirmed ─────────────────────

KW = dict(entry_zone=(100.0, 101.0), invalidation=94.0, target=(112.0, 114.0), risk=6.5)


def _bars(*days):
    return tuple(np.array(col, dtype=float) for col in zip(*days, strict=True))


def test_intraday_wick_through_the_stop_does_not_end_the_round():
    o, h, low, c = _bars((100.5, 101.0, 100.0, 100.5), (100.0, 101.0, 90.0, 99.0))
    assert cr.replay_state(o, h, low, c, **KW)["status"] == "triggered"
    assert se.simulate_native_daily(o, h, low, c, **KW)["status"] == "open_at_end"


def test_target_reached_and_close_below_the_stop_on_one_bar_counts_the_target():
    o, h, low, c = _bars((100.5, 101.0, 100.0, 100.5), (100.0, 115.0, 90.0, 93.0), (93.0, 93.5, 92.0, 92.5))
    a, b = se.simulate_native_daily(o, h, low, c, **KW), cr.replay_state(o, h, low, c, **KW)
    assert a["status"] == b["status"] == "closed" and a["exit_reason"] == b["exit_reason"] == "target_zone_upper"
    entry = 100.5 * (1 + se.SLIP)
    gross = (0.5 * 112.0 * (1 - se.SLIP) + 0.5 * 114.0 * (1 - se.SLIP) - entry) / KW["risk"]
    assert a["r"] == b["r"] == pytest.approx(gross - 1.5 * se.FEE_R(KW["risk"], entry), abs=1e-3)
