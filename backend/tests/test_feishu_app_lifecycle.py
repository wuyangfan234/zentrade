"""Offline lifecycle checks; no credentials or real Feishu transport used."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.services import feishu_app as app


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(app, "_credentials", lambda: ("test-id", "test-secret"))
    monkeypatch.setattr(app, "_saved_chat_id", lambda: "test-chat")
    monkeypatch.setattr(app, "_TIMEOUT_S", 0.03)
    monkeypatch.setattr(app, "_CANCEL_TIMEOUT_S", 0.1, raising=False)


def call_send(in_loop=False):
    if not in_loop:
        return app.send_card("test", "", "body")

    async def call():
        return app.send_card("test", "", "body")
    return asyncio.run(call())


def join_workers():
    for thread in threading.enumerate():
        if thread.name == "feishu-app-send":
            thread.join(3)
            assert not thread.is_alive()


@pytest.mark.parametrize("in_loop", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_send_releases_transport_and_stops_channel_on_every_exit(monkeypatch, in_loop, outcome):
    closed, stopped, loops = [], [], []

    class Channel:
        async def send(self, *args):
            loops.append(asyncio.get_running_loop())
            try:
                if outcome == "error":
                    raise RuntimeError("injected")
                if outcome == "timeout":
                    await asyncio.sleep(10)
                return SimpleNamespace(success=True)
            finally:
                # Real HTTPX transport owns its client via async with.
                await asyncio.sleep(0)
                closed.append(True)

        def stop(self, *, join_timeout):
            assert asyncio.get_running_loop() is loops[0]
            stopped.append(join_timeout)

    monkeypatch.setattr(app, "_channel", lambda *args: Channel())
    assert call_send(in_loop) is (outcome == "success")
    join_workers()
    assert closed == [True]
    assert len(stopped) == 1
    assert loops[0].is_closed()


@pytest.mark.parametrize("in_loop", [False, True])
@pytest.mark.parametrize("blocking_stage", ["send", "stop"])
def test_uncancellable_worker_is_bounded_and_no_new_channel_is_opened(monkeypatch, caplog, in_loop, blocking_stage):
    release = threading.Event()
    entered = threading.Event()
    channels, stopped = [], []

    class Channel:
        async def send(self, *args):
            if blocking_stage == "send":
                entered.set()
                release.wait(2)  # simulates C/synchronous code that asyncio cannot cancel
            return SimpleNamespace(success=True)

        def stop(self, *, join_timeout):
            stopped.append(True)
            if blocking_stage == "stop":
                entered.set()
                release.wait(2)

    def factory(*args):
        channels.append(Channel())
        return channels[-1]
    monkeypatch.setattr(app, "_channel", factory)
    try:
        start = time.monotonic()
        assert call_send(in_loop) is False
        assert time.monotonic() - start < 0.8
        assert entered.is_set()
        # join(timeout) does not kill a Python thread. Keep ownership until exit.
        for _ in range(3):
            assert call_send(in_loop) is False
        assert len(channels) == 1
        assert len([t for t in threading.enumerate() if t.name == "feishu-app-send"]) == 1
        assert "test-secret" not in caplog.text
    finally:
        release.set()
        join_workers()
    assert stopped == [True]
    assert call_send(in_loop) is True
    join_workers()


def test_cancellation_resistant_coroutine_does_not_spawn_replacement_workers(monkeypatch):
    release, cancelled = threading.Event(), threading.Event()
    count, closed = [], []

    class Channel:
        async def send(self, *args):
            count.append(1)
            try:
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        cancelled.set()
                return SimpleNamespace(success=True)
            finally:
                closed.append(True)

        def stop(self, *, join_timeout):
            pass

    monkeypatch.setattr(app, "_channel", lambda *args: Channel())
    # Run the caller independently so the regression test also safely releases old code.
    results = []
    caller = threading.Thread(target=lambda: results.append(call_send(True)), daemon=True)
    try:
        caller.start()
        caller.join(0.6)
        assert not caller.is_alive(), "cancellation wait must be bounded"
        assert results == [False] and cancelled.is_set()
        assert call_send(True) is False
        assert count == [1]
    finally:
        release.set()
        caller.join(3)
        join_workers()
    assert closed == [True]
