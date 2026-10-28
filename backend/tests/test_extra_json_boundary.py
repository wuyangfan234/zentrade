"""Report #16: exercise the real FastAPI serialization boundary offline."""
import json
import math
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data, screener
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.strategy.engine import StrategyEngine, StrategyResult

DAY = date(2026, 10, 2)


def reject_constant(value):
    raise AssertionError(f"invalid JSON constant: {value}")


def strict_json(response):
    assert response.status_code == 200, response.text
    return json.loads(response.content, parse_constant=reject_constant)


def nested():
    return {"values": [{"nan": float("nan"), "upper": float("inf"), "lower": -float("inf")}],
            "date": DAY, "datetime": datetime(2026, 10, 2, 9, 30),
            "text": "NaN", "finite": 2.5, "flag": True, "none": None}


def assert_clean(value):
    assert value == {"values": [{"nan": None, "upper": None, "lower": None}],
                     "date": "2026-10-02", "datetime": "2026-10-02T09:30:00",
                     "text": "NaN", "finite": 2.5, "flag": True, "none": None}


def assert_original(value):
    assert math.isnan(value["values"][0]["nan"])
    assert value["values"][0]["upper"] == float("inf")
    assert value["values"][0]["lower"] == -float("inf")
    assert value["date"] is DAY
    assert isinstance(value["datetime"], datetime)


@pytest.fixture
def app(tmp_path):
    app = FastAPI()
    app.include_router(screener.router)
    app.include_router(ext_data.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return app


def install_result(app, monkeypatch, result):
    app.state.strategy_engine = SimpleNamespace(
        has=lambda sid: True, get=lambda sid: SimpleNamespace(meta={"id": sid}),
        run=lambda *a, **k: result,
        run_all=lambda *a, **k: {"demo": result},
    )
    monkeypatch.setattr(screener.ScreenerService, "run", lambda *a, **k: result)
    monkeypatch.setattr(screener.ScreenerService, "build_strategy_context", lambda *a, **k: object())
    monkeypatch.setattr(screener.ScreenerService, "coverage_warnings", lambda *a, **k: [])


@pytest.mark.parametrize("endpoint", ["run", "run_preset", "run_all"])
@pytest.mark.parametrize("field", ["rows", "total"])
def test_screener_execution_json(app, monkeypatch, endpoint, field):
    value = nested()
    result = StrategyResult(as_of=DAY, strategy_id="demo", rows=[{"symbol": "AAPL"}], total=1)
    if field == "rows":
        result.rows[0]["meta"] = value
    else:
        result.total = float("inf")
    install_result(app, monkeypatch, result)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.post(f"/api/screener/{endpoint}", json={
            "as_of": str(DAY), "conditions": [], "strategy_id": "demo", "strategy_ids": ["demo"],
        }))
    if endpoint == "run_all":
        payload = payload["results"]["demo"]
    if field == "rows":
        assert_clean(payload["rows"][0]["meta"])
        assert_original(value)
    else:
        assert payload["total"] is None
        assert math.isinf(result.total)


@pytest.mark.parametrize("progressive", [False, True])
def test_screener_summary_total(app, monkeypatch, progressive):
    result = StrategyResult(as_of=DAY, strategy_id="demo", total=float("nan"))
    install_result(app, monkeypatch, result)
    monkeypatch.setattr(screener.settings, "strategy_run_all_first_return_s", 1 if progressive else 0)

    def submit(key, ids, job):
        handle = screener.strategy_run_queue.StrategyRunHandle(key, ids)
        job(handle)
        handle.finish()
        return handle

    monkeypatch.setattr(screener.strategy_run_queue.MANAGER, "get_or_submit", submit)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.post("/api/screener/run_all", json={
            "as_of": str(DAY), "strategy_ids": ["demo"], "summary_only": True,
        }))
    assert payload["results"]["demo"]["total"] is None
    assert math.isnan(result.total)


def test_screener_real_literal_meta(app, tmp_path):
    directory = tmp_path / "strategies"
    directory.mkdir()
    code = '''import polars as pl
META = {"id": "demo", "name": "Demo", "params": [{"id": "threshold", "default": 1e309}], "audit": {"values": [-1e309], "text": "NaN"}}
def filter(df, params):
    return pl.lit(True)
'''
    source = directory / "demo.py"
    source.write_text(code)
    engine = StrategyEngine(strategy_dirs=[directory])
    engine.reload()
    app.state.strategy_engine = engine
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.get("/api/screener/strategies"))
    assert payload["presets"][0]["params"][0]["default"] is None
    assert math.isinf(engine.get("demo").meta["params"][0]["default"])
    assert source.read_text() == code


@pytest.mark.parametrize("endpoint", ["cached", "cached-result/demo"])
def test_screener_cached_nested_rows(app, monkeypatch, endpoint):
    value = nested()
    cached = {"as_of": str(DAY), "results": {"demo": {"as_of": str(DAY), "total": 1,
              "rows": [{"symbol": "AAPL", "meta": value}]}}, "updated_at": 0}
    monkeypatch.setattr(screener.strategy_cache, "read_cache", lambda *a: cached)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.get(f"/api/screener/{endpoint}"))
    result = payload["results"]["demo"] if endpoint == "cached" else payload["result"]
    assert_clean(result["rows"][0]["meta"])
    assert_original(value)


def install_ext(app, tmp_path):
    value = nested()
    frame = pl.DataFrame([{"symbol": "AAPL", "tags": "tech", "value": value}])
    config = ExtConfig(id="demo", label="Demo", mode="snapshot",
                       fields=[ExtField("symbol", "string"), ExtField("value", "string")],
                       symbol_map={"type": "mapped", "col": "value"},
                       pull=PullConfig(url="https://offline.invalid/data"))
    ExtConfigStore(tmp_path).upsert(config)
    frame.write_parquet(tmp_path / "ext_data" / "demo" / "part.parquet")
    return value


@pytest.mark.parametrize("endpoint", ["rows", "dimension-members?field=tags&value=tech"])
def test_ext_nested_parquet_response(app, tmp_path, endpoint):
    value = install_ext(app, tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.get(f"/api/ext-data/demo/{endpoint}"))
    assert_clean(payload["rows"][0]["value"])
    assert_original(value)
    stored = pl.read_parquet(tmp_path / "ext_data" / "demo" / "part.parquet").to_dicts()[0]["value"]
    assert math.isnan(stored["values"][0]["nan"])


def test_screener_ext_projection_json(app, tmp_path, monkeypatch):
    value = install_ext(app, tmp_path)
    result = StrategyResult(as_of=DAY, strategy_id="demo", rows=[{"symbol": "AAPL"}], total=1)
    install_result(app, monkeypatch, result)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.post("/api/screener/run", json={
            "as_of": str(DAY), "conditions": [], "ext_columns": "demo.value",
        }))
    assert_clean(payload["rows"][0]["demo__value"])
    assert_original(value)


@pytest.mark.parametrize("endpoint", ["detect-url", "demo/pull/test"])
def test_ext_preview_json_without_network(app, tmp_path, monkeypatch, endpoint):
    install_ext(app, tmp_path)
    value = nested()
    rows = [{"symbol": "AAPL", "value": value}]

    async def request_json(*a, **k):
        return rows

    async def request(self, method, url, **kwargs):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)

    monkeypatch.setattr(ext_data, "_request_json", request_json)
    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = strict_json(client.post(f"/api/ext-data/{endpoint}", json={"url": "https://offline.invalid/data"}))
    assert_clean(payload["preview"][0]["value"])
    assert_original(value)


@pytest.mark.parametrize("cleaner", [screener._safe, screener._safe_ext_value, ext_data._safe_json_value])
def test_cleaners_do_not_mutate_input(cleaner):
    value = {"rows": [{"scalar": float("nan"), "meta": nested()}], "total": float("inf")}
    cleaned = cleaner(value)
    assert cleaned["rows"][0]["scalar"] is None
    assert cleaned["total"] is None
    assert_clean(cleaned["rows"][0]["meta"])
    assert math.isnan(value["rows"][0]["scalar"])
    assert_original(value["rows"][0]["meta"])
    assert math.isinf(value["total"])
