"""开源默认值守门: 同源 CORS、登录 Cookie Secure、新闻翻译兜底默认关、公开端点 UA 如实标识、Compose 默认只绑本机。"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config

REPO = Path(__file__).resolve().parents[2]
LAN_PEER = "192.168.1.20"
PUBLIC_PEER = "203.0.113.9"


@pytest.fixture
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    from app.api import auth as auth_api
    from app.services import auth
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    auth.set_password("correct-horse")
    yield
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()


def _login(peer: str, *, base_url: str = "http://testserver", headers: dict | None = None) -> str:
    from app.api import auth as auth_api
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app, client=(peer, 50000), base_url=base_url)
    r = client.post("/api/auth/login", json={"password": "correct-horse"}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.headers["set-cookie"]


def _is_secure(cookie: str) -> bool:
    return any(part.strip().lower() == "secure" for part in cookie.split(";"))


# ── 登录 Cookie ─────────────────────────────────────────────────────────────

def test_cookie_not_secure_on_plain_http_lan(isolated_auth, monkeypatch):
    monkeypatch.setattr(app_config.settings, "cookie_secure", "auto")
    cookie = _login(LAN_PEER)
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert not _is_secure(cookie)  # 纯 HTTP 内网: 带 Secure 浏览器会丢 Cookie, 无法登录


def test_cookie_secure_when_request_is_https(isolated_auth, monkeypatch):
    monkeypatch.setattr(app_config.settings, "cookie_secure", "auto")
    assert _is_secure(_login(PUBLIC_PEER, base_url="https://testserver"))


def test_cookie_secure_behind_trusted_https_proxy(isolated_auth, monkeypatch):
    monkeypatch.setattr(app_config.settings, "cookie_secure", "auto")
    assert _is_secure(_login(LAN_PEER, headers={"X-Forwarded-Proto": "https"}))


def test_cookie_ignores_forwarded_proto_from_public_peer(isolated_auth, monkeypatch):
    monkeypatch.setattr(app_config.settings, "cookie_secure", "auto")
    assert not _is_secure(_login(PUBLIC_PEER, headers={"X-Forwarded-Proto": "https"}))


@pytest.mark.parametrize(("mode", "expected"), [("true", True), ("false", False), ("1", True), ("off", False)])
def test_cookie_secure_explicit_modes(isolated_auth, monkeypatch, mode, expected):
    monkeypatch.setattr(app_config.settings, "cookie_secure", mode)
    assert _is_secure(_login(LAN_PEER, headers={"X-Forwarded-Proto": "https"} if not expected else {})) is expected


def test_logout_clears_cookie_with_matching_attributes(isolated_auth, monkeypatch):
    from app.api import auth as auth_api
    monkeypatch.setattr(app_config.settings, "cookie_secure", "auto")
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app, client=(LAN_PEER, 50000))
    r = client.post("/api/auth/logout", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "max-age=0" in cookie.lower() and _is_secure(cookie)


# ── CORS ──────────────────────────────────────────────────────────────────────

def _cors_headers(origin: str) -> dict:
    from app.main import app
    client = TestClient(app, client=("127.0.0.1", 50000))
    r = client.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    return {k.lower(): v for k, v in r.headers.items()}


def test_cors_default_is_same_origin_only():
    assert app_config.settings.cors_allow_origins == ""
    headers = _cors_headers("https://evil.example")
    assert "access-control-allow-origin" not in headers


def test_cors_allowlist_reflects_only_listed_origins(monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(app_config.settings, "cors_allow_origins", "https://ui.example.com/, https://b.example")
    try:
        reloaded = importlib.reload(main_mod)
        client = TestClient(reloaded.app, client=("127.0.0.1", 50000))
        ok = client.options("/health", headers={"Origin": "https://ui.example.com",
                                                "Access-Control-Request-Method": "GET"})
        assert ok.headers.get("access-control-allow-origin") == "https://ui.example.com"
        assert "access-control-allow-credentials" not in {k.lower() for k in ok.headers}
        bad = client.options("/health", headers={"Origin": "https://evil.example",
                                                 "Access-Control-Request-Method": "GET"})
        assert bad.headers.get("access-control-allow-origin") is None
    finally:
        monkeypatch.setattr(app_config.settings, "cors_allow_origins", "")
        importlib.reload(main_mod)


# ── 新闻翻译兜底默认关 ──────────────────────────────────────────────────────────

def test_google_translate_fallback_is_opt_in(tmp_path, monkeypatch):
    from app.services import news_ingest as ni
    from app.services import news_translate as nt

    now = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    ev = {"id": "alpaca:0", "headline": "Lumentum rips higher", "summary": "", "created_at": "2026-10-02T10:00:00Z",
          "symbols": ["LITE"], "mentions": {"LITE": "headline"}, "sources": ["alpaca"], "refs": [], "scope": "focused"}
    ni.save_shard(tmp_path, now, {ev["id"]: ev})

    async def down(*_a, **_k):
        raise RuntimeError("model down")

    calls: list[str] = []

    def no_network(url, *a, **k):  # 未开启时绝不能出网
        calls.append(url)
        raise AssertionError("google fallback must stay off by default")

    monkeypatch.setattr(app_config.settings, "news_google_translate_fallback", False)
    monkeypatch.setattr(nt.urllib.request, "urlopen", no_network)
    stats = asyncio.run(nt.translate_pending(tmp_path, now=now, ai_call=down))
    assert stats["fallback"] == 0 and calls == []
    assert not ni.load_shard(tmp_path, now)["alpaca:0"].get("zh_headline")


# ── 公开端点 UA 如实标识 ─────────────────────────────────────────────────────────

def test_public_endpoints_use_honest_user_agent():
    ua = app_config.PUBLIC_HTTP_USER_AGENT
    assert "ZenTrade" in ua
    assert not re.search(r"Chrome/|Safari/|Windows NT|Macintosh|Firefox/", ua)
    assert not re.search(r"Chrome/|Safari/|Windows NT|Macintosh", app_config.Settings().ai_user_agent)
    app_dir = REPO / "backend" / "app"
    offenders = [str(p.relative_to(REPO)) for p in app_dir.rglob("*.py")
                 if re.search(r"Mozilla/5\.0 \((Windows NT|Macintosh)|Chrome/\d|Safari/\d", p.read_text("utf-8"))]
    assert offenders == []


# ── 部署默认值 ─────────────────────────────────────────────────────────────────

def test_compose_binds_loopback_and_codex_is_opt_in():
    compose = (REPO / "docker-compose.yml").read_text("utf-8")
    assert '"${HOST:-127.0.0.1}:${PORT:-3018}:3018"' in compose
    assert ".codex" not in compose  # 主机凭据目录只在可选覆盖文件里挂载
    assert "/.codex}:/root/.codex:ro" in (REPO / "docker-compose.codex.yml").read_text("utf-8")


def test_dockerfile_defaults_to_upstream_registries_and_offline_runtime():
    dockerfile = (REPO / "Dockerfile").read_text("utf-8")
    assert "ARG USE_CN_MIRROR=1" not in dockerfile
    assert dockerfile.count("ARG USE_CN_MIRROR=0") == 4
    assert "--no-sync" in dockerfile and "UV_DEFAULT_INDEX=${PYPI_INDEX}" not in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_env_example_documents_new_settings():
    env = (REPO / ".env.example").read_text("utf-8")
    for key in ("HOST=127.0.0.1", "CORS_ALLOW_ORIGINS=", "COOKIE_SECURE=auto", "NEWS_GOOGLE_TRANSLATE_FALLBACK=0"):
        assert key in env, key
    json.dumps(env)  # 纯文本, 可序列化
