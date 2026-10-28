"""Rewrites must leave the old complete file on write/replace failure."""
import json
import os
import time
from pathlib import Path

import pytest

from app.services import alert_store


@pytest.mark.parametrize("operation", ["delete", "prune", "clear"])
@pytest.mark.parametrize("stage", ["temp_write", "replace"])
def test_failed_rewrite_preserves_original_bytes(tmp_path, monkeypatch, operation, stage):
    now = int(time.time() * 1000)
    rows = [{"ts": 1, "rule_id": "old"}, {"ts": now, "rule_id": "keep"}]
    path = alert_store._path(tmp_path)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = path.read_bytes()
    attempted = []
    real_write_text = Path.write_text

    def failed_replace(src, dst):
        attempted.append("replace")
        assert Path(dst) == path
        assert path.read_bytes() == before
        raise OSError("injected replace failure")

    def partial_temp_write(self, text, *args, **kwargs):
        if self == path.with_suffix(".jsonl.tmp"):
            attempted.append("temp_write")
            real_write_text(self, text[:5], *args, **kwargs)
            raise OSError("injected partial temp write")
        return real_write_text(self, text, *args, **kwargs)

    if stage == "replace":
        monkeypatch.setattr(os, "replace", failed_replace)
    else:
        monkeypatch.setattr(Path, "write_text", partial_temp_write)
    if operation == "delete":
        assert alert_store.delete_one(tmp_path, 1) is False
    elif operation == "prune":
        with alert_store._lock:
            alert_store._prune_locked(path)
    else:
        with pytest.raises(OSError):
            alert_store.clear(tmp_path)
    assert attempted == [stage]
    assert path.read_bytes() == before


def test_delete_prune_clear_preserve_semantics(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    rows = [{"ts": now, "rule_id": "first"}, {"ts": now, "rule_id": "second"},
            {"ts": 1, "rule_id": "old"}, {"ts": now + 1, "rule_id": "latest"}]
    monkeypatch.setattr(alert_store, "_write_count", 0)
    alert_store.append_many(tmp_path, rows)
    assert alert_store.delete_one(tmp_path, now)
    assert not alert_store.delete_one(tmp_path, 123)
    monkeypatch.setattr(alert_store, "MAX_RECORDS", 1)
    with alert_store._lock:
        alert_store._prune_locked(alert_store._path(tmp_path))
    assert alert_store.list_recent(tmp_path) == [rows[-1]]
    assert alert_store.clear(tmp_path) == 1
    assert alert_store.count(tmp_path) == 0


def test_delete_one_same_ts_uses_symbol_and_rule(tmp_path):
    ts = int(time.time() * 1000)
    rows = [
        {"ts": ts, "symbol": "DELL", "rule_id": "a"},
        {"ts": ts, "symbol": "IREN", "rule_id": "b"},
    ]
    alert_store.append_many(tmp_path, rows)
    assert alert_store.delete_one(tmp_path, ts, symbol="IREN", rule_id="b") is True
    left = alert_store.list_recent(tmp_path)
    assert {r["symbol"] for r in left} == {"DELL"}
