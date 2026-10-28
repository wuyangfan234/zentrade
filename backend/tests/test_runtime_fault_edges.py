"""Additional failure edges discovered while reviewing the first fix."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api import qveris as qapi
from app.plugins.qveris import provider as qp
from app.services import feishu_app, qveris_gate


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(feishu_app, "_credentials", lambda: ("fake", "fake"))
    monkeypatch.setattr(feishu_app, "_saved_chat_id", lambda: "fake-chat")
    monkeypatch.setattr(feishu_app, "_TIMEOUT_S", 0.05)
    monkeypatch.setattr(feishu_app, "_CANCEL_TIMEOUT_S", 0.1, raising=False)
    monkeypatch.setattr(feishu_app, "_cleanup_failed", False, raising=False)


def test_stop_failure_quarantines_instead_of_reopening(bot, monkeypatch):
    made = []

    class Channel:
        async def send(self, *args):
            return SimpleNamespace(success=True)
        def stop(self, **kwargs):
            raise OSError("injected resource close failure")

    monkeypatch.setattr(feishu_app, "_channel", lambda *args: made.append(1) or Channel())
    assert feishu_app.send_card("t", "", "b") is False
    assert feishu_app.send_card("t", "", "b") is False
    assert made == [1], "failed resource close must not admit more channels"


def test_thread_start_failure_closes_loop_in_async_caller(bot, monkeypatch):
    loops = []
    create_loop = asyncio.new_event_loop

    def make_loop():
        loop = create_loop()
        loops.append(loop)
        return loop

    def fail_start(self):
        raise RuntimeError("injected thread start failure")

    async def call():
        assert feishu_app.send_card("t", "", "b") is False

    monkeypatch.setattr(asyncio, "new_event_loop", make_loop)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        asyncio.run(call())
        assert all(loop.is_closed() for loop in loops)
    finally:
        for loop in loops:
            if not loop.is_closed():
                for task in asyncio.all_tasks(loop):
                    task.get_coro().close()
                loop.close()


def test_corrupt_quota_api_is_diagnostic_and_never_calls_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(qp, "_api_key", lambda: "fake")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    monkeypatch.setattr(qp, "execute", lambda *a: pytest.fail("paid provider must not be called"))
    monkeypatch.setattr(qp, "credits", lambda *a: pytest.fail("no external balance call needed"))
    qveris_gate._ledger_path(tmp_path).write_bytes(b'{"day":')
    app = FastAPI()
    app.include_router(qapi.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)
    usage = client.get("/api/qveris/usage")
    assert usage.status_code == 200
    assert usage.json()["spent_today"] is None
    assert "ledger.jsonl" in usage.json()["error"]
    response = client.get("/api/qveris/estimates", params={"symbol": "X", "refresh": 1})
    assert response.status_code == 409
    assert "ledger.jsonl" in response.json()["detail"]


def test_billing_intent_serializes_distinct_symbols_within_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "load", lambda: {"qveris_daily_limit": 1})
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def execute(*args):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"data": {}, "charged": 1}

    def fetch(symbol):
        try:
            qveris_gate.fetch(tmp_path, "options_chain", symbol, {}, fresh=lambda e: False, execute=execute)
        except (qveris_gate.QuotaExceeded, qveris_gate.GateError) as exc:
            errors.append(type(exc).__name__)

    first, second = threading.Thread(target=fetch, args=("A",)), threading.Thread(target=fetch, args=("B",))
    try:
        first.start()
        assert entered.wait(2)
        second.start()
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert calls == [1]
    assert errors == ["QuotaExceeded"]
    assert qveris_gate.spent_today(tmp_path) == 1
    assert len([json.loads(line) for line in qveris_gate._ledger_path(tmp_path).read_text().splitlines()]) == 1
