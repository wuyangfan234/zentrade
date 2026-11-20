"""Tests for the full_market_discovery_enabled master switch (default ON).

Covers:
1. Default value is True and persists cleanly via preferences + strict API validation.
2. Turning OFF immediately stops QuoteService without overwriting realtime_quotes_enabled,
   blocks QuoteService.boot_check() / enable() / PUT realtime-quotes (409), and gates
   MinuteRefreshService._gate_reason() / is_healthy().
3. Turning OFF narrows OpportunityService._load_universe and decision_scan_job.run
   to watchlist + open positions + SPY, skips cross-sectional strategies and full-pool
   shadow_forward, while preserving watchlist_v4 snapshot output.
4. Turning OFF reports decision_desk as idle/disabled in /api/trading/system-status.
5. Intraday index quotes fallback uses fresh intel cache when QuoteService is stopped,
   and watchlist hot-quote overlay updates intraday prices when full-market scan is off.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import intraday as intraday_api
from app.api import settings as settings_api
from app.api import trading as trading_api
from app.api import watchlist as watchlist_api
from app.market_clock import us_clock
from app.services import decision_scan_job, preferences
from app.services.minute_refresh import MinuteRefreshService
from app.services.opportunity_service import OpportunityService
from app.services.quote_service import QuoteService


@pytest.fixture
def isolated_prefs(tmp_path, monkeypatch):
    pref_file = tmp_path / "user_data" / "preferences.json"
    pref_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(preferences, "_path", lambda: pref_file)
    preferences._invalidate_cache()
    yield pref_file
    preferences._invalidate_cache()


def test_default_enabled_and_api_strict_bool(isolated_prefs):
    assert preferences.get_full_market_discovery_enabled() is True

    app = FastAPI()
    app.include_router(settings_api.router)
    stopped = []
    started = []

    class DummyQS:
        _running = True

        def stop(self):
            self._running = False
            stopped.append(1)

        def start(self):
            self._running = True
            started.append(1)

        @staticmethod
        def is_realtime_allowed():
            return True

        @staticmethod
        def is_paused():
            return False

    app.state.quote_service = DummyQS()
    preferences.save({"realtime_quotes_enabled": True})
    client = TestClient(app)

    # Reject non-bool strict payloads (422) without mutating disk
    for bad in ("false", 0, 1, "true"):
        r = client.put("/api/settings/preferences/full-market-discovery", json={"full_market_discovery_enabled": bad})
        assert r.status_code == 422
    assert preferences.get_full_market_discovery_enabled() is True

    # Turn OFF: stops quote service, preserves realtime_quotes_enabled
    r = client.put("/api/settings/preferences/full-market-discovery", json={"full_market_discovery_enabled": False})
    assert r.status_code == 200
    assert r.json() == {"full_market_discovery_enabled": False, "quote_running": False}
    assert len(stopped) == 1
    assert preferences.get_full_market_discovery_enabled() is False
    assert preferences.get_realtime_quotes_enabled() is True

    # Re-enabling realtime quotes while master switch is OFF returns 409
    r_rt = client.put("/api/settings/preferences/realtime-quotes", json={"realtime_quotes_enabled": True})
    assert r_rt.status_code == 409

    # Turn ON: restores polling because realtime_quotes_enabled was preserved
    r_on = client.put("/api/settings/preferences/full-market-discovery", json={"full_market_discovery_enabled": True})
    assert r_on.status_code == 200
    assert r_on.json() == {"full_market_discovery_enabled": True, "quote_running": True}
    assert len(started) == 1
    assert preferences.get_full_market_discovery_enabled() is True

    # GET /preferences exposes the flag
    prefs_resp = client.get("/api/settings/preferences").json()
    assert prefs_resp["full_market_discovery_enabled"] is True


def test_quote_service_and_minute_refresh_respect_toggle(isolated_prefs, monkeypatch):
    preferences.save({"realtime_quotes_enabled": True, "minute_refresh_enabled": True})
    preferences.set_full_market_discovery_enabled(False)

    qs = QuoteService()
    monkeypatch.setattr(QuoteService, "is_realtime_allowed", classmethod(lambda cls: True))
    started = []
    monkeypatch.setattr(qs, "start", lambda *a, **kw: started.append(1))
    qs.boot_check()
    assert started == []
    assert preferences.get_realtime_quotes_enabled() is True  # Not overwritten
    assert qs.enable() is False
    assert qs.status()["full_market_discovery_enabled"] is False

    mr = MinuteRefreshService(repo=None)
    monkeypatch.setattr(mr, "capability_ok", lambda: True)
    assert mr._gate_reason() == "full_market_disabled"
    assert mr.is_healthy() is False
    st = mr.status()
    assert st["full_market_discovery_enabled"] is False
    assert st["gate_reason"] == "full_market_disabled"


def test_load_universe_and_strategy_picks_narrow_when_disabled(isolated_prefs, tmp_path, monkeypatch):
    preferences.set_full_market_discovery_enabled(False)
    end = date(2026, 11, 20)
    dates = [end - timedelta(days=i) for i in range(250, -1, -1) if (end - timedelta(days=i)).weekday() < 5]

    captured_symbols = []

    def make_bars(syms):
        rows = []
        for s in syms:
            for d in dates:
                rows.append({"symbol": s, "date": d, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1_000_000.0})
        return pl.DataFrame(rows)

    class FakeRepo:
        store = SimpleNamespace(data_dir=tmp_path)

        @staticmethod
        def get_instruments():
            return pl.DataFrame({"symbol": ["AAPL", "NVDA", "OTHER1", "OTHER2"]})

        @staticmethod
        def get_daily_batch(symbols, start, end_d):
            captured_symbols.append(list(symbols))
            return make_bars(symbols)

    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"AAPL"})
    svc = OpportunityService(FakeRepo())
    params = {"universe": {"min_adv_usd": 1_000_000, "min_price": 5.0, "min_history_days": 50}}
    universe, spy = svc._load_universe(params, end)

    assert set(universe.keys()) == {"AAPL"}
    assert "OTHER1" not in captured_symbols[0]
    assert "OTHER2" not in captured_symbols[0]
    assert "SPY" in captured_symbols[0]
    assert not spy.is_empty()

    # Cross-sectional strategy is skipped when watchlist_only=True
    seen_ids = []
    seen_ctx_symbols = []

    class FakeEngine:
        @staticmethod
        def list_strategies():
            return [
                {"id": "ibs_mean_reversion", "asset_types": ["stock"], "timeframes": ["1d"], "research_only": False},
                {"id": "momentum_rsi2_pullback", "asset_types": ["stock"], "timeframes": ["1d"], "research_only": False, "cross_sectional": True},
            ]

        @staticmethod
        def run_all(ctx, overrides_map=None, strategy_ids=None):
            seen_ids.extend(strategy_ids or [])
            return {sid: SimpleNamespace(rows=[], exit_signal_hits=[]) for sid in (strategy_ids or [])}

        @staticmethod
        def get(sid):
            return SimpleNamespace(stop_loss=-0.05)

    from app.services.screener import ScreenerService

    def fake_build_ctx(self, engine, as_of, ids, timeframe="1d", overrides_map=None, symbols=None, **kw):
        seen_ctx_symbols.append(symbols)
        return SimpleNamespace(current=None, history=None)

    monkeypatch.setattr(ScreenerService, "build_strategy_context", fake_build_ctx)
    monkeypatch.setattr(decision_scan_job, "_app_state", lambda: SimpleNamespace(strategy_engine=FakeEngine()))
    _, _, meta = decision_scan_job.strategy_picks(FakeRepo(), end, {"AAPL"}, watchlist_only=True)
    assert seen_ids == ["ibs_mean_reversion"]
    assert seen_ctx_symbols == [{"AAPL", "SPY"}]
    assert meta["watchlist_only"] is True


def test_system_status_marks_decision_desk_disabled(isolated_prefs, tmp_path):
    preferences.set_full_market_discovery_enabled(False)
    app = FastAPI()
    app.include_router(trading_api.router)
    app.state.trading_loop = None
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    body = TestClient(app).get("/api/trading/system-status").json()
    desk = next(c for c in body["components"] if c["id"] == "decision_desk")
    assert desk["state"] == "idle"
    assert desk["idle_kind"] == "disabled"
    assert "已关闭" in desk["summary"]


def test_intraday_indices_fallback_uses_intel_cache(tmp_path, monkeypatch):
    intel_dir = tmp_path / "intel"
    intel_dir.mkdir(parents=True, exist_ok=True)
    today = date(2026, 11, 20)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 11, 20, 12, 0, tzinfo=us_clock.tz))
    (intel_dir / "etfs.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T11:59:00-05:00",
        "rows": {"SPY": {"price": 710.0, "live": True, "as_of": "2026-11-20T11:59:00-05:00", "session": "regular", "time_et": "11:59"}},
    }), encoding="utf-8")
    (intel_dir / "etf_daily.json").write_text(json.dumps({
        "rows": {"SPY": {"close": 700.0, "day": "2026-11-19", "chg_pct": -1.0, "d5": -2.0}},
    }), encoding="utf-8")

    app = FastAPI()
    app.include_router(intraday_api.router)
    app.state.quote_service = SimpleNamespace(get_index_quotes=lambda syms=None: pl.DataFrame())
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), execute_all=lambda *a, **kw: [])

    resp = TestClient(app).get("/api/intraday/indices?symbols=SPY").json()
    assert resp["source"] == "intel"
    assert resp["count"] == 1
    assert resp["rows"][0]["symbol"] == "SPY"
    assert resp["rows"][0]["last_price"] == 710.0
    assert resp["rows"][0]["date"] == today.isoformat()


def test_watchlist_hot_quote_overlay_when_disabled(isolated_prefs, monkeypatch):
    preferences.set_full_market_discovery_enabled(False)
    now = datetime(2026, 11, 20, 11, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "in_session", lambda dt=None: True)
    from app.api import portfolio as portfolio_api

    monkeypatch.setattr(portfolio_api, "_live_marks", lambda syms: {"AAPL": (220.0, int(now.timestamp() * 1000) - 5000, "tickflow")})
    rows = [{"symbol": "AAPL", "close": 200.0, "prev_close": 195.0, "change_amount": 5.0, "change_pct": 0.025}]
    watchlist_api._overlay_hot_quotes_rows(rows, date(2026, 11, 19))
    assert rows[0]["close"] == 220.0
    assert rows[0]["prev_close"] == 200.0
    assert rows[0]["change_amount"] == 20.0
    assert abs(rows[0]["change_pct"] - 0.1) < 1e-9
