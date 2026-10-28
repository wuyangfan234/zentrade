"""Report #5: extension must see the bar before its first ex-date.

Exercise the actual raw/all provider and factor persistence with offline bars.
Anchors are historical comparison dates, NOT normalization or future endpoint dates.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers import custom
from app.indicators import pipeline
from app.jobs.daily_pipeline import _adj_anchor_dates
from app.plugins.alpaca import provider as alp
from app.services import extend_history as eh
from app.services import kline_sync, preferences
from app.storage.repository import DataStore, KlineRepository

BEFORE = date(2024, 6, 7)
EX_DATE = date(2024, 6, 10)
EARLIEST = date(2024, 6, 17)


def _bar(day, close, volume):
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close, "l": close,
            "c": close, "v": volume, "vw": close}


def _write_daily(root, frame):
    for part in frame.partition_by("date"):
        path = root / "kline_daily" / f"date={part['date'][0]}" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(path)


def _offline_source(monkeypatch, factor, vol_factor):
    calls = []
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("offline", "offline"))

    def iter_bars(key, secret, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        calls.append((start.date(), end.date(), adjustment))
        for symbol in symbols:
            for day in (BEFORE, EX_DATE, EARLIEST):
                if start.date() <= day <= end.date():
                    pre_raw = day == BEFORE and adjustment == "raw"
                    close = 50.0 * factor if pre_raw else 50.0
                    volume = 10000.0 / vol_factor if pre_raw else 10000.0
                    yield symbol, _bar(day, close, volume)

    monkeypatch.setattr(alp, "_iter_bars", iter_bars)
    return alp.AlpacaProvider(), calls


@pytest.mark.parametrize("factor, vol_factor", [(10.0, 10.0), (1.02, 1.0)], ids=["split", "dividend"])
def test_extension_persists_first_day_ex_event_from_prior_week(tmp_path, monkeypatch, factor, vol_factor):
    provider, calls = _offline_source(monkeypatch, factor, vol_factor)
    latest = alp.AlpacaProvider._daily_rows_to_frame([
        alp.AlpacaProvider._daily_row("TEST", _bar(EARLIEST, 50.0, 10000.0))])
    _write_daily(tmp_path, latest)
    repo = KlineRepository(DataStore(tmp_path))
    refresh_view = eh._refresh_single_view
    refresh_view(repo, "kline_daily")
    monkeypatch.setattr(eh, "_resolve_universe", lambda *a: ["TEST"])
    monkeypatch.setattr(eh, "_invalidate", lambda *a: None)
    monkeypatch.setattr(eh, "_refresh_single_view", lambda repo, name:
                        refresh_view(repo, name) if name != "kline_enriched" else None)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda: 0)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "alpaca")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *a: True)
    monkeypatch.setattr(custom, "get_provider", lambda *a: provider)

    def sync_daily(symbols, repo, capset, *, start_date, end_date, **kwargs):
        frame = provider.get_daily(symbols, start_date, end_date)
        _write_daily(tmp_path, frame)
        return frame.height

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", sync_daily)
    try:
        result = eh.run_extend_history(repo, SimpleNamespace(has=lambda *a: True), 7, "day")
        assert result["earliest_after"] == EX_DATE.isoformat()
        assert result["adj_factor_rows"] == 1
        events = pl.read_parquet(tmp_path / "adj_factor" / "all.parquet")
        assert events.to_dicts() == [{"symbol": "TEST", "trade_date": EX_DATE,
                                      "ex_factor": pytest.approx(factor), "vol_factor": pytest.approx(vol_factor)}]
        factor_calls = [row for row in calls if row[2] == "all"]
        assert factor_calls[0][0] == EX_DATE - timedelta(days=7)
        # A true backward extension has no local bar before its new start. The
        # main pipeline's real lookup agrees: do not invent an EARLIEST/today anchor.
        assert _adj_anchor_dates(repo, ["TEST"], EX_DATE - timedelta(days=7)) == {}
        # The first ex-date is already on the post-event basis. Its newly
        # recovered event must not readjust that day or any later daily bar.
        raw = provider.get_daily(["TEST"], datetime.combine(EX_DATE, datetime.min.time()),
                                 datetime.combine(EARLIEST, datetime.min.time()))
        adjusted = pipeline._apply_adj_factor(raw, events)
        assert adjusted["close"].to_list() == [50.0, 50.0]
        assert adjusted["volume"].to_list() == [10000.0, 10000.0]
    finally:
        repo.db.close()


@pytest.mark.parametrize("anchor", [None, BEFORE, EARLIEST], ids=["absent", "prior", "future"])
def test_real_provider_anchor_can_only_extend_left_edge(monkeypatch, anchor):
    provider, calls = _offline_source(monkeypatch, 10.0, 10.0)
    events = provider.get_adj_factors(
        ["TEST"], datetime.combine(EX_DATE, datetime.min.time()),
        datetime.combine(EARLIEST, datetime.min.time()),
        anchor_dates={} if anchor is None else {"TEST": anchor},
    )
    assert {call[1] for call in calls} == {EARLIEST}  # anchor never moves the right edge
    if anchor == BEFORE:
        assert {call[0] for call in calls} == {BEFORE}
        assert events.to_dicts() == [{"symbol": "TEST", "trade_date": EX_DATE,
                                      "ex_factor": pytest.approx(10.0), "vol_factor": pytest.approx(10.0)}]
    else:
        assert {call[0] for call in calls} == {EX_DATE}
        assert events.is_empty()  # no before-bar means no fabricated first-day event
