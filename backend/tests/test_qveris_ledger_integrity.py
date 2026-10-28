"""Offline fault injection: an unreadable cost ledger is never free quota."""
import builtins
import json
import os
from pathlib import Path

import pytest

from app import secrets_store
from app.plugins.qveris import provider as qp
from app.services import qveris_gate as gate

DAY = "2026-10-02"
ROW = {"day": DAY, "kind": "options_chain", "charged": 99.0, "ok": True}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(gate, "_et_day", lambda ts=None: DAY)
    monkeypatch.setattr(secrets_store, "load", lambda: {"qveris_daily_limit": 100})
    monkeypatch.setattr(qp, "_api_key", lambda: "")
    monkeypatch.setattr(qp, "execute", lambda *a, **kw: pytest.fail("real provider forbidden"))


def seed(tmp_path, content):
    path = gate._ledger_path(tmp_path)
    path.write_bytes(content)
    return path


def fetch(tmp_path, calls):
    def execute(*args):
        calls.append(1)
        return {"data": {}, "charged": 1.0}
    return gate.fetch(tmp_path, "options_chain", "NVDA", {}, fresh=lambda e: False, execute=execute)


@pytest.mark.parametrize("bad", [
    b'{"day":', b'not json\n', b'null\n', b'[]\n', b'\xff\n',
    b'{"day":"2026-10-02","kind":"x"}\n',
    b'{"day":"2026-10-02","kind":"x","charged":null}\n',
    b'{"day":"2026-10-02","kind":"x","charged":NaN}\n',
    b'{"day":"2026-10-02","kind":"x","charged":-2}\n',
    b'{"day":"bad","kind":"x","charged":2}\n',
    b'{"day":"2026-10-01","kind":"x","charged":"oops"}\n',
])
def test_corrupt_rows_fail_closed_with_line_diagnostic(tmp_path, bad):
    original = json.dumps(ROW).encode() + b"\n" + bad
    path = seed(tmp_path, original)
    calls = []
    with pytest.raises(gate.GateError, match=r"ledger\.jsonl.*2"):
        fetch(tmp_path, calls)
    assert calls == []
    assert path.read_bytes() == original  # no skipping, truncating or automatic repair


def test_usage_reports_unknown_spend_not_zero_and_can_recover_after_reconciliation(tmp_path):
    path = seed(tmp_path, json.dumps(ROW).encode() + b'\n{"day":')
    usage = gate.usage(tmp_path)
    assert usage["spent_today"] is None
    assert "ledger.jsonl" in usage["error"]
    # Simulated operator repair from verified billing: retain the charged row.
    path.write_text(json.dumps(ROW) + "\n", encoding="utf-8")
    assert gate.spent_today(tmp_path) == 99
    calls = []
    fetch(tmp_path, calls)
    with pytest.raises(gate.QuotaExceeded):
        fetch(tmp_path, calls)
    assert calls == [1]


def test_read_and_append_reject_complete_json_without_line_terminator(tmp_path):
    original = json.dumps(ROW).encode()
    path = seed(tmp_path, original)
    with pytest.raises(gate.GateError):
        gate._append_ledger(tmp_path, ROW)
    assert path.read_bytes() == original


def test_paid_call_is_guarded_by_durable_intent_and_ledger_is_fsynced(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fsync)

    def execute(*args):
        pending = gate._ledger_path(tmp_path).with_suffix(".pending")
        assert pending.exists(), "paid call must have a recoverable intent before network"
        assert pending.stat().st_ino in synced
        return {"data": {}, "charged": 1.0}
    gate.fetch(tmp_path, "options_chain", "X", {}, fresh=lambda e: False, execute=execute)
    assert gate._ledger_path(tmp_path).stat().st_ino in synced
    assert not gate._ledger_path(tmp_path).with_suffix(".pending").exists()
    assert gate.spent_today(tmp_path) == 1


@pytest.mark.parametrize("failure", ["fsync", "partial_write"])
def test_failed_paid_booking_blocks_retries_and_preserves_bytes(tmp_path, monkeypatch, failure):
    path = seed(tmp_path, json.dumps(ROW).encode() + b"\n")
    calls = []
    real_fsync, real_open = os.fsync, builtins.open

    def fsync(fd):
        if calls and os.fstat(fd).st_ino == path.stat().st_ino:
            raise OSError("injected ledger fsync failure")
        real_fsync(fd)

    class PartialWriter:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def write(self, data):
            self.file.write(data[:10])
            self.file.flush()
            raise OSError("injected partial write")
        def __getattr__(self, name):
            return getattr(self.file, name)

    def open_file(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        return PartialWriter(opened) if Path(file) == path and "a" in mode else opened

    with monkeypatch.context() as m:
        if failure == "fsync":
            m.setattr(os, "fsync", fsync)
        else:
            m.setattr(builtins, "open", open_file)
        with pytest.raises(gate.GateError):
            fetch(tmp_path, calls)
    after = path.read_bytes()
    assert calls == [1]
    assert path.with_suffix(".pending").exists()
    # Clearing only process state emulates a restart: durable intent still blocks.
    monkeypatch.setattr(gate, "_ledger_faults", {}, raising=False)
    for _ in range(2):
        with pytest.raises(gate.GateError):
            fetch(tmp_path, calls)
        with pytest.raises(gate.GateError):
            gate._append_ledger(tmp_path, ROW)
    assert calls == [1]
    assert path.read_bytes() == after
    assert gate.usage(tmp_path)["spent_today"] is None


def test_intent_fsync_failure_does_not_call_provider(tmp_path, monkeypatch):
    def fail(fd):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(os, "fsync", fail)
    calls = []
    with pytest.raises(gate.GateError):
        fetch(tmp_path, calls)
    assert not calls
