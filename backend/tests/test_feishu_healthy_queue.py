"""Healthy concurrent monitoring notifications queue; all transports are offline."""
import asyncio
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.services import feishu_app as app


@pytest.fixture(autouse=True)
def offline_bot(monkeypatch):
    monkeypatch.setattr(app, "_credentials", lambda: ("fake", "fake"))
    monkeypatch.setattr(app, "_saved_chat_id", lambda: "fake-chat")
    monkeypatch.setattr(app, "_TIMEOUT_S", 1.0)
    monkeypatch.setattr(app, "_CANCEL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(app, "_cleanup_failed", False)


def test_quote_service_two_healthy_notifications_are_both_sent(monkeypatch):
    from app import secrets_store
    from app.services import email_adapter, preferences, quote_service

    for name in ("get_feishu_webhook_url", "get_feishu_webhook_secret",
                 "get_wecom_webhook_url", "get_custom_webhook_url"):
        monkeypatch.setattr(preferences, name, lambda: "")
    monkeypatch.setattr(preferences, "get_email_smtp_config", lambda: {})
    monkeypatch.setattr(secrets_store, "get_custom_webhook_secret", lambda: "")
    monkeypatch.setattr(secrets_store, "get_email_smtp_password", lambda: "")
    monkeypatch.setattr(email_adapter, "is_configured", lambda config: False)
    first_entered, second_submitted = threading.Event(), threading.Event()
    sent, stopped, futures, workers = [], [], [], []

    class Channel:
        async def send(self, target, payload, options):
            body = payload["card"]["body"]["elements"][0]["content"]
            workers.append(threading.current_thread())
            if body == "first":
                first_entered.set()
                assert second_submitted.wait(0.8)
                # Keep the healthy worker active as the second pool thread enters.
                await asyncio.sleep(0.05)
            assert all(not worker.is_alive() for worker in workers[:-1])
            sent.append(body)
            return SimpleNamespace(success=True)

        def stop(self, **kwargs):
            stopped.append(True)

    monkeypatch.setattr(app, "_channel", lambda *args: Channel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        class Executor:
            def submit(self, fn, *args):
                future = pool.submit(fn, *args)
                futures.append(future)
                if len(futures) == 1:
                    assert first_entered.wait(0.8)
                else:
                    second_submitted.set()
                return future

        monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", Executor())
        service = object.__new__(quote_service.QuoteService)
        engine = SimpleNamespace(rules={"rule": {"webhook_channels": ["feishu"]}})
        service._maybe_send_webhook([
            {"rule_id": "rule", "message": "first"},
            {"rule_id": "rule", "message": "second"},
        ], engine)
        results = [future.result(timeout=2) for future in futures]
    assert results == [True, True], results
    assert sent == ["first", "second"]
    assert stopped == [True, True]
    assert all(not worker.is_alive() for worker in workers)


def test_queued_timeout_closes_unstarted_coroutine_without_new_worker(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    first_result = []

    async def first():
        entered.set()
        assert release.wait(2)
        return "first"

    async def second():
        pytest.fail("expired queued work must not run")

    caller = threading.Thread(target=lambda: first_result.append(app._run(first())), daemon=True)
    coro = second()
    try:
        caller.start()
        assert entered.wait(0.8)
        monkeypatch.setattr(app, "_TIMEOUT_S", 0.06)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            app._run(coro)
        elapsed = time.monotonic() - start
        assert 0.04 <= elapsed < 0.4
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        assert len([t for t in threading.enumerate() if t.name == "feishu-app-send"]) == 1
    finally:
        release.set()
        caller.join(2)
        coro.close()
    assert not caller.is_alive()
    assert first_result == ["first"]

    async def recovered():
        return "recovered"

    assert app._run(recovered()) == "recovered"
