"""0.16.1 交易逻辑修复回归 (2026-10-14 产品决策): #1 盘后定版用官方日K、缺价只跳过该只、补跑看完成+净值;
#2 后续帧复核入场区与失效价; #3 入场前置步骤异常只停开仓。由 /tmp/zt-repro1014 复现改写。"""
import json
import threading
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.services.trading_loop import TradingLoop
from app.storage.repository import KlineRepository
from app.trader.trader import SwingTrader

ET = us_clock.tz
DAY = date(2026, 9, 25)
PC_DAY = date(2026, 9, 29)


# ── #1 盘后定版 ─────────────────────────────────────────────────────
def test_official_daily_ignores_live_cache_override():
    repo = object.__new__(KlineRepository)
    official = pl.DataFrame({"symbol": ["X"], "date": [DAY], "close": [94.0], "raw_close": [94.0]})
    cached = pl.DataFrame({"symbol": ["X"], "date": [DAY], "close": [96.0], "raw_close": [96.0]})
    repo._scan_daily_symbol = lambda s, a, b, cols: official
    repo.get_enriched_latest = lambda: (cached, DAY)
    repo._filter_cached = lambda df, s, cols: df.filter(pl.col("symbol") == s)
    assert repo.get_official_daily("X", DAY, DAY)["raw_close"][-1] == 94.0


class PCRepo:
    """get_daily = 带盘中缓存覆盖的价; get_official_daily = 官方日K。"""
    def __init__(self, p, official, cached):
        self.store = SimpleNamespace(data_dir=p)
        self.official, self.cached = official, cached

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["A", "B"], "sector": ["Technology"] * 2})

    def _frame(self, src, s, end):
        return pl.DataFrame({"date": [end], "close": [src[s]], "raw_close": [src[s]]}) if s in src else pl.DataFrame()

    def get_daily(self, s, start, end, columns=None):
        return self._frame(self.cached, s, end)

    def get_official_daily(self, s, start, end):
        return self._frame(self.official, s, end)


def _pc_loop(tmp_path, monkeypatch, official, cached):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 9, 29, 16, 40, tzinfo=ET))
    loop = TradingLoop(PCRepo(tmp_path, official, cached), Account())
    loop.boot()
    for sym in ("A", "B"):
        loop.ledger.append("fill", {"symbol": sym, "side": "buy", "qty": 10, "price": 100., "fee": 1.,
                                    "trade_date": PC_DAY.isoformat(), "card_id": f"held-{sym}",
                                    "stop_price": 95., "invalidation": 95.}, at="2026-09-29T10:00:00-04:00")
    return loop


def _intents(loop):
    return [e for e in loop.ledger.read_events() if e["kind"] == "exit_intent"]


def test_post_close_uses_official_close_not_cached(tmp_path, monkeypatch):
    loop = _pc_loop(tmp_path, monkeypatch, official={"A": 94., "B": 100.}, cached={"A": 96., "B": 100.})
    r = loop.post_close_manage(PC_DAY, confirmed_official=True)
    assert [e["symbol"] for e in _intents(loop)] == ["A"] and _intents(loop)[0]["signal_price"] == 94.
    assert r["nav"]["market_value"] == 1940.


def test_post_close_missing_mark_skips_only_that_symbol_and_nav(tmp_path, monkeypatch):
    loop = _pc_loop(tmp_path, monkeypatch, official={"B": 94.}, cached={"A": 96., "B": 94.})
    r = loop.post_close_manage(PC_DAY, confirmed_official=True)
    assert not r.get("skipped") and r["missing_symbols"] == ["A"]
    assert [e["symbol"] for e in _intents(loop)] == ["B"]                      # B 照常判止损
    assert r["nav"] is None                                                     # 缺价不定版净值
    assert not (loop.ledger.data_dir / "nav" / "daily.jsonl").exists()


def test_pipeline_scans_even_when_a_mark_is_missing(tmp_path, monkeypatch):
    loop = _pc_loop(tmp_path, monkeypatch, official={"B": 100.}, cached={})
    scan = MagicMock(return_value=SimpleNamespace(cards=[], watch=[], gate="green"))
    loop.opp_svc.run_daily_scan = scan
    qs = SimpleNamespace(is_official_close=lambda d: True)
    monkeypatch.setattr(daily_pipeline, "_get_app_state_attr",
                        lambda name: {"trading_loop": loop, "quote_service": qs}.get(name))
    monkeypatch.setattr("app.services.notify_opportunity.enqueue_daily_opportunities", lambda *a, **k: None)
    r = daily_pipeline._run_zen_post_close(loop.repo, PC_DAY)
    assert scan.called and r["status"] == "completed" and r["nav"] is None and r["missing_symbols"] == ["A"]


SCHED = {"hour": 16, "minute": 30}


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _job(zen, started="2026-10-09T20:35:00Z"):
    return {"status": "succeeded", "started_at": started, "result": {"zen_trade": zen}}


def test_catchup_requires_completed_and_nav_written(cal):
    t = daily_pipeline.pipeline_catchup_target
    sun = datetime(2026, 10, 11, 21, 0, tzinfo=ET)
    fri = date(2026, 10, 9)
    assert t([_job({"status": "skipped", "reason": "official_close_not_confirmed"})], sun, SCHED,
             nav_dates={"2026-10-09"}) == fri
    assert t([_job({"status": "completed"})], sun, SCHED, nav_dates={"2026-10-08"}) == fri
    assert t([_job({"status": "completed"})], sun, SCHED, nav_dates={"2026-10-09"}) is None


# ── #2 入场成交 ─────────────────────────────────────────────────────
def card(**kw):
    d = dict(id="c1", symbol="AAPL", setup_type="breakout", created_date=DAY, entry_zone=(100., 102.),
             invalidation=95., target_zone=(112., 116.), risk_per_share=6., reward_risk=2.3, max_holding_days=7,
             state=CardState.ACTIVE)
    d.update(kw)
    return OpportunityCard(**d)


@pytest.fixture
def trader(tmp_path):
    acc = Account()
    led = Ledger(tmp_path / "ledger")
    led.init_account(acc)
    led.replay(acc)
    return SwingTrader(led, acc)


def tick(t, c, price, minute):
    ts = f"2026-09-25T10:{minute:02d}:00-04:00"
    return t.try_fill_entry(c, price=price, trade_date=DAY, at=ts, quote_ts=ts)


@pytest.mark.parametrize("side, zone, inv, beyond, outside, inside", [
    ("long", (100., 102.), 95., 94., 103., 101.5),
    ("short", (100., 102.), 107., 108., 99., 100.5),
])
def test_second_frame_must_stay_in_zone_and_inside_invalidation(trader, side, zone, inv, beyond, outside, inside):
    c = card(entry_zone=zone, invalidation=inv, side=side,
             target_zone=(112., 116.) if side == "long" else (88., 92.))
    assert tick(trader, c, 101., 0) is None                                    # 区内 → intent
    assert tick(trader, c, beyond, 1) is None                                  # 穿过失效 → 不成交
    assert tick(trader, c, outside, 2) is None                                 # 出区 → 不成交
    assert not any(e["kind"] == "fill" for e in trader.ledger.read_events())
    fill = tick(trader, c, inside, 3)                                          # 回到区内 → 成交
    assert fill is not None and fill["kind"] == "fill"
    assert trader.ledger.open_risk() > 0


def test_nonpositive_actual_risk_rejected_even_inside_zone(trader):
    # 入场区跨过失效价的坏卡: 区内但成交价已在失效外侧 → 拒单
    c = card(entry_zone=(94., 102.), invalidation=95.)
    assert tick(trader, c, 101., 0) is None
    assert tick(trader, c, 94.5, 1) is None
    assert not any(e["kind"] == "fill" for e in trader.ledger.read_events())


# ── #3 盘中流程 ─────────────────────────────────────────────────────
def _bare_loop(monkeypatch, *, fail_step):
    loop = object.__new__(TradingLoop)
    loop._boot, loop._run_lock = True, threading.Lock()
    loop.execution_source = {}
    loop.rejected_quotes = []
    loop.entry_blockers = []
    loop.ledger = SimpleNamespace(_lock=threading.Lock())

    def boom(*a, **k):
        raise pl.exceptions.SchemaError("partition schema mismatch")
    loop.ensure_watchlist_book = boom if fail_step == "book" else (lambda now: None)
    loop._cover_new_symbols = boom if fail_step == "cover" else (lambda now: None)
    loop.opp_svc = SimpleNamespace(advance_lifecycle=boom if fail_step == "lifecycle" else (lambda *a, **k: None),
                                   revalidate_unfilled=boom if fail_step == "revalidate" else (lambda now: {"AAPL"}))
    loop.execution_symbols = lambda: ["AAPL"]
    loop._execution_quotes = lambda p, s: ([{"symbol": "AAPL"}], [])
    seen = {}

    def fill_check(q, entry_symbols=None):
        seen["entry_symbols"] = entry_symbols
        return ["managed"]
    loop.intraday_fill_check = fill_check
    monkeypatch.setattr(us_clock, "in_session", lambda now: True)
    return loop, seen


@pytest.mark.parametrize("step", ["book", "cover", "lifecycle", "revalidate"])
def test_entry_prestep_error_only_stops_entries(monkeypatch, step):
    loop, seen = _bare_loop(monkeypatch, fail_step=step)
    assert loop.run_intraday(provider=object()) == ["managed"]                 # 持仓管理照常
    assert seen["entry_symbols"] == set()                                       # 不开新仓
    assert "SchemaError" in loop.last_error


def test_clean_run_clears_last_error(monkeypatch):
    loop, seen = _bare_loop(monkeypatch, fail_step=None)
    loop.last_error = "old"
    assert loop.run_intraday(provider=object()) == ["managed"]
    assert seen["entry_symbols"] == {"AAPL"} and loop.last_error is None
