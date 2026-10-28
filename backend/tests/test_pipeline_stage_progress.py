from app.services.pipeline_jobs import JobStore


def _started_store(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, is_new = store.create(timeout_s=60)
    assert is_new
    store.start(job_id)
    return store, job_id


def test_new_stage_without_explicit_percentage_resets_previous_stage(tmp_path):
    store, job_id = _started_store(tmp_path)
    store.progress(job_id, "sync_daily", 45, "daily chunk", stage_pct=87)
    store.progress(job_id, "compute_enriched", 50, "new stage")

    actual = store.get(job_id)
    assert actual["stage"] == "compute_enriched"
    assert actual["progress"] == 50
    assert actual["stage_pct"] == 0


def test_same_stage_without_explicit_percentage_preserves_progress(tmp_path):
    store, job_id = _started_store(tmp_path)
    store.progress(job_id, "sync_daily", 45, "daily chunk", stage_pct=87)
    store.progress(job_id, "sync_daily", 46, "same stage")
    assert store.get(job_id)["stage_pct"] == 87


def test_new_stage_explicit_percentage_wins(tmp_path):
    store, job_id = _started_store(tmp_path)
    store.progress(job_id, "sync_daily", 45, "daily chunk", stage_pct=87)
    store.progress(job_id, "compute_enriched", 50, "new stage", stage_pct=12)
    assert store.get(job_id)["stage_pct"] == 12
