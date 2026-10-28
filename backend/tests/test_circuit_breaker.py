"""Circuit breaker tests (approved 2026-10-01/02: day stop, -10% drawdown half risk; 2026-10-03 day stop -2% -> -3%;
recovery: next trading day / drawdown back within -5%). Only limits NEW entries; never force-closes."""
from datetime import date

from app.services.circuit_breaker import CircuitBreaker, evaluate


def test_day_loss_three_percent_blocks_new_entries_today_only():
    s = evaluate(day=date(2026, 10, 2), nav_now=96_900, prev_close_nav=100_000, peak_nav=100_000, state={})
    assert s["block_new_entries"] is True and s["day_halt"] is True
    assert "3%" in s["rules"]["day_halt"]
    nxt = evaluate(day=date(2026, 10, 5), nav_now=96_900, prev_close_nav=96_900, peak_nav=100_000, state=s["state"])
    assert nxt["block_new_entries"] is False  # next trading day auto-recovers


def test_small_day_loss_does_not_block():
    # -2.9% (the old -2% line) no longer halts
    s = evaluate(day=date(2026, 10, 2), nav_now=97_100, prev_close_nav=100_000, peak_nav=100_000, state={})
    assert s["block_new_entries"] is False and s["risk_multiplier"] == 1.0


def test_drawdown_ten_percent_halves_risk_until_back_within_five():
    s = evaluate(day=date(2026, 10, 2), nav_now=89_900, prev_close_nav=90_000, peak_nav=100_000, state={})
    assert s["risk_multiplier"] == 0.5 and s["half_risk"] is True
    # recovered to -7%: still half (hysteresis)
    s2 = evaluate(day=date(2026, 10, 5), nav_now=93_000, prev_close_nav=92_500, peak_nav=100_000, state=s["state"])
    assert s2["risk_multiplier"] == 0.5
    # back within -5%: restored
    s3 = evaluate(day=date(2026, 10, 6), nav_now=95_100, prev_close_nav=94_800, peak_nav=100_000, state=s2["state"])
    assert s3["risk_multiplier"] == 1.0 and s3["half_risk"] is False


def test_breaker_persists_and_logs_transitions(tmp_path):
    cb = CircuitBreaker(tmp_path)
    s = cb.update(day=date(2026, 10, 2), nav_now=97_000, prev_close_nav=100_000, peak_nav=100_000)
    assert s["block_new_entries"]
    again = CircuitBreaker(tmp_path).current(date(2026, 10, 2))
    assert again["block_new_entries"] is True
    log = (tmp_path / "circuit_breaker" / "events.jsonl").read_text().strip().splitlines()
    assert len(log) == 1 and "day_halt_on" in log[0]
    cb.update(day=date(2026, 10, 2), nav_now=96_000, prev_close_nav=100_000, peak_nav=100_000)
    assert len((tmp_path / "circuit_breaker" / "events.jsonl").read_text().strip().splitlines()) == 1  # no duplicate


def test_missing_prev_close_never_trips():
    s = evaluate(day=date(2026, 10, 2), nav_now=50_000, prev_close_nav=None, peak_nav=None, state={})
    assert s["block_new_entries"] is False and s["risk_multiplier"] == 1.0


# ── wiring into TradingLoop ────────────────────────────────────────
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services.trading_loop import TradingLoop


@pytest.fixture
def loop_rig(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 30, 11, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["AAPL", "MSFT"], "sector": ["Technology", "Software"]}),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [end], "close": [100.], "raw_close": [100.]}))
    loop = TradingLoop(repo, Account())
    loop.boot()
    monkeypatch.setattr(loop.earnings, "usable", lambda *a: True)
    monkeypatch.setattr(loop.earnings, "event_for", lambda *a: None)
    monkeypatch.setattr(loop.corp_actions, "usable", lambda *a: True)
    monkeypatch.setattr(loop.corp_actions, "covers", lambda *a: True)
    card = OpportunityCard("cb-AAPL", "AAPL", "breakout", date(2026, 9, 29), (99., 102.), 94., (110., 120.),
                           risk_per_share=6., reward_risk=2., evidence={"sector": "Technology"}, state=CardState.OPEN)
    loop.opp_svc.save_scan(ScanResult(card.created_date, cards=[card], gate="green"))
    return loop, clock


def _q(at, price=100.):
    return {"AAPL": {"symbol": "AAPL", "price": price, "timestamp": at.isoformat(), "feed": "iex",
                     "open": 100., "open_timestamp": at.replace(hour=9, minute=30, second=0).isoformat()}}


def test_day_halt_blocks_entry_and_reports_blocker(loop_rig, monkeypatch):
    loop, clock = loop_rig
    monkeypatch.setattr(loop, "_breaker_inputs", lambda now, quotes: (97_000.0, 100_000.0, 100_000.0))
    for _ in range(3):
        loop.intraday_fill_check(_q(clock[0]))
        clock[0] += timedelta(seconds=15)
    assert not loop.ledger.open_positions
    assert {"symbol": "*", "reason": "circuit_breaker_day_halt"} in loop.entry_blockers
    assert loop.status()["circuit_breaker"]["block_new_entries"] is True


def test_half_risk_halves_position_size(loop_rig, monkeypatch):
    loop, clock = loop_rig
    monkeypatch.setattr(loop, "_breaker_inputs", lambda now, quotes: (89_000.0, 89_500.0, 100_000.0))
    for _ in range(2):
        loop.intraday_fill_check(_q(clock[0]))
        clock[0] += timedelta(seconds=15)
    pos = loop.ledger.position("AAPL")
    assert pos is not None
    # 1% of ~$100k / $6 risk = 166 shares normally; half risk → 83 planned,
    # then the per-trade risk cap (2026-10-12) resizes on the actual fill-to-stop distance → 82
    assert pos.qty == 82
    fill = next(e for e in loop.ledger.read_events() if e["kind"] == "fill")
    assert fill["risk_cap"]["planned_qty"] == 83
    assert fill["qty"] * (fill["price"] - fill["invalidation"]) <= fill["risk_cap"]["budget"] + 1e-6


# ── same-basis NAV (2026-10-03 fix of the 10-02 false trip) ─────────────────

def _pos_rig(tmp_path, monkeypatch, closes):
    """Ledger with positions entered on 10-01 at cost 100; prev official closes from `closes`."""
    from app.ledger.core import Account as Acct
    clock = [datetime(2026, 10, 2, 9, 31, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    calls = []

    def get_daily(sym, start, end):
        calls.append((sym, start))
        v = closes.get(sym)
        return pl.DataFrame({"date": [start], "close": [v], "raw_close": [v]}) if v is not None else pl.DataFrame()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_instruments=lambda: pl.DataFrame(), get_daily=get_daily)
    loop = TradingLoop(repo, Acct())
    loop.boot()
    for sym in ("AAA", "BBB"):
        loop.ledger.append("fill", {"symbol": sym, "side": "buy", "qty": 100, "price": 100.0, "fee": 0.0,
                                    "trade_date": "2026-10-01", "card_id": f"c-{sym}", "stop_price": 90.0}, at="2026-10-01T10:00:00-04:00")
    nav_dir = tmp_path / "ledger" / "nav"
    nav_dir.mkdir(parents=True, exist_ok=True)
    cash = loop.ledger.cash
    (nav_dir / "daily.jsonl").write_text(
        '{"date": "2026-10-01", "nav": %s}\n' % (cash + 100 * closes["AAA"] + 100 * closes["BBB"]))
    return loop, clock, calls


def test_missing_quote_uses_prev_official_close_not_cost(tmp_path, monkeypatch):
    # both closed at 120 on 10-01 (+$4000 unrealized in the baseline); only AAA quoted at 120 this round
    loop, clock, calls = _pos_rig(tmp_path, monkeypatch, {"AAA": 120.0, "BBB": 120.0})
    nav_now, prev_close, _ = loop._breaker_inputs(clock[0], {"AAA": {"price": 120.0}})
    assert nav_now == prev_close  # flat day, BBB valued at its prev close, not at cost 100
    assert loop.ledger.nav({"AAA": 120.0}) == prev_close - 2000  # the old (cost) basis would have shown -2000
    # cached per day: second round does not re-read
    n = len(calls)
    loop._breaker_inputs(clock[0], {"AAA": {"price": 120.0}})
    assert len(calls) == n


def test_missing_prev_close_skips_judgement_instead_of_tripping(tmp_path, monkeypatch):
    loop, clock, _ = _pos_rig(tmp_path, monkeypatch, {"AAA": 120.0, "BBB": 120.0})
    loop.repo.get_daily = lambda *a, **k: pl.DataFrame()
    assert loop._breaker_inputs(clock[0], {"AAA": {"price": 120.0}}) == (None, None, None)
    st = loop.breaker.update(day=clock[0].date(), nav_now=None, prev_close_nav=None, peak_nav=None)
    assert st["block_new_entries"] is False


def test_replay_of_2026_10_02_open_no_longer_trips(tmp_path, monkeypatch):
    """10-02 09:30: real portfolio -1.275% at the open; old basis logged -2.638% and halted the day."""
    loop, clock, _ = _pos_rig(tmp_path, monkeypatch, {"AAA": 120.0, "BBB": 120.0})
    nav_now, prev_close, peak = loop._breaker_inputs(clock[0], {"AAA": {"price": 117.0}})  # BBB unquoted
    st = loop.breaker.update(day=clock[0].date(), nav_now=nav_now, prev_close_nav=prev_close, peak_nav=peak)
    assert st["block_new_entries"] is False and st["day_change_pct"] > -1.0


def test_real_three_percent_drop_with_all_quotes_still_trips(tmp_path, monkeypatch):
    loop, clock, _ = _pos_rig(tmp_path, monkeypatch, {"AAA": 120.0, "BBB": 120.0})
    nav_now, prev_close, peak = loop._breaker_inputs(clock[0], {"AAA": {"price": 100.0}, "BBB": {"price": 100.0}})
    st = loop.breaker.update(day=clock[0].date(), nav_now=nav_now, prev_close_nav=prev_close, peak_nav=peak)
    assert st["day_change_pct"] <= -3.0 and st["block_new_entries"] is True


# ── #2 熔断 fail-closed ──
def test_breaker_error_blocks_new_entries(loop_rig, monkeypatch):
    loop, clock = loop_rig

    def boom(now, quotes):
        raise RuntimeError("nav file corrupt")
    monkeypatch.setattr(loop, "_breaker_inputs", boom)
    for _ in range(3):
        loop.intraday_fill_check(_q(clock[0]))
        clock[0] += timedelta(seconds=15)
    assert not loop.ledger.open_positions
    assert {"symbol": "*", "reason": "circuit_breaker_unavailable"} in loop.entry_blockers
    st = loop.status()["circuit_breaker"]
    assert st["block_new_entries"] is True and st["unavailable"] is True
