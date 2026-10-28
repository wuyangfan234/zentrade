"""Strategy source writes and rollback must never expose a partial .py file."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy
from app.services import fs_utils
from app.strategy.engine import StrategyEngine


def _code(sid, name="Original"):
    return f'''import polars as pl
META = {{
    "id": "{sid}",
    "name": "{name}",
    "params": [],
    "scoring": {{}},
}}
def filter(df, params):
    return pl.lit(True)
'''


@pytest.fixture
def app(tmp_path):
    app = FastAPI()
    app.include_router(strategy.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = StrategyEngine(strategy_dirs=[
        tmp_path / "strategies" / source for source in ("custom", "ai", "composite")
    ])
    return app


def _operation(app, client, kind, create=False):
    """Seed real engine state; return the actual HTTP operation and source path."""
    if kind == "composite":
        assert client.post("/api/strategies/code/save", json={
            "strategy_id": "custom_child", "code": _code("custom_child"),
        }).status_code == 200
        payload = {"strategy_id": "composite_demo", "name": "Original",
                   "children": [{"strategy_id": "custom_child", "weight": 1.0}]}
        url = "/api/strategies/composite/save"
        source = "composite"
    elif kind == "publish":
        payload = {"strategy_id": "ai_demo", "code": _code("ai_demo"), "target_source": "ai"}
        assert client.post("/api/strategies/code/save", json=payload).status_code == 200
        return "/api/strategies/ai_demo/publish", {}, app.state.strategy_engine.get("ai_demo").file_path
    elif kind == "legacy_ai":
        payload = {"strategy_id": "ai_demo", "code": _code("ai_demo")}
        url, source = "/api/strategies/ai/save", "ai"
    else:
        payload = {"strategy_id": "custom_demo", "code": _code("custom_demo")}
        url, source = "/api/strategies/code/save", "custom"
    if not create:
        assert client.post(url, json=payload).status_code == 200
        payload = {**payload, "mode": "update", "name": "Replacement"}
        if "code" in payload:
            payload["code"] = _code(payload["strategy_id"], "Replacement")
    path = app.state.repo.store.data_dir / "strategies" / source / f'{payload["strategy_id"]}.py'
    return url, payload, path


def _tear_write(monkeypatch, path, *, fail_at=1):
    real = Path.write_text
    observations = []
    calls = 0

    def write(candidate, text, *args, **kwargs):
        nonlocal calls
        if candidate.parent == path.parent and candidate.name in (path.name, path.name + ".tmp"):
            calls += 1
            if calls == fail_at:
                before = path.read_bytes() if path.exists() else None
                real(candidate, text[:8], *args, **kwargs)
                observations.append((before, path.read_bytes() if path.exists() else None))
                raise OSError(28, "simulated interrupted write")
        return real(candidate, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    return observations


@pytest.mark.parametrize("kind", ["custom", "composite", "publish", "legacy_ai"])
def test_interrupted_source_update_preserves_previous_source(app, monkeypatch, kind):
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload, path = _operation(app, client, kind)
        before = path.read_bytes()
        observed = _tear_write(monkeypatch, path)
        response = client.post(url, json=payload)
        assert response.status_code >= 400
        assert observed == [(before, before)]
        assert path.read_bytes() == before
        reloaded = StrategyEngine(strategy_dirs=app.state.strategy_engine._strategy_dirs)
        assert reloaded.load_errors() == []
        assert reloaded.get(path.stem).meta["name"] == "Original"


@pytest.mark.parametrize("kind", ["custom", "composite", "legacy_ai"])
def test_interrupted_source_create_leaves_no_discoverable_file(app, monkeypatch, kind):
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload, path = _operation(app, client, kind, create=True)
        observed = _tear_write(monkeypatch, path)
        response = client.post(url, json=payload)
        assert response.status_code >= 400
        assert observed == [(None, None)]
        assert not path.exists()
        reloaded = StrategyEngine(strategy_dirs=app.state.strategy_engine._strategy_dirs)
        assert reloaded.load_errors() == []
        assert not reloaded.has(path.stem)


def _fail_first_reload(monkeypatch, engine):
    reload = engine.reload
    calls = 0

    def reload_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("simulated load failure")
        return reload()

    monkeypatch.setattr(engine, "reload", reload_once)


@pytest.mark.parametrize("kind", ["custom", "composite", "publish", "legacy_ai"])
def test_rollback_is_atomic_and_restores_previous_runtime(app, monkeypatch, kind):
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload, path = _operation(app, client, kind)
        before = path.read_bytes()
        observed = []
        replace = fs_utils.os.replace

        def observe_replace(src, dst):
            if Path(dst) == path:
                observed.append((path.read_bytes(), Path(src).read_bytes()))
            return replace(src, dst)

        monkeypatch.setattr(fs_utils.os, "replace", observe_replace)
        _fail_first_reload(monkeypatch, app.state.strategy_engine)
        response = client.post(url, json=payload)
        assert response.status_code >= 400
        assert path.read_bytes() == before
        assert app.state.strategy_engine.get(path.stem).meta["name"] == "Original"
        assert len(observed) == 2
        assert observed[0][0] == before
        assert observed[1] == (observed[0][1], before)
        assert observed[0][1] != before


@pytest.mark.parametrize("kind", ["custom", "composite", "publish", "legacy_ai"])
def test_interrupted_rollback_preserves_complete_current_source(app, monkeypatch, kind):
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload, path = _operation(app, client, kind)
        before = path.read_bytes()
        observed = _tear_write(monkeypatch, path, fail_at=2)
        _fail_first_reload(monkeypatch, app.state.strategy_engine)
        response = client.post(url, json=payload)
        assert response.status_code >= 400
        assert len(observed) == 1
        complete_current, after = observed[0]
        assert complete_current != before
        assert after == complete_current
        assert path.read_bytes() == complete_current
        assert StrategyEngine(strategy_dirs=app.state.strategy_engine._strategy_dirs).load_errors() == []


@pytest.mark.parametrize("kind", ["custom", "composite", "legacy_ai"])
def test_failed_create_removes_new_source(app, monkeypatch, kind):
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload, path = _operation(app, client, kind, create=True)
        _fail_first_reload(monkeypatch, app.state.strategy_engine)
        response = client.post(url, json=payload)
        assert response.status_code >= 400
        assert not path.exists()
        assert not app.state.strategy_engine.has(path.stem)
