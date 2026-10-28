"""SSE jobs must start on reconnect even if the first response never streamed."""
import asyncio
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI

from app.api import backtest as api
from app.backtest import worker
from app.config import settings

ROUTES = {
    "backtest": "/api/backtest/strategy/stream",
    "optimize": "/api/backtest/optimize/stream",
    "walkforward": "/api/backtest/walkforward/stream",
}


def _params(kind, *, guarded=False):
    result = {
        "strategy_id": f"reconnect_{kind}",
        "symbols": "TEST",
        "start": "2020-01-01" if guarded else "2026-01-05",
        "end": "2026-09-04" if guarded else "2026-01-09",
    }
    if kind in {"optimize", "walkforward"}:
        result["param_grid"] = '{"n": [5]}'
    if kind == "walkforward" and guarded:
        result.update(train_days="400", test_days="400")
    return result


async def _invoke(app, kind, params, *, abort=False, timeout=0.5):
    path = ROUTES[kind]
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(),
        "query_string": urlencode(params).encode(), "root_path": "",
        "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 50000), "server": ("test", 80),
    }
    messages = []
    first_receive = True
    never = asyncio.Event()

    async def receive():
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message):
        saved = dict(message)
        body = saved.get("body", b"")
        if isinstance(body, bytes):
            saved["body"] = body.decode()
        messages.append(saved)
        if abort and message["type"] == "http.response.start":
            raise OSError("controlled disconnect before body iteration")

    outcome = "returned"
    exception = None
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    except TimeoutError:
        outcome = "timeout"
    except BaseException as exc:
        outcome = "exception"
        exception = f"{type(exc).__name__}: {exc}"
    return {
        "outcome": outcome, "exception": exception, "messages": messages,
        "bodies": [m.get("body", "") for m in messages if m["type"] == "http.response.body"],
    }


@pytest.fixture
def stream_app(monkeypatch, tmp_path):
    starts = []

    def fake_run(task, progress=None, cancel_event=None):
        starts.append(task["kind"])
        return {"ok": True, "kind": task["kind"]}

    monkeypatch.setattr(worker, "run_worker_task", fake_run)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(api, "_running_jobs", {})
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(
        earliest_daily_date=lambda: date(2020, 1, 1),
        earliest_minute_date=lambda: None,
    )
    app.state.strategy_engine = None
    return app, starts


def _only_job():
    assert len(api._running_jobs) == 1
    return next(iter(api._running_jobs.values()))


@pytest.mark.parametrize("kind", list(ROUTES))
def test_first_disconnect_does_not_poison_same_key_reconnect(stream_app, kind):
    app, starts = stream_app

    async def exercise():
        params = _params(kind)
        first = await _invoke(app, kind, params, abort=True)
        assert "ClientDisconnect" in first["exception"]
        job = _only_job()
        assert not job.done and starts == []

        second = await _invoke(app, kind, params, timeout=2.0)
        assert len(starts) == 1
        assert job.done
        assert any("event: done" in body for body in second["bodies"])

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", list(ROUTES))
def test_guard_rejection_finishes_placeholder_without_starting_worker(stream_app, kind, monkeypatch):
    app, starts = stream_app
    monkeypatch.setattr(settings, "backtest_range_guard", True)

    async def exercise():
        response = await _invoke(app, kind, _params(kind, guarded=True))
        job = _only_job()
        assert any("event: error" in body for body in response["bodies"])
        assert job.done
        assert job.error
        assert starts == []

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", list(ROUTES))
def test_normal_stream_still_starts_once_and_completes(stream_app, kind):
    app, starts = stream_app

    async def exercise():
        response = await _invoke(app, kind, _params(kind), timeout=2.0)
        job = _only_job()
        assert len(starts) == 1
        assert job.done
        assert any("event: done" in body for body in response["bodies"])

    asyncio.run(exercise())
