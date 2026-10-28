"""Cancelled DNS/to_thread work must not hide a still-running executor thread."""
import asyncio
import threading
import time
from types import SimpleNamespace

from app.services import feishu_app as app


def test_executor_work_survives_cancellation_but_blocks_reopening(monkeypatch):
    monkeypatch.setattr(app, "_credentials", lambda: ("fake", "fake"))
    monkeypatch.setattr(app, "_saved_chat_id", lambda: "fake-chat")
    monkeypatch.setattr(app, "_TIMEOUT_S", 0.03)
    monkeypatch.setattr(app, "_CANCEL_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(app, "_cleanup_failed", False, raising=False)
    release = threading.Event()
    made, executors, closed = [], [], []

    class Channel:
        async def send(self, *args):
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, release.wait, 2)
            executors.append(loop._default_executor)
            await future
            return SimpleNamespace(success=True)
        def stop(self, **kwargs):
            closed.append(True)

    monkeypatch.setattr(app, "_channel", lambda *args: made.append(1) or Channel())
    try:
        start = time.monotonic()
        assert app.send_card("t", "", "b") is False
        # Complete bounded worker cleanup while its blocking executor is still live.
        for t in threading.enumerate():
            if t.name == "feishu-app-send":
                t.join(0.3)
                assert not t.is_alive()
        assert time.monotonic() - start < 0.7
        assert any(t.is_alive() for pool in executors for t in pool._threads)
        assert app.send_card("t", "", "b") is False
        assert made == [1], "a detached executor thread must keep the channel quarantined"
        assert closed == [True]
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == "feishu-app-send":
                t.join(3)
        for pool in executors:
            for t in pool._threads:
                t.join(3)
                assert not t.is_alive()
