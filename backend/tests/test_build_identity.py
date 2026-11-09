"""A fixed product version must retain verifiable build/update identities."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.api import data, routes
from app.services import update_check


@pytest.fixture
def fixed_build(monkeypatch):
    monkeypatch.setattr(update_check, "__version__", "0.55.0")
    monkeypatch.setattr(update_check.settings, "update_check_enabled", True)
    monkeypatch.setattr(update_check.settings, "update_check_repo", "owner/repo")
    monkeypatch.setenv("ZENTRADE_COMMIT", "a" * 40)
    update_check._cache.clear()
    yield
    update_check._cache.clear()


def test_health_and_version_share_build_identity(monkeypatch):
    monkeypatch.setenv("ZENTRADE_COMMIT", "a" * 40)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(data.router)
    with TestClient(app) as client:
        health = client.get("/health").json()
        version = client.get("/api/data/version").json()
    assert health["version"] == __version__ == "0.55.0"
    assert version["version"] == "v0.55.0"
    assert health["revision"] == version["revision"] == "a" * 40


def test_build_revision_invalid_env_is_not_exposed(monkeypatch):
    from app import build_info

    monkeypatch.setenv("ZENTRADE_COMMIT", "not-a-commit")
    assert build_info.revision() == "unknown"


def test_source_checkout_revision_is_automatic(monkeypatch):
    import subprocess

    from app import build_info

    monkeypatch.delenv("ZENTRADE_COMMIT", raising=False)
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        assert build_info.revision() == "unknown"
        return
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert build_info.revision() == expected


def test_fixed_version_update_compares_commits(fixed_build, monkeypatch):
    calls = []

    def response(url):
        calls.append(url)
        if url.endswith("/commits/HEAD"):
            return 200, {"sha": "b" * 40}
        assert url.endswith(f'/compare/{"a" * 40}...{"b" * 40}')
        return 200, {"status": "ahead"}

    monkeypatch.setattr(update_check, "_get_json", response)
    result = update_check.check(now=1000)
    assert result["status"] == "update_available"
    assert result["source"] == "commit"
    assert result["current_revision"] == "a" * 40
    assert result["latest_revision"] == "b" * 40
    assert update_check.check(now=1001)["cached"] is True
    assert len(calls) == 2
    monkeypatch.setenv("ZENTRADE_COMMIT", "b" * 40)
    assert update_check.check(now=1002)["status"] == "latest"
    assert len(calls) == 3


@pytest.mark.parametrize("comparison", ["identical", "behind"])
def test_up_to_date_or_local_ahead_is_not_an_update(fixed_build, monkeypatch, comparison):
    monkeypatch.setattr(update_check, "_get_json", lambda url: (
        200, {"sha": "b" * 40} if url.endswith("/commits/HEAD") else {"status": comparison}
    ))
    assert update_check.check(force=True)["status"] == "latest"


@pytest.mark.parametrize("status,payload", [(404, None), (200, {"status": "diverged"}), (200, {})])
def test_unrelated_or_unknown_history_is_not_reported_latest(fixed_build, monkeypatch, status, payload):
    monkeypatch.setattr(update_check, "_get_json", lambda url: (
        (200, {"sha": "b" * 40}) if url.endswith("/commits/HEAD") else (status, payload)
    ))
    assert update_check.check(force=True)["status"] == "error"


def test_unknown_build_does_not_claim_latest(fixed_build, monkeypatch):
    monkeypatch.setenv("ZENTRADE_COMMIT", "dev")
    monkeypatch.setattr(update_check, "_get_json", lambda url: pytest.fail("unknown build must not compare"))
    assert update_check.check(force=True)["status"] == "error"


def test_docker_passes_revision_into_runtime():
    root = Path(__file__).resolve().parents[2]
    text = (root / "Dockerfile").read_text()
    assert "ENV ZENTRADE_COMMIT=${ZENTRADE_COMMIT}" in text
