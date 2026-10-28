"""SDK conformance check: real HTTPX clients, MockTransport only (zero network)."""
import asyncio
import threading

import httpx
import pytest
import requests

from app.services import feishu_app as app


def test_sdk_httpx_clients_close_when_send_is_cancelled(monkeypatch):
    pytest.importorskip("lark_channel")
    from lark_channel.core.token.manager import TokenManager
    # SDK token acquisition is synchronous requests, so stub it explicitly.
    monkeypatch.setattr(TokenManager, "get_self_tenant_token", lambda config: "fake")
    monkeypatch.setattr(app, "_credentials", lambda: ("test-offline-lifecycle", "fake"))
    monkeypatch.setattr(app, "_saved_chat_id", lambda: "fake-chat")
    monkeypatch.setattr(app, "_TIMEOUT_S", 0.2)
    monkeypatch.setattr(app, "_CANCEL_TIMEOUT_S", 0.3, raising=False)
    real_client = httpx.AsyncClient
    clients, routes = [], []

    async def handler(request):
        routes.append(request.url.path)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake", "expire": 7200})
        if request.url.path.endswith("/im/v1/messages"):
            await asyncio.sleep(10)
        pytest.fail(f"unexpected mocked endpoint {request.url.path}")

    def client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs["trust_env"] = False
        instance = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(instance)
        return instance

    def forbid_sync(*args, **kwargs):
        pytest.fail("real/synchronous network forbidden")

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(requests, "request", forbid_sync)
    assert app.send_card("t", "", "body") is False
    for thread in threading.enumerate():
        if thread.name == "feishu-app-send":
            thread.join(2)
            assert not thread.is_alive()
    assert any(route.endswith("/im/v1/messages") for route in routes)
    assert clients and all(client.is_closed for client in clients)
