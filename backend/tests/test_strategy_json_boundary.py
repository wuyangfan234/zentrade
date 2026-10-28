"""Exercise real HTTP/NDJSON serialization, not just a JSON helper."""
import json
import math
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy
from app.services.screener import ScreenerService
from app.strategy.engine import StrategyEngine, StrategyResult


CODE = '''import polars as pl
META = {
    "id": "custom_demo",
    "name": "Demo",
    "params": [{"id": "threshold", "default": 1e309}],
    "scoring": {},
    "audit": {"nested": [1e309, {"low": -1e309}], "pair": (1e309, 2.5)},
}
def filter(df, params):
    return pl.lit(True)
'''


def _reject_constant(token):
    raise AssertionError(f"invalid JSON constant: {token}")


def _strict_json(response):
    assert response.status_code == 200, response.text
    return json.loads(response.content, parse_constant=_reject_constant)


def _assert_meta(meta):
    assert meta["id"] == "custom_demo"
    assert meta["params"][0]["default"] is None
    assert meta["audit"] == {"nested": [None, {"low": None}], "pair": [None, 2.5]}


@pytest.fixture
def app(tmp_path):
    app = FastAPI()
    app.include_router(strategy.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])
    return app


def _run_engine(app, monkeypatch, result):
    # Keep the actual StrategyResult dataclass and HTTP boundary; isolate expensive data reads.
    app.state.strategy_engine = SimpleNamespace(
        get=lambda sid: SimpleNamespace(meta={"id": sid}),
        list_strategies=lambda: [{"id": "custom_demo"}],
        run=lambda *args, **kwargs: result,
        run_all=lambda *args, **kwargs: {"custom_demo": result},
    )
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda *a, **k: object())


@pytest.mark.parametrize("field", ["rows", "total", "elapsed_ms", "scores", "entry_signal_hits", "exit_signal_hits"])
def test_run_response_cleans_every_result_field(app, monkeypatch, field):
    nested = {"stats": [float("nan"), {"upper": float("inf"), "lower": -float("inf")}],
              "tuple": (float("inf"), 3.5), "ok": True, "none": None, "text": "NaN"}
    result = StrategyResult(as_of=date(2026, 10, 2), strategy_id="custom_demo", rows=[{"symbol": "AAPL"}], total=1)
    if field in ("total", "elapsed_ms"):
        setattr(result, field, float("inf"))
    elif field == "scores":
        result.scores = {"AAPL": float("nan")}
    else:
        setattr(result, field, [nested])
    _run_engine(app, monkeypatch, result)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.post("/api/strategies/run", json={
            "strategy_id": "custom_demo", "as_of": "2026-10-02",
        }))
    assert payload["strategy_id"] == "custom_demo"
    assert payload["as_of"] == "2026-10-02"
    if field in ("total", "elapsed_ms"):
        assert payload[field] is None
    elif field == "scores":
        assert payload[field] == {"AAPL": None}
        assert math.isnan(result.scores["AAPL"])
    else:
        assert payload[field] == [{"stats": [None, {"upper": None, "lower": None}],
                                   "tuple": [None, 3.5], "ok": True, "none": None, "text": "NaN"}]
        assert math.isnan(nested["stats"][0])


def test_run_all_response_cleans_summary_total(app, monkeypatch):
    result = StrategyResult(as_of=date(2026, 10, 2), strategy_id="custom_demo", total=float("inf"))
    _run_engine(app, monkeypatch, result)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.post("/api/strategies/run-all", json={"as_of": "2026-10-02"}))
    assert payload == {"as_of": "2026-10-02", "results": {"custom_demo": {"total": None, "as_of": "2026-10-02"}}}


@pytest.mark.parametrize("endpoint", ["/code/validate", "/code/save"])
def test_code_response_cleans_actual_meta_from_literal_source(app, endpoint):
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.post("/api/strategies" + endpoint, json={
            "strategy_id": "custom_demo", "code": CODE,
        }))
    _assert_meta(payload["meta"])
    if endpoint.endswith("save"):
        # Output cleaning must not silently rewrite the user's source or runtime META.
        assert app.state.strategy_engine.get("custom_demo").file_path.read_text() == CODE
        assert math.isinf(app.state.strategy_engine.get("custom_demo").meta["params"][0]["default"])


@pytest.mark.parametrize("endpoint", ["", "/custom_demo"])
def test_strategy_detail_response_cleans_nested_parameter_metadata(app, tmp_path, endpoint):
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True)
    (custom_dir / "custom_demo.py").write_text(CODE)
    app.state.strategy_engine.reload()
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.get("/api/strategies" + endpoint))
    detail = payload["strategies"][0] if not endpoint else payload
    assert detail["params"][0]["default"] is None
    assert detail["params_defaults"] == {"threshold": None}
    assert math.isinf(app.state.strategy_engine.get("custom_demo").meta["params"][0]["default"])


@pytest.mark.parametrize("endpoint", ["/build", "/ai/generate"])
def test_generated_meta_uses_same_json_boundary(app, monkeypatch, endpoint):
    async def generate(self, *args, **kwargs):
        return self.validate_code(CODE)

    monkeypatch.setattr(strategy.AIStrategyGenerator, "generate", generate)
    body = {"step": 1, "strategy_id": "custom_demo"} if endpoint == "/build" else {"prompt": "demo"}
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.post("/api/strategies" + endpoint, json=body))
    assert payload["valid"] is True
    _assert_meta(payload["meta"])


def test_build_stream_never_emits_nonstandard_json_constants(app, monkeypatch):
    async def stream(self, *args, **kwargs):
        yield CODE

    monkeypatch.setattr(strategy.AIStrategyGenerator, "stream", stream)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/strategies/build/stream", json={"step": 1, "strategy_id": "custom_demo"})
    assert response.status_code == 200
    events = [json.loads(line, parse_constant=_reject_constant) for line in response.iter_lines() if line]
    result = next(e for e in events if e.get("type") == "result")
    assert result["valid"] is True
    _assert_meta(result["meta"])
    assert next(e for e in events if e.get("type") == "delta")["content"] == CODE


def test_iterate_response_cleans_nested_result_without_mutating_it(app, monkeypatch):
    from app.services import ai_provider
    from app.strategy.ai_iterator import AIStrategyIterator

    result = {"meta": {"stats": [{"score": float("nan"), "interval": (float("inf"), 1.5)}]}, "ok": True}

    async def iterate(self, *args, **kwargs):
        return result

    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(AIStrategyIterator, "iterate", iterate)
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _strict_json(client.post("/api/strategies/ai/iterate", json={"name": "demo"}))
    assert payload == {"meta": {"stats": [{"score": None, "interval": [None, 1.5]}]}, "ok": True}
    assert math.isnan(result["meta"]["stats"][0]["score"])


def test_run_finite_response_is_unchanged(app, monkeypatch):
    result = StrategyResult(as_of=date(2026, 10, 2), strategy_id="custom_demo",
                            rows=[{"symbol": "AAPL", "score": 0.0}], total=1,
                            scores={"AAPL": 1.5})
    _run_engine(app, monkeypatch, result)
    with TestClient(app) as client:
        payload = _strict_json(client.post("/api/strategies/run", json={
            "strategy_id": "custom_demo", "as_of": "2026-10-02",
        }))
    assert payload == {"as_of": "2026-10-02", "strategy_id": "custom_demo", "rows": [{"symbol": "AAPL", "score": 0.0}],
                       "total": 1, "elapsed_ms": 0.0, "scores": {"AAPL": 1.5}, "entry_signal_hits": [], "exit_signal_hits": []}
