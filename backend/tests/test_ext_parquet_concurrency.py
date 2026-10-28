"""Report #15: deterministic read/merge/write collisions on real Parquet.

The first writer pauses after reading; the second either reaches the same
read (old code) or contends on the real config lock (fixed code). No stress
loop or scheduler sleeps are used to manufacture the collision.
"""
from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import ext_data, ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig

DAY = date(2026, 10, 2)


def _config(mode, config_id="scores"):
    return ExtConfig(
        id=config_id, label="Scores", mode="timeseries" if mode == "intraday" else mode,
        fields=[ExtField("symbol"), ExtField("value", "float"), ExtField("ts")],
        pull=PullConfig(url="https://unused.invalid", time_field="ts" if mode == "intraday" else None),
    )


def _frame(symbol, value, ts):
    return pl.DataFrame({"symbol": [symbol], "value": [value], "ts": [ts]})


def _target(root, config):
    directory = root / "ext_data" / config.id
    if config.mode == "timeseries":
        directory = directory / "timeseries" / f"date={DAY}"
    return directory / "part.parquet"


def _write(root, config, frame):
    return ext_data.write_ext_parquet(frame, config, root, snapshot_date=DAY)


def _rows(path):
    return sorted(pl.read_parquet(path).select("symbol", "value", "ts").iter_rows())


def _observe_contention(monkeypatch, reached):
    # Wrap, never replace, the implementation's real lock. Old code has no
    # lock factory and instead signals reached through the instrumented read.
    factory = getattr(ext_data, "_config_write_lock", None)
    if factory is None:
        return

    class ObservedLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                reached.set()
                self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    monkeypatch.setattr(ext_data, "_config_write_lock", lambda *args: ObservedLock(factory(*args)))


@pytest.mark.parametrize("mode", ["snapshot", "timeseries", "intraday"])
@pytest.mark.parametrize("failure", [None, "read", "schema", "write", "first_write"])
def test_concurrent_updates_preserve_committed_rows_and_release_on_failure(
    tmp_path, monkeypatch, mode, failure,
):
    config = _config(mode)
    first_symbol = "AAPL" if mode == "intraday" else "MSFT"
    second_symbol = "AAPL" if mode == "intraday" else "NVDA"
    seed = _frame("AAPL", 1.0, "09:30")
    first_frame = _frame(first_symbol, 2.0, "09:31")
    second_frame = _frame(second_symbol, 3.0, "09:32")
    _write(tmp_path, config, seed)
    target = _target(tmp_path, config)
    if failure == "schema":
        second_frame = second_frame.with_columns(pl.lit("unexpected").alias("extra"))

    first_read, release_first = threading.Event(), threading.Event()
    first_finished, second_reached = threading.Event(), threading.Event()
    identities = {}
    original_read, original_write = pl.read_parquet, pl.DataFrame.write_parquet
    _observe_contention(monkeypatch, second_reached)

    def read(path, *args, **kwargs):
        if Path(path) != target:
            return original_read(path, *args, **kwargs)
        who = identities.get(threading.get_ident())
        if who == "second" and failure == "read":
            second_reached.set()
            assert first_finished.wait(5)
            raise OSError("injected existing-file read failure")
        frame = original_read(path, *args, **kwargs)
        if who == "first":
            first_read.set()
            assert release_first.wait(5)
        elif who == "second":
            second_reached.set()
            # Old code has already read stale bytes; make its replace happen
            # after the first writer. Fixed code reads only after lock release.
            assert first_finished.wait(5)
        return frame

    def write(frame, path, *args, **kwargs):
        who = identities.get(threading.get_ident())
        if (who, failure) in {("first", "first_write"), ("second", "write")}:
            Path(path).write_bytes(b"PAR1 interrupted")
            raise OSError("injected partial parquet write")
        return original_write(frame, path, *args, **kwargs)

    monkeypatch.setattr(pl, "read_parquet", read)
    monkeypatch.setattr(pl.DataFrame, "write_parquet", write)

    def writer(who, frame):
        identities[threading.get_ident()] = who
        try:
            # Different config objects, same id, must still share the lock.
            return _write(tmp_path, copy.deepcopy(config), frame)
        finally:
            if who == "first":
                first_finished.set()
            identities.pop(threading.get_ident(), None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer, "first", first_frame)
        second = None
        try:
            assert first_read.wait(5)
            second = pool.submit(writer, "second", second_frame)
            assert second_reached.wait(5), "second writer never reached read or lock contention"
        finally:
            release_first.set()
        if failure == "first_write":
            with pytest.raises(OSError, match="partial parquet write"):
                first.result(timeout=5)
        else:
            assert first.result(timeout=5) == 2
        if failure in {"read", "schema", "write"}:
            with pytest.raises((OSError, pl.exceptions.PolarsError)):
                second.result(timeout=5)
        else:
            assert second.result(timeout=5) == (2 if failure == "first_write" else 3)

    expected = seed.rows()
    if failure != "first_write":
        expected += first_frame.rows()
    if failure in {None, "first_write"}:
        expected += second_frame.rows()
    assert _rows(target) == sorted(expected)
    # Failure does not strand the config lock or contaminate a later write.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_write, tmp_path, config, seed).result(timeout=5) == len(expected)
    assert _rows(target) == sorted(expected)


@pytest.mark.parametrize("separation", ["config_id", "data_root"])
def test_unrelated_config_writes_do_not_wait_for_blocked_file_io(tmp_path, monkeypatch, separation):
    first_config = _config("snapshot")
    second_config = _config("snapshot", "other" if separation == "config_id" else "scores")
    second_root = tmp_path if separation == "config_id" else tmp_path / "other-root"
    blocked, release = threading.Event(), threading.Event()
    first_target = _target(tmp_path, first_config)
    original = ext_data.atomic_write_parquet

    def write(frame, path):
        if path == first_target:
            blocked.set()
            assert release.wait(5)
        return original(frame, path)

    monkeypatch.setattr(ext_data, "atomic_write_parquet", write)
    frame = _frame("AAPL", 1.0, "09:30")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write, tmp_path, first_config, frame)
        try:
            assert blocked.wait(5)
            assert pool.submit(_write, second_root, second_config, frame).result(timeout=5) == 1
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    assert _rows(first_target) == frame.rows()
    assert _rows(_target(second_root, second_config)) == frame.rows()


def test_pull_network_wait_does_not_hold_config_write_lock(tmp_path, monkeypatch):
    config = _config("snapshot")
    fetching, release = threading.Event(), threading.Event()

    async def fetch(*args):
        fetching.set()
        assert release.wait(5)
        return [{"symbol": "MSFT", "code": "MSFT", "value": 2.0, "ts": "09:31"}]

    monkeypatch.setattr(ext_pull, "fetch_rows_for_date", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pull = pool.submit(lambda: asyncio.run(ext_pull.fetch_and_ingest(config, tmp_path, DAY)))
        try:
            assert fetching.wait(5)
            ingest = pool.submit(
                ext_data.rows_to_parquet,
                [{"symbol": "AAPL", "code": "AAPL", "value": 1.0, "ts": "09:30"}],
                config, tmp_path, DAY,
            )
            assert ingest.result(timeout=5) == 1
        finally:
            release.set()
        assert pull.result(timeout=5) == (2, DAY.isoformat())
    assert _rows(_target(tmp_path, config)) == [("AAPL", 1.0, "09:30"), ("MSFT", 2.0, "09:31")]


@pytest.mark.parametrize("mode", ["snapshot", "timeseries"])
def test_incoming_values_are_cast_before_merging_not_used_to_replace_history(tmp_path, mode):
    config = _config(mode)
    _write(tmp_path, config, _frame("AAPL", 1.0, "09:30"))
    # JSON may infer an integer today even though the configured field is float.
    assert _write(tmp_path, config, _frame("MSFT", 2, "09:31")) == 2
    assert _rows(_target(tmp_path, config)) == [("AAPL", 1.0, "09:30"), ("MSFT", 2.0, "09:31")]
