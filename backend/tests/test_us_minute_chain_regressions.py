"""Production minute-chain regressions (offline, no credentials/network)."""
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.api import kline as api
from app.market_clock import us_clock
from app.plugins.alpaca import provider as alp
from app.services import kline_sync, minute_refresh

DAY = date(2026, 9, 25)


def frame(n=390, *, start=None):
    start = start or datetime(2026, 9, 25, 9, 30)
    return pl.DataFrame({
        "symbol": ["AAPL"] * n,
        "datetime": [start + timedelta(minutes=i) for i in range(n)],
        "open": [200.] * n, "high": [201.] * n, "low": [199.] * n,
        "close": [200.] * n, "volume": [100.] * n, "amount": [20000.] * n,
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = datetime(2026, 9, 25, 16, 20, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(api.us_clock, "now", lambda: now)
    monkeypatch.setattr(api.us_clock, "today", lambda: DAY)
    monkeypatch.setattr(api.trading_day, "is_trading_day", lambda *a: True)
    monkeypatch.setattr(api, "_get_stock_info", lambda *a: {"name": "Apple"})
    monkeypatch.setattr(api, "_get_previous_closes", lambda *a: {DAY: 200.})
    monkeypatch.setattr(api.minute_adjust, "minute_basis_is_raw", lambda *a: False)
    monkeypatch.setattr(api, "_gzip_payload", lambda req, payload, **kw: payload)
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "")
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.resolve_asset_type.return_value = "stock"
    repo.get_minute.return_value = frame(375)
    repo.get_minute_batch.return_value = frame(375)
    repo.get_etf_symbol_set.return_value = set()
    repo.latest_daily_date.return_value = date(2026, 9, 24)
    repo.latest_minute_date.return_value = date(2026, 9, 24)
    repo.latest_minute_date_global.return_value = date(2026, 9, 24)
    repo._write_lock = RLock()
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=MagicMock())), headers={})
    fetch = MagicMock(return_value=frame())
    monkeypatch.setattr(kline_sync, "fetch_minute_single", fetch)
    return req, fetch


def test_close_tail_must_not_freeze_at_90_percent(env):
    req, fetch = env
    body = api.get_minute(req, "AAPL", DAY, True)
    fetch.assert_called_once()
    assert body["rows"][-1]["datetime"].strftime("%H:%M") == "15:59"


def test_source_outage_preserves_existing_minutes(env):
    req, fetch = env
    req.app.state.repo.get_minute.return_value = frame(100)
    fetch.return_value = pl.DataFrame()
    body = api.get_minute(req, "AAPL", DAY, False)
    assert len(body["rows"]) == 100
    assert body["source"] == "local"


def test_missing_daily_after_close_does_not_rewind_minute_date(env):
    req, fetch = env
    body = api.get_minute(req, "AAPL", None, False)
    assert body["date"] == DAY.isoformat()
    assert body["rows"][-1]["datetime"].strftime("%H:%M") == "15:59"


def test_batch_missing_daily_after_close_does_not_rewind_date(env, monkeypatch):
    req, _ = env
    sync = MagicMock(return_value=frame())
    monkeypatch.setattr(kline_sync, "sync_minute_batch", sync)
    api.get_minute_batch(req, {"symbols": ["AAPL"]})
    assert req.app.state.repo.get_minute_batch.call_args.args[1] == DAY
    assert sync.call_args.kwargs["end_time"] == datetime(2026, 9, 25, 16, tzinfo=us_clock.tz)


def test_batch_source_outage_preserves_existing_minutes(env, monkeypatch):
    req, _ = env
    monkeypatch.setattr(kline_sync, "sync_minute_batch", lambda *a, **kw: pl.DataFrame())
    body = api.get_minute_batch(req, {"symbols": ["AAPL"], "date": DAY.isoformat()})
    assert len(body["data"]["AAPL"]) == 375


def test_old_premarket_cache_not_returned_or_counted_as_full(env):
    req, fetch = env
    req.app.state.repo.get_minute.return_value = frame(390, start=datetime(2026, 9, 25, 5, 25))
    body = api.get_minute(req, "AAPL", DAY, False)
    fetch.assert_called_once()
    assert len(body["rows"]) == 390
    assert min(r["datetime"].strftime("%H:%M") for r in body["rows"]) == "09:30"


def test_provider_rejects_inverted_sip_window_without_http(monkeypatch):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("test", "test"))
    http = MagicMock(return_value={"bars": {}})
    monkeypatch.setattr(alp, "_get", http)
    future = us_clock.now() + timedelta(days=2)
    assert alp.AlpacaProvider().get_minute(["AAPL"], future, future + timedelta(hours=1)).is_empty()
    http.assert_not_called()


@pytest.mark.parametrize(("day", "utc_hour"), [(date(2026, 9, 25), 13), (date(2026, 1, 15), 14)])
@pytest.mark.parametrize("aware", [True, False])
def test_et_request_serialization_in_both_dst_seasons(monkeypatch, day, utc_hour, aware):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("test", "test"))
    http = MagicMock(return_value={"bars": {}})
    monkeypatch.setattr(alp, "_get", http)
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=us_clock.tz if aware else None)
    alp.AlpacaProvider().get_minute(["AAPL"], start, start + timedelta(hours=1))
    assert f"start={day}T{utc_hour:02}:30:00Z" in http.call_args.args[0]


def test_regular_filter_preserves_every_session_minute():
    rows = frame(500, start=datetime(2026, 9, 25, 9))
    out = kline_sync._filter_regular_session(rows)
    assert len(out) == us_clock.total_minutes
    assert out["datetime"][0].strftime("%H:%M") == "09:30"
    assert out["datetime"][-1].strftime("%H:%M") == "15:59"


def test_sip_delayed_close_collection_window(monkeypatch, tmp_path):
    from app.services import preferences, trading_day
    svc = minute_refresh.MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(preferences, "get_minute_refresh_enabled", lambda: True)
    monkeypatch.setattr(svc, "capability_ok", lambda: True)
    monkeypatch.setattr(svc, "_resolve_custom", lambda: (SimpleNamespace(minute_delay_minutes=16), "alpaca"))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *a: True)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 9, 25, 16, 10, tzinfo=us_clock.tz))
    assert svc._gate_reason() is None
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 9, 25, 16, 19, tzinfo=us_clock.tz))
    assert svc._gate_reason() is None  # 还没有完成最终可用窗口的请求
    svc._finalized_date = DAY
    assert svc._gate_reason() == "outside_trading_hours"


def test_sip_delay_is_not_a_repair_gap(tmp_path, monkeypatch):
    svc = minute_refresh.MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(svc, "_resolve_custom", lambda: (SimpleNamespace(minute_delay_minutes=16), "alpaca"))
    monkeypatch.setattr(svc, "_today_coverage_lag_minutes", lambda: 17.0)
    assert svc._select_mode() == "increment"


def test_range_capable_source_recovers_large_gap_incrementally(tmp_path, monkeypatch):
    svc = minute_refresh.MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    provider = SimpleNamespace(minute_delay_minutes=16, get_intraday_since=lambda **kw: None)
    monkeypatch.setattr(svc, "_resolve_custom", lambda: (provider, "alpaca"))
    monkeypatch.setattr(svc, "_today_coverage_lag_minutes", lambda: 85.0)
    assert svc._select_mode() == "increment"


def test_window_increment_supported_without_universe_latest(tmp_path, monkeypatch):
    svc = minute_refresh.MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    assert svc._custom_supports_increment(SimpleNamespace(get_intraday_since=lambda **kw: None))


def test_refresh_health_uses_effective_interval(tmp_path, monkeypatch):
    svc = minute_refresh.MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(minute_refresh.preferences, "get_minute_refresh_enabled", lambda: True)
    monkeypatch.setattr(minute_refresh.preferences, "get_minute_refresh_interval", lambda: 6)
    monkeypatch.setattr(svc, "_effective_interval", lambda: 60)
    svc._thread = SimpleNamespace(is_alive=lambda: True)
    svc._state.last_round_at = minute_refresh.time.time() - 45
    assert svc.is_healthy()
