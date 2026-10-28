"""US cost defaults must survive all strategy API boundaries and SSE cancellation."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest
from app.backtest.strategy import StrategyBacktestService


def test_post_run_passes_explicit_per_share_costs_to_worker(monkeypatch):
    from app.backtest import worker
    captured = []
    monkeypatch.setattr(backtest, "_guard_server_backtest_range", lambda *a: None)
    monkeypatch.setattr(worker, "make_worker_task", lambda kind, root, cfg: cfg)
    monkeypatch.setattr(worker, "run_worker_task", lambda cfg: captured.append(cfg) or StrategyBacktestService._config_to_dict(cfg))
    app = FastAPI()
    app.include_router(backtest.router)
    with TestClient(app) as client:
        response = client.post("/api/backtest/strategy/run", json={
            "strategy_id": "test", "start": "2026-01-05", "end": "2026-01-09",
            "cost_model": "us_per_share_v1", "commission_per_share": 0.02, "commission_min": 2.0,
        })
    assert response.status_code == 200
    assert captured[0].commission_per_share == 0.02
    assert response.json()["cost_model"] == "us_per_share_v1"
    assert response.json()["commission_min"] == 2.0


def test_default_request_does_not_inject_legacy_fees():
    req = backtest.StrategyBacktestRequest(strategy_id="test")
    assert req.fees_pct is None
    assert req.cost_model == "us_per_share_v1"
    assert req.commission_per_share == 0.005
    assert req.commission_min == 1.0


def test_cache_keys_distinguish_share_cost_models():
    args = ("s", None, None, None, "open_t+1", None, None, None, 5.0, 10, 1.0, 1e6, "equal", None, None)
    default = backtest._make_job_key(*args)
    assert default != backtest._make_job_key(*args, commission_min=2.0)
    assert default != backtest._make_job_key(*args, commission_per_share=0.01)
    assert default != backtest._make_job_key(*args, cost_model="legacy_pct")


def test_cancel_reconstructs_all_cost_and_fill_fields():
    args = ("s", None, "2026-01-05", "2026-01-09", "open_t+1", None, None, None, 5.0, 10, 1.0, 1e6, "equal", None, None)
    key = backtest._make_job_key(*args, cost_model="us_per_share_v1", commission_min=2.0, minute_fill=True)
    job = backtest._BacktestJob(key)
    backtest._running_jobs[key] = job
    async def body():
        return {"qs": "strategy_id=s&start=2026-01-05&end=2026-01-09&cost_model=us_per_share_v1&commission_min=2&minute_fill=true"}
    try:
        result = asyncio.run(backtest.strategy_cancel(SimpleNamespace(json=body)))
        assert result == {"ok": True}
        assert job.cancel_event.is_set()
    finally:
        backtest._running_jobs.pop(key, None)


@pytest.mark.parametrize("route", ["strategy", "optimize", "walkforward"])
@pytest.mark.parametrize("costs", [
    {},
    {"cost_model": "us_per_share_v1", "commission_per_share": 0.02, "commission_min": 2.0},
    {"cost_model": "legacy_pct", "fees_pct": 0.0007, "commission_pct": 0.0003, "stamp_tax_pct": 0.001},
])
def test_stream_worker_roundtrip_preserves_cost_contract(monkeypatch, tmp_path, route, costs):
    from app.backtest import worker
    from app.backtest.engine import MatcherConfig
    captured = []

    def run(task, *args):
        captured.append(task)
        if route == "strategy":
            cfg = worker._decode_backtest_config(task["config"])
        else:
            from app.backtest.strategy import StrategyBacktestConfig
            kwargs = task["config"]["backtest_kwargs"]
            cfg = StrategyBacktestConfig(strategy_id="test", symbols=None, start=date(2026, 1, 5), end=date(2026, 1, 9), **kwargs)
        return StrategyBacktestService._config_to_dict(cfg)

    monkeypatch.setattr(backtest, "_running_jobs", {})
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    monkeypatch.setattr(backtest.settings, "backtest_range_guard", False)
    monkeypatch.setattr(worker, "run_worker_task", run)
    app = FastAPI()
    app.include_router(backtest.router)
    params = {"strategy_id": "test", "start": "2026-01-05", "end": "2026-01-09", **costs}
    if route != "strategy":
        params["param_grid"] = json.dumps({"window": [5, 10]})
    with TestClient(app) as client:
        response = client.get(f"/api/backtest/{route}/stream", params=params)
    assert response.status_code == 200
    assert "event: done" in response.text, response.text
    done = next(part for part in response.text.split("\n\n") if part.startswith("event: done"))
    result = json.loads(done.split("data: ", 1)[1])
    expected_model = costs.get("cost_model", "us_per_share_v1")
    assert result["cost_model"] == expected_model
    assert result["fees_pct"] == costs.get("fees_pct")
    assert result["commission_pct"] == costs.get("commission_pct")
    assert result["stamp_tax_pct"] == costs.get("stamp_tax_pct")
    payload = captured[0]["config"]
    if route != "strategy":
        payload = payload["backtest_kwargs"]
    assert payload["cost_model"] == expected_model
    assert payload["commission_per_share"] == costs.get("commission_per_share", 0.005)
    assert payload["commission_min"] == costs.get("commission_min", 1.0)
    model = MatcherConfig(**{k: payload[k] for k in (
        "cost_model", "fees_pct", "commission_pct", "stamp_tax_pct", "commission_per_share", "commission_min",
    )}, slippage_bps=0)
    if expected_model == "legacy_pct":
        assert model.buy_value(10, 100) == pytest.approx(1000.3)
        assert model.sell_value(10, 100) == pytest.approx(998.7)
    else:
        assert model.buy_value(10, 100) == pytest.approx(1000 + costs.get("commission_min", 1))


@pytest.mark.parametrize("minute_value", ["true", "1", "on", "TRUE"])
def test_cancel_matches_stream_key_with_all_fields(monkeypatch, minute_value):
    from urllib.parse import urlencode
    monkeypatch.setattr(backtest, "_running_jobs", {})
    params = dict(
        strategy_id="s", symbols="AAPL,MSFT", start="2026-01-05", end="2026-01-09",
        matching="close_t", entry_fill="open_t+1", exit_fill="signal_next_minute",
        fees_pct=0.0004, cost_model="legacy_pct", commission_pct=0.0003, stamp_tax_pct=0.001,
        commission_per_share=0.02, commission_min=2.0, slippage_bps=7.0,
        max_positions=4, max_exposure_pct=0.5, initial_capital=10000.0, position_sizing="score_weight",
        params='{"window":5}', overrides='{"score_min":0}', mode="full", holding_days=7,
        asset_type="etf", minute_fill=True, regime_filter='{"states":["up"]}',
    )
    key = backtest._make_job_key(**params)
    job = backtest._BacktestJob(key)
    backtest._running_jobs[key] = job
    async def body():
        return {"qs": urlencode({**params, "minute_fill": minute_value})}
    assert asyncio.run(backtest.strategy_cancel(SimpleNamespace(json=body))) == {"ok": True}
    assert job.cancel_event.is_set()
