"""A corrupt menu must be diagnosable without hiding healthy user menus."""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analysis


@pytest.mark.parametrize("broken, exception_type", [
    (b'{"id":', "JSONDecodeError"),
    (b'{"id":"broken","label":"private-label","data_source":"secret","order":"private-value"}',
     "ValidationError"),
    (b'["private-value"]', "TypeError"),
    (b'\xff\xfe', "UnicodeDecodeError"),
])
def test_bad_menu_warns_without_hiding_healthy_menus(tmp_path, caplog, broken, exception_type):
    base = tmp_path / "analysis_menus"
    base.mkdir()
    for sid, order in [("healthy_a", 5), ("healthy_b", 1)]:
        (base / f"{sid}.json").write_text(json.dumps({
            "id": sid, "label": sid, "data_source": "demo", "order": order,
        }), encoding="utf-8")
    bad = base / "broken.json"
    bad.write_bytes(broken)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.iterdir()}
    app = FastAPI()
    app.include_router(analysis.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with caplog.at_level(logging.WARNING, logger="app.api.analysis"), TestClient(app) as client:
        response = client.get("/api/analysis-menus")
        assert response.status_code == 200
        assert [m["id"] for m in response.json()["items"]] == ["healthy_b", "healthy_a"]
        assert client.get("/api/analysis-menus/healthy_a").json()["id"] == "healthy_a"
    warnings = [r for r in caplog.records if r.name == "app.api.analysis" and r.levelno >= logging.WARNING]
    assert warnings, "corrupt persisted menu was silently discarded"
    assert all("broken.json" in r.message and exception_type in r.message for r in warnings)
    assert "private-label" not in caplog.text
    assert "private-value" not in caplog.text
    assert "secret" not in caplog.text
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.iterdir()} == before


def test_missing_menu_directory_remains_empty_and_quiet(tmp_path, caplog):
    app = FastAPI()
    app.include_router(analysis.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with caplog.at_level(logging.WARNING, logger="app.api.analysis"), TestClient(app) as client:
        response = client.get("/api/analysis-menus")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert not [r for r in caplog.records if r.name == "app.api.analysis"]
