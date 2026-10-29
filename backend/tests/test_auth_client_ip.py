"""BH-01 (2026-10-11): 客户端 IP 判定与登录限流。

典型隧道链路: 公网 → Cloudflare → cloudflared 容器 → 宿主 3018 端口 → 容器内看到 peer=172.25.0.1 (docker 网关)。
局域网直连宿主 3018 端口在容器内同样是 peer=172.25.0.1, 且不带转发头。
Cloudflare 一定附带 Cf-Connecting-IP (访客真实 IP, 不可伪造), 并把访客 IP 追加到 X-Forwarded-For 末尾。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config

DOCKER_GW = "172.25.0.1"
VISITOR = "203.0.113.9"


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    from app.api import auth as auth_api
    from app.services import auth
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    yield
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()


def _auth_client(peer: str) -> TestClient:
    from app.api import auth as auth_api
    app = FastAPI()
    app.include_router(auth_api.router)
    return TestClient(app, client=(peer, 50000))


def _tunnel_headers(spoof: str | None = None) -> dict[str, str]:
    """Cloudflare 隧道请求的头: 访客自带的 XFF 在前, Cloudflare 追加真实 IP 在末尾。"""
    xff = f"{spoof}, {VISITOR}" if spoof else VISITOR
    return {"CF-Connecting-IP": VISITOR, "X-Forwarded-For": xff}


def test_tunnel_login_lockout_cannot_be_bypassed_by_rotating_xff() -> None:
    from app.services import auth
    auth.set_password("correct-horse")
    c = _auth_client(DOCKER_GW)
    for i in range(5):
        r = c.post("/api/auth/login", json={"password": "wrong"}, headers=_tunnel_headers(f"10.9.9.{i}"))
        assert r.status_code == 401, r.text
    # 第 6 次换一个伪造 XFF: 修前按伪造 IP 计数 → 401 继续猜; 修后按真实访客 IP 计数 → 429
    r = c.post("/api/auth/login", json={"password": "wrong"}, headers=_tunnel_headers("10.9.9.99"))
    assert r.status_code == 429, r.text


def test_tunnel_request_is_not_local_even_with_spoofed_loopback_xff() -> None:
    from app.api import auth as auth_api
    c = _auth_client(DOCKER_GW)
    for spoof in (None, "127.0.0.1", "192.168.1.2"):
        r = c.post("/api/auth/setup", json={"password": "attacker-pw"}, headers=_tunnel_headers(spoof))
        assert r.status_code == 403, (spoof, r.text)
    from app.services import auth
    assert not auth.is_configured()
    assert auth_api._fail_counter == {}


def test_unconfigured_middleware_rejects_tunnel_requests() -> None:
    from app.main import app
    c = TestClient(app, client=(DOCKER_GW, 50000))
    for spoof in (None, "127.0.0.1"):
        r = c.get("/api/version", headers=_tunnel_headers(spoof))
        assert r.status_code == 403, (spoof, r.status_code)


@pytest.mark.parametrize("peer, headers, local", [
    ("127.0.0.1", {}, True),                       # 宿主机本机 / 健康检查
    (DOCKER_GW, {}, True),                         # 局域网直连宿主 3018 端口 (经 docker NAT)
    ("192.168.1.20", {}, True),                    # 局域网直连 (保留源 IP 的部署)
    ("10.0.0.5", {}, True),
    ("8.8.8.8", {}, False),                        # 公网直连
    ("8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}, False),   # 公网伪造 XFF
    (DOCKER_GW, {"CF-Connecting-IP": VISITOR}, False),       # 隧道
    (DOCKER_GW, {"CF-Connecting-IP": VISITOR, "X-Forwarded-For": f"127.0.0.1, {VISITOR}"}, False),
    ("127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 192.168.1.20"}, True),  # 本机 Nginx: 取最右 (反代自己追加的) 一跳
    ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1, 8.8.4.4"}, False),    # 本机 Nginx 转公网: 伪造的最左段不采信
])
def test_client_ip_decision_table(peer: str, headers: dict[str, str], local: bool) -> None:
    from starlette.requests import Request
    from app.api import auth as auth_api
    scope = {"type": "http", "client": (peer, 1), "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    ip = auth_api._client_ip(Request(scope))
    assert auth_api._is_local_network(ip) is local, (peer, headers, ip)
