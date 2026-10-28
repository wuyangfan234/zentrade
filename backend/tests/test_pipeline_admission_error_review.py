"""Keep worker exceptions separate from the new admission timeout policy."""
from __future__ import annotations

import pytest

from app.services import heavy_job_limiter, pipeline_jobs
from app.services.heavy_job_limiter import HeavyJobLimiter, HeavyJobLimitTimeoutError
from app.services.pipeline_jobs import JobStore, run_with_capacity


def test_worker_timeout_is_not_reported_as_queue_timeout(monkeypatch, tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    limiter = HeavyJobLimiter(capacity=2)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", limiter)
    jid, _ = store.create(timeout_s=60)
    error = HeavyJobLimitTimeoutError("worker-specific capacity timeout")

    def work():
        assert store.get(jid)["status"] == "running"
        raise error

    with pytest.raises(HeavyJobLimitTimeoutError) as exc:
        run_with_capacity(jid, work)
    assert exc.value is error
    # The existing outer API/scheduler owns failure reporting once admitted.
    assert store.get(jid)["status"] == "running"
    assert not pipeline_jobs.is_cancelled(jid)
    assert limiter.in_use == 0
