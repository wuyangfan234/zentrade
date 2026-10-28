"""Adjustment migration must survive crashes and long suspensions (independent review R1/R2/R6)."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

import app.plugins.alpaca.provider as alp
from app.indicators import pipeline
from app.jobs import daily_pipeline as dp
from app.services import kline_sync as ks
from app.services import preferences
from app.data_providers import custom

DAY0, DAY1, RECENT = date(2024, 6, 7), date(2024, 6, 10), date(2026, 9, 30)


def _bar(d, close, volume):
    return {"t": f"{d}T04:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": volume, "vw": close}


def _write_daily(root, symbols):
    rows = []
    for i, s in enumerate(symbols):
        scale = i + 1
        for d, c in ((DAY0, 100.0 * scale), (DAY1, 10.0 * scale), (RECENT, 10.0 * scale)):
            rows.append(alp.AlpacaProvider._daily_row(s, _bar(d, c, 1000.0)))
    frame = alp.AlpacaProvider._daily_rows_to_frame(rows)
    for part in frame.partition_by("date"):
        out = root / "kline_daily" / f"date={part['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(out)
    return frame


def _split_source(monkeypatch, failing: set[str]):
    def iter_bars(key, secret, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        for sym in symbols:
            if sym in failing and adjustment == "all":
                raise RuntimeError(f"injected {sym}")
            scale = 1 if sym == "A" else 2
            for d, raw_c, v in ((DAY0, 100.0 * scale, 1000), (DAY1, 10.0 * scale, 10000), (RECENT, 10.0 * scale, 10000)):
                if start.date() <= d <= end.date():
                    before = adjustment == "all" and d < DAY1
                    yield sym, _bar(d, raw_c / 10 if before else raw_c, v * 10 if before else v)
    monkeypatch.setattr(alp, "_BATCH", 1)
    monkeypatch.setattr(alp, "_iter_bars", iter_bars)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("offline", "offline"))
    monkeypatch.setattr(preferences, "get_enriched_batch_size", lambda: 2)
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda default, rows: 2)
    monkeypatch.setattr(pipeline, "_custom_signal_exprs", {})
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    symbols = ["A", "B"]
    frame = _write_daily(tmp_path, symbols)
    # 旧库: enriched 以错误口径物化 (未复权), 因子表为旧逐日水平口径
    stale = frame.with_columns(pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"), pl.col("low").alias("raw_low"))
    for part in stale.partition_by("date"):
        out = tmp_path / "kline_daily_enriched" / f"date={part['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(out)
    (tmp_path / "adj_factor").mkdir()
    pl.DataFrame({"symbol": symbols, "trade_date": [DAY0] * 2, "ex_factor": [10.0] * 2}).write_parquet(tmp_path / "adj_factor/all.parquet")
    monkeypatch.setattr(dp.instrument_sync, "sync_instruments", lambda *a: 0)
    monkeypatch.setattr(dp, "_resolve_universe", lambda *a: symbols)
    for name in ("_invalidate", "_refresh_single_view", "_refresh_views"):
        monkeypatch.setattr(dp, name, lambda *a: None)
    monkeypatch.setattr(dp, "warn_if_enriched_too_thin", lambda *a: 3)
    monkeypatch.setattr(dp, "_run_zen_post_close", lambda *a: {"status": "skipped"})
    monkeypatch.setattr(dp, "_get_app_state", lambda: None)
    monkeypatch.setattr(dp, "cn_today", lambda: RECENT)
    monkeypatch.setattr(preferences, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "alpaca")
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: False)
    monkeypatch.setattr(ks, "sync_and_persist_daily_batch", lambda *a, **kw: 0)
    from app.services import data_integrity
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *a, **kw: [])
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *a: True)
    monkeypatch.setattr(custom, "get_provider", lambda *a: alp.AlpacaProvider())
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: RECENT,
                           earliest_daily_date=lambda: DAY0, symbols_lagging=lambda *a, **kw: [])
    return repo, SimpleNamespace(has=lambda *a: False), tmp_path


def _close(root, symbol, d=DAY0):
    part = pl.read_parquet(root / "kline_daily_enriched" / f"date={d}" / "part.parquet")
    return part.filter(pl.col("symbol") == symbol)["close"][0]


def test_partial_source_failure_fails_the_run_and_keeps_migration_pending(monkeypatch, wired):
    repo, capset, root = wired
    failing = {"B"}
    _split_source(monkeypatch, failing)
    with pytest.raises(dp.PipelineStageError):
        dp.run_now(repo, capset)
    assert ks.adj_factor_basis(root / "adj_factor") != ks.ADJ_EVENT_BASIS
    failing.clear()
    dp.run_now(repo, capset)
    assert ks.adj_factor_basis(root / "adj_factor") == ks.ADJ_EVENT_BASIS
    assert _close(root, "A") == pytest.approx(10.0)
    assert _close(root, "B") == pytest.approx(20.0)


def test_crash_between_factor_commit_and_enriched_rebuild_is_retried(monkeypatch, wired):
    repo, capset, root = wired
    _split_source(monkeypatch, set())
    original = dp.run_pipeline

    def crash(*a, **kw):
        raise RuntimeError("injected crash before enriched publication")
    monkeypatch.setattr(dp, "run_pipeline", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        dp.run_now(repo, capset)
    monkeypatch.setattr(dp, "run_pipeline", original)
    dp.run_now(repo, capset)
    assert _close(root, "A") == pytest.approx(10.0)
    assert not (root / "adj_factor" / "rebuild_pending.json").exists()


def test_incremental_window_reaches_back_to_a_long_suspended_symbols_last_bar(monkeypatch):
    """A symbol halted since 08-31 resumes 09-30 after a 10:1 split. The daily incremental window
    (09-23..) has no prior bar for it, so the event can only be detected with a per-symbol anchor."""
    before, after = date(2026, 8, 31), date(2026, 9, 30)
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("offline", "offline"))
    requested = []

    def iter_bars(key, secret, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        requested.append((tuple(symbols), start.date()))
        for d, c, v in ((before, 100.0, 1000), (after, 10.0, 10000)):
            if start.date() <= d <= end.date():
                pre = adjustment == "all" and d == before
                yield "HALT", _bar(d, c / 10 if pre else c, v * 10 if pre else v)
    monkeypatch.setattr(alp, "_iter_bars", iter_bars)
    anchors = {"HALT": before}
    events = alp.AlpacaProvider().get_adj_factors(["HALT"], datetime(2026, 9, 23), datetime(2026, 10, 1),
                                                   anchor_dates=anchors)
    assert events.to_dicts() == [{"symbol": "HALT", "trade_date": after, "ex_factor": pytest.approx(10.0),
                                  "vol_factor": pytest.approx(10.0)}]
