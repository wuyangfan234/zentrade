"""Production wiring is exercised, not just imported."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path
from app.jobs import daily_pipeline as dp
from app.market_clock import us_clock


def test_paper_jobs_registered_and_call_shared_loop(monkeypatch):
    monkeypatch.setattr(dp.AsyncIOScheduler,'start',lambda self:None)
    loop=SimpleNamespace(run_intraday=MagicMock())
    state=SimpleNamespace(trading_loop=loop,repo=SimpleNamespace(store=SimpleNamespace(data_dir=Path('/tmp/unused'))))
    monkeypatch.setattr(dp,'_app_state_ref',state)
    sched=dp.start_scheduler(state.repo,MagicMock())
    job=sched.get_job('zen_paper_tick')
    assert job is not None
    assert job.max_instances==1
    assert job.coalesce
    job.func()
    loop.run_intraday.assert_called_once()
    assert sched.get_job('depth_finalize') is None
    assert sched.get_job('zen_notification_outbox') is not None


def test_unknown_api_route_is_json404(monkeypatch):
    from app.main import app
    from fastapi.testclient import TestClient
    from app.services import auth
    monkeypatch.setattr(auth,'is_configured',lambda:False)
    client=TestClient(app,client=('127.0.0.1',1234))
    r=client.get('/api/definitely-not-a-route')
    assert r.status_code==404
    assert 'json' in r.headers['content-type']
