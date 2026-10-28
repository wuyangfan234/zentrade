"""Malformed stream configuration must publish a terminal error on reconnect."""
import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.api import backtest as api
from app.backtest import worker
from app.config import settings


@pytest.mark.parametrize("field", ["params", "overrides", "regime_filter"])
def test_invalid_json_completes_first_and_reconnected_streams(monkeypatch, tmp_path, field):
    starts = []
    monkeypatch.setattr(worker, "run_worker_task", lambda *args: starts.append(args) or {})
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(api, "_running_jobs", {})
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(
        earliest_daily_date=lambda: date(2020, 1, 1), earliest_minute_date=lambda: None,
    )
    app.state.strategy_engine = None
    request = SimpleNamespace(app=app, is_disconnected=AsyncMock(return_value=True))
    kwargs = {
        "strategy_id": f"invalid_{field}", "symbols": "TEST",
        "start": "2026-01-05", "end": "2026-01-09", field: "{not-json",
    }

    async def read(response):
        chunks = []
        try:
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        finally:
            await response.body_iterator.aclose()
        return chunks

    async def exercise():
        first = await api.strategy_stream(request, **kwargs)
        first_error = None
        try:
            first_chunks = await read(first)
        except Exception as exc:  # old behavior: raw JSONDecodeError escapes
            first_error = exc
            first_chunks = []
        assert len(api._running_jobs) == 1
        job = next(iter(api._running_jobs.values()))

        second = await api.strategy_stream(request, **kwargs)
        second_chunks = await asyncio.wait_for(read(second), timeout=0.4)

        assert first_error is None
        assert any("event: error" in chunk for chunk in first_chunks)
        assert any("event: error" in chunk for chunk in second_chunks)
        assert job.done and job.error
        assert starts == []

    asyncio.run(exercise())
