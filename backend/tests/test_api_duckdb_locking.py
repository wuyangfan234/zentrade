"""Report #4: real DuckDB access must share the repository's lock.

The holder pauses between execute/fetch. A contender signals either lock
contention or unguarded DB access; events select the interleaving, not sleeps.
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import polars as pl
import pytest

from app.api import ext_data, kline, screener, watchlist
from app.storage.repository import KlineRepository


class ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.contender = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contender.set()
            self.lock.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *args):
        self.owner = None
        self.lock.release()

    def owned(self):
        return self.owner == threading.get_ident()


class CheckedHandle:
    """Delegate to real DuckDB, recording access without lock ownership."""

    def __init__(self, handle, lock, unsafe):
        self.handle, self.lock, self.unsafe = handle, lock, unsafe

    def __getattr__(self, name):
        target = getattr(self.handle, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            guarded = self.lock.owned()
            if not guarded:
                self.unsafe.append(name)
            try:
                result = target(*args, **kwargs)
            finally:
                if not guarded:
                    # For execute, signal after the real statement has run so
                    # the holder can observe overwritten connection results.
                    self.lock.contender.set()
            if name in {"cursor", "execute", "query"}:
                return CheckedHandle(result, self.lock, self.unsafe)
            return result

        return call


@pytest.fixture
def shared_repo(tmp_path, monkeypatch):
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE VIEW ext_legacy AS SELECT 'AAPL' AS symbol, 3.5 AS score")
    lock, unsafe = ObservedLock(), []
    checked = CheckedHandle(connection, lock, unsafe)
    repo = KlineRepository(SimpleNamespace(db=checked, data_dir=tmp_path))
    repo._lock = lock
    monkeypatch.setattr(repo, "clear_cache", Mock())
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set())
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (
        pl.DataFrame({"symbol": ["AAPL"], "close": [100.0]}), date(2026, 10, 2),
    ))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame())
    monkeypatch.setattr(repo, "get_name_map", lambda _: {"AAPL": "Apple"})
    monkeypatch.setattr(watchlist.watchlist, "list_symbols", lambda: [{"symbol": "AAPL"}])
    yield repo, connection, lock, unsafe
    connection.close()


def _request(repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _parquet(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["AAPL"], "score": [3.5]}).write_parquet(path)


def _rebuild(request, monkeypatch):
    from app.api import data
    from app.indicators import pipeline
    from app.services import pipeline_jobs

    store = Mock()
    store.create.return_value = ("isolated-rebuild", True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "release_run_slot", lambda _: None)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda _, fn: fn())
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **_: 1)
    monkeypatch.setattr(data, "invalidate_storage_cache", lambda: None)

    async def run():
        response = await kline.rebuild_enriched(request)
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        await asyncio.gather(*tasks)
        store.succeed.assert_called_once()
        store.fail.assert_not_called()
        return response

    return lambda: asyncio.run(run())


@pytest.mark.parametrize("entry", ["refresh_legacy", "refresh_config", "rebuild", "screener", "watchlist"])
def test_api_access_waits_for_shared_connection_and_materializes_under_lock(
    shared_repo, tmp_path, monkeypatch, entry,
):
    repo, connection, lock, unsafe = shared_repo
    request = _request(repo)
    if entry == "refresh_legacy":
        _parquet(tmp_path / "instruments_ext" / "part.parquet")
        def action():
            return ext_data._refresh_views(request)
        view = "instruments_ext"
    elif entry == "refresh_config":
        cfg_dir = tmp_path / "ext_data" / "fresh"
        _parquet(cfg_dir / "part.parquet")
        (cfg_dir / "config.json").write_text(json.dumps({"id": "fresh", "mode": "snapshot"}))
        def action():
            return ext_data._refresh_views(request)
        view = "ext_fresh"
    elif entry == "rebuild":
        _parquet(tmp_path / "kline_daily_enriched" / "date=2026-10-02" / "part.parquet")
        action = _rebuild(request, monkeypatch)
        view = "kline_enriched"
    elif entry == "screener":
        def action():
            return screener._load_ext_value_maps(repo, "legacy.score")
    else:
        def action():
            return watchlist.watchlist_enriched(request, ext_columns="legacy.score")

    original_from_arrow = pl.from_arrow

    def materialize(*args, **kwargs):
        if not lock.owned():
            unsafe.append("polars materialization")
        return original_from_arrow(*args, **kwargs)

    monkeypatch.setattr(pl, "from_arrow", materialize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            connection.execute("SELECT 41 AS pending_result")
            future = pool.submit(action)
            assert lock.contender.wait(5), "API did not reach either lock or connection"
            pending = connection.fetchall()
        result = future.result(timeout=5)

    assert pending == [(41,)], "concurrent API overwrote the connection's pending result"
    assert unsafe == [], f"shared DuckDB access bypassed repo lock: {unsafe}"
    if entry == "screener":
        assert result == {"legacy__score": {"AAPL": 3.5}}
    elif entry == "watchlist":
        assert result["rows"][0]["legacy__score"] == 3.5
    else:
        assert repo.execute_all(f"SELECT symbol, score FROM {view}") == [("AAPL", 3.5)]


def test_locked_dataframe_preserves_null_types_and_closes_after_error(shared_repo):
    repo, _, lock, unsafe = shared_repo
    df = repo.execute_frame("SELECT 'AAPL' AS symbol, NULL::DOUBLE AS score")
    assert df.schema == {"symbol": pl.String, "score": pl.Float64}
    assert df.to_dicts() == [{"symbol": "AAPL", "score": None}]
    empty = repo.execute_frame("SELECT 'AAPL' AS symbol, 3.5::DOUBLE AS score WHERE false")
    assert empty.schema == df.schema
    assert empty.is_empty()
    with pytest.raises(duckdb.Error):
        repo.execute_frame("SELECT * FROM missing_table")
    # A failed query cannot strand the lock or poison subsequent queries.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(repo.execute_one, "SELECT 99").result(timeout=5) == (99,)
    assert not lock.lock.locked()
    assert unsafe == []
