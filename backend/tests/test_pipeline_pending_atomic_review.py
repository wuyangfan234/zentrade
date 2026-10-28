"""Review #6/#10: bounded pending admission and crash-safe job snapshots.

All workers/paths are isolated; every blocked worker is joined even on RED.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import fs_utils, heavy_job_limiter, pipeline_jobs, preferences
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.pipeline_jobs import JobCancelledError, JobStore, run_with_capacity


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition did not become true")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = JobStore(store_dir=tmp_path / "jobs")
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.002)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", limiter)
    return store, limiter


def test_pending_wait_times_out_without_polling_and_never_executes_later(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=0.05)
    called = threading.Event()
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_with_capacity, jid, called.set)
        try:
            wait_until(future.done)
            with pytest.raises(JobCancelledError):
                future.result()
            assert store.get(jid)["status"] == "failed"
            assert "等待" in store.get(jid)["error"]
            assert "超时" in store.get(jid)["error"]
            assert pipeline_jobs.is_cancelled(jid)
            assert store.active_id() is None
            assert limiter.in_use == 1
            assert not limiter._waiters
        finally:
            limiter.release("normal")
    assert not called.is_set()
    assert limiter.in_use == 0


def test_pending_reaper_uses_creation_time_not_progress(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    store._active_jobs[jid]["created_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    store.progress(jid, "init", 0, "still waiting")
    store.reap_stale()
    assert store.get(jid)["status"] == "failed"
    assert "等待" in store.get(jid)["error"]
    with pytest.raises(JobCancelledError):
        run_with_capacity(jid, lambda: pytest.fail("reaped job resurrected"))
    assert limiter.in_use == 0


def test_executor_delay_counts_towards_pending_budget(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    store._active_jobs[jid]["created_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    with pytest.raises(JobCancelledError):
        run_with_capacity(jid, lambda: pytest.fail("expired job executed"))
    assert store.get(jid)["status"] == "failed"
    assert limiter.in_use == 0


@pytest.mark.parametrize("at_boundary", ["before_start", "after_start"])
def test_cancel_at_admission_boundary_never_calls_body(isolated, monkeypatch, at_boundary):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    original = store.start
    called = []

    def cancel_at_start(job_id):
        if at_boundary == "before_start":
            store.terminate(job_id, "cancel at admission")
        result = original(job_id)
        if at_boundary == "after_start":
            store.terminate(job_id, "cancel at admission")
        return result

    monkeypatch.setattr(store, "start", cancel_at_start)
    with pytest.raises(JobCancelledError):
        run_with_capacity(jid, lambda: called.append(True))
    assert called == []
    assert store.get(jid)["status"] == "failed"
    assert limiter.in_use == 0


def test_terminal_job_cannot_execute_when_cancel_flag_is_evicted(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    store.terminate(jid, "cancelled")
    pipeline_jobs._CANCEL_FLAGS.pop(jid)
    called = []
    with pytest.raises(JobCancelledError):
        run_with_capacity(jid, lambda: called.append(True))
    assert called == []
    assert store.get(jid)["status"] == "failed"
    assert limiter.in_use == 0


def test_reopened_job_times_out_without_releasing_cancelled_running_worker(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    assert pipeline_jobs.try_acquire_run_slot(jid)
    entered, finish, second_called = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        assert finish.wait(3)
        return "worker actually ended"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_with_capacity, jid, work)
        try:
            assert entered.wait(1)
            store.terminate(jid, "cancelled while writing")
            next_id, is_new = store.create(timeout_s=0.05)
            assert is_new
            # Legacy admission token is not the actual worker's shared capacity.
            assert pipeline_jobs.try_acquire_run_slot(next_id)
            second = pool.submit(run_with_capacity, next_id, second_called.set)
            wait_until(second.done)
            with pytest.raises(JobCancelledError):
                second.result()
            assert limiter.in_use == 2
            assert not limiter.acquire("normal", timeout=0)
            assert not second_called.is_set()
        finally:
            finish.set()
        assert first.result(timeout=1) == "worker actually ended"
    assert limiter.in_use == 0
    assert not second_called.is_set()
    pipeline_jobs.release_run_slot(jid)
    pipeline_jobs.release_run_slot(next_id)


def test_cancelled_queue_entry_keeps_later_legacy_owner(isolated):
    store, limiter = isolated
    jid, _ = store.create(timeout_s=60)
    assert pipeline_jobs.try_acquire_run_slot(jid)
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_with_capacity, jid, lambda: pytest.fail("cancelled body"))
        try:
            wait_until(lambda: bool(limiter._waiters))
            store.terminate(jid, "cancel in queue")
            next_id, _ = store.create(timeout_s=60)
            assert pipeline_jobs.try_acquire_run_slot(next_id)
            with pytest.raises(JobCancelledError):
                future.result(timeout=1)
            pipeline_jobs.release_run_slot(jid)
            assert not pipeline_jobs.try_acquire_run_slot("third")
            assert limiter.in_use == 1
        finally:
            limiter.release("normal")
    pipeline_jobs.release_run_slot(next_id)


def test_expired_fifo_head_unblocks_next_waiter_without_stealing_capacity():
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.002)
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        head = pool.submit(limiter.acquire, "exclusive", timeout=0.05)
        try:
            wait_until(lambda: bool(limiter._waiters))
            assert not limiter.acquire("normal", timeout=0)
            next_waiter = pool.submit(limiter.acquire, "normal", timeout=1)
            assert head.result(timeout=1) is False
            assert next_waiter.result(timeout=1) is True
            assert limiter.in_use == 2
            limiter.release("normal")
        finally:
            limiter.release("normal")
    assert not limiter._waiters
    assert limiter.in_use == 0


def test_late_capacity_release_cannot_admit_expired_waiter(monkeypatch):
    limiter = HeavyJobLimiter(capacity=2)
    assert limiter.acquire("normal", timeout=0)
    clock = [0.0]
    monkeypatch.setattr(heavy_job_limiter, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def wake_after_deadline(timeout):
        clock[0] = 2.0
        limiter.release("normal")

    monkeypatch.setattr(limiter._condition, "wait", wake_after_deadline)
    admitted = limiter.acquire("exclusive", timeout=1)
    if admitted:
        limiter.release("exclusive")
    assert admitted is False
    assert limiter.in_use == 0
    assert not limiter._waiters


@pytest.mark.parametrize("transition", ["start", "succeed", "fail", "orphan"])
@pytest.mark.parametrize("max_jobs", [1, 50])
def test_failed_replace_preserves_previous_readable_snapshot(tmp_path, monkeypatch, transition, max_jobs):
    store = JobStore(max_jobs=max_jobs, store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=60)
    if transition != "start":
        store.start(jid)
    target = store._store_dir / f"{jid}.json"
    previous = target.read_bytes()
    attempts = []

    def crash_before_replace(src, dst):
        attempts.append((src, dst))
        assert target.read_bytes() == previous
        assert json.loads(src.read_text("utf-8"))["id"] == jid
        raise OSError("simulated failure before replace")

    monkeypatch.setattr(fs_utils.os, "replace", crash_before_replace)
    if transition == "start":
        store.start(jid)
    elif transition == "succeed":
        store.succeed(jid, {"rows": 3})
    elif transition == "fail":
        store.fail(jid, "failed")
    else:
        JobStore(max_jobs=max_jobs, store_dir=store._store_dir)
    assert target.read_bytes() == previous
    assert attempts, "job persistence bypassed the atomic replace helper"
    assert json.loads(target.read_text("utf-8"))["id"] == jid


def test_serialization_failure_preserves_snapshot_at_retention_limit(tmp_path):
    store = JobStore(max_jobs=1, store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    target = store._store_dir / f"{jid}.json"
    previous = target.read_bytes()
    store.succeed(jid, object())  # Existing best-effort logging contract.
    assert target.read_bytes() == previous
    assert store.get(jid)["status"] == "running"


def test_job_schema_remains_compatible_and_restart_sees_terminal_result(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, is_new = store.create(timeout_s=60)
    assert is_new
    created_at = store.get(jid).get("created_at")
    assert created_at is not None
    assert datetime.fromisoformat(created_at).tzinfo is not None
    store.start(jid)
    store.succeed(jid, {"rows": 7, "label": "完成"})
    revived = JobStore(store_dir=store._store_dir)
    result = revived.get(jid)
    assert result["status"] == "succeeded"
    assert result["result"] == {"rows": 7, "label": "完成"}
    assert len(list(store._store_dir.glob("*.json"))) == 1
