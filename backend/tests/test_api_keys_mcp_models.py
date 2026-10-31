"""0.44.0: 对外 API 密钥 + MCP 端点 + 模型库 + 新版本检查。

鉴权测试走真实 app.main 中间件 (TestClient), 数据目录全部隔离到 tmp。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import secrets_store

PUBLIC_PEER = "203.0.113.9"
OWNER_PW = "placeholder"


@pytest.fixture
def iso(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    from app.api import auth as auth_api
    from app.services import auth
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    auth.set_password(OWNER_PW)
    yield tmp_path
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()


def _client(peer: str = PUBLIC_PEER) -> TestClient:
    from app.main import app
    return TestClient(app, client=(peer, 50000))


def _login(c: TestClient) -> None:
    r = c.post("/api/auth/login", json={"password": OWNER_PW})
    assert r.status_code == 200, r.text


def _make_key(scope: str) -> str:
    c = _client()
    _login(c)
    r = c.post("/api/settings/api-keys", json={"name": f"agent-{scope}", "scope": scope})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token.startswith("zt_")
    return token


# ── API 密钥 ──────────────────────────────────────────────────────────

def test_key_plaintext_never_persisted_and_list_hides_it(iso: Path) -> None:
    token = _make_key("full")
    raw = (iso / "user_data" / "api_keys.json").read_text(encoding="utf-8")
    assert token not in raw
    assert oct((iso / "user_data" / "api_keys.json").stat().st_mode & 0o777) == "0o600"
    c = _client()
    _login(c)
    listed = c.get("/api/settings/api-keys").json()["keys"]
    assert len(listed) == 1 and "hash" not in listed[0] and "token" not in listed[0]
    assert listed[0]["prefix"] == token[:10]


def test_no_credentials_still_401_from_public(iso: Path) -> None:
    assert _client().get("/api/watchlist").status_code == 401


def test_full_key_can_read_and_write_with_bearer_or_header(iso: Path) -> None:
    token = _make_key("full")
    c = _client()
    r = c.get("/api/settings/ai/models", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    r = c.post("/api/settings/ai/models", headers={"X-API-Key": token},
               json={"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"})
    assert r.status_code == 200, r.text


def test_read_key_blocks_writes_but_allows_get(iso: Path) -> None:
    token = _make_key("read")
    h = {"Authorization": f"Bearer {token}"}
    c = _client()
    assert c.get("/api/settings/ai/models", headers=h).status_code == 200
    r = c.post("/api/settings/ai/models", headers=h, json={"name": "x", "model": "y"})
    assert r.status_code == 403 and "只读" in r.json()["detail"]
    assert c.delete("/api/watchlist", headers=h).status_code == 403


@pytest.mark.parametrize("scope", ["full", "read"])
def test_no_key_can_touch_auth_or_key_management(iso: Path, scope: str) -> None:
    token = _make_key(scope)
    h = {"Authorization": f"Bearer {token}"}
    c = _client()
    assert c.get("/api/settings/api-keys", headers=h).status_code == 403
    assert c.post("/api/settings/api-keys", headers=h, json={"name": "evil", "scope": "full"}).status_code == 403
    r = c.post("/api/auth/change-password", headers=h, json={"old_password": "x", "new_password": "hijacked"})
    assert r.status_code == 403


@pytest.mark.parametrize("bad", ["zt_nope", "zt_" + "A" * 43, "Bearer zt_", "zt_" + "x" * 300])
def test_bad_or_unknown_key_is_401_not_cookie_fallback(iso: Path, bad: str) -> None:
    c = _client()
    _login(c)  # 带着有效 Cookie, 但只要带了密钥头就只按密钥判定
    r = c.get("/api/watchlist", headers={"X-API-Key": bad})
    assert r.status_code == 401


def test_revoked_and_expired_keys_rejected(iso: Path) -> None:
    from app.services import api_keys
    token = _make_key("full")
    c = _client()
    _login(c)
    kid = c.get("/api/settings/api-keys").json()["keys"][0]["id"]
    assert c.delete(f"/api/settings/api-keys/{kid}").status_code == 200
    assert _client().get("/api/watchlist", headers={"X-API-Key": token}).status_code == 401
    # 过期
    _info, tok2 = api_keys.create_key("short", "read", 1, now=1_000_000.0)
    assert api_keys.verify(tok2, now=1_000_000.0 + 3600) is not None
    assert api_keys.verify(tok2, now=1_000_000.0 + 86400 + 1) is None


def test_corrupt_key_file_fails_closed(iso: Path) -> None:
    token = _make_key("full")
    (iso / "user_data" / "api_keys.json").write_text("{not json", encoding="utf-8")
    assert _client().get("/api/watchlist", headers={"X-API-Key": token}).status_code == 401


def test_create_key_validation(iso: Path) -> None:
    c = _client()
    _login(c)
    assert c.post("/api/settings/api-keys", json={"name": "a", "scope": "root"}).status_code == 422
    assert c.post("/api/settings/api-keys", json={"name": "", "scope": "read"}).status_code == 422
    assert c.post("/api/settings/api-keys", json={"name": "a", "scope": "read", "expires_days": 0}).status_code == 422


# ── MCP ───────────────────────────────────────────────────────────────

def _rpc(c: TestClient, h: dict, method: str, params: dict | None = None, mid: int = 1):
    return c.post("/api/mcp", headers=h, json={"jsonrpc": "2.0", "id": mid, "method": method,
                                                "params": params or {}})


def test_mcp_requires_auth(iso: Path) -> None:
    r = _client().post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401


def test_mcp_initialize_tools_list_and_notification(iso: Path) -> None:
    h = {"Authorization": f"Bearer {_make_key('read')}"}
    c = _client()
    r = _rpc(c, h, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})
    body = r.json()
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["serverInfo"]["name"] == "zentrade"
    assert "tools" in body["result"]["capabilities"]
    r = c.post("/api/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    tools = {t["name"]: t for t in _rpc(c, h, "tools/list").json()["result"]["tools"]}
    for name in ("watchlist_add", "strategy_save", "backtest_strategy", "zentrade_api", "zentrade_endpoints"):
        assert name in tools
        assert tools[name]["inputSchema"]["type"] == "object"
    assert tools["watchlist_list"]["annotations"]["readOnlyHint"] is True
    # 未知版本回退到最新
    r = _rpc(c, h, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"


def test_mcp_tool_call_reuses_api_and_scope(iso: Path) -> None:
    c = _client()
    full = {"Authorization": f"Bearer {_make_key('full')}"}
    read = {"Authorization": f"Bearer {_make_key('read')}"}
    # full: 通用入口写一条模型库
    r = _rpc(c, full, "tools/call", {"name": "zentrade_api", "arguments": {
        "method": "POST", "path": "/api/settings/ai/models",
        "body": {"name": "Claude", "base_url": "https://gw.example/v1", "model": "claude-x"}}})
    res = r.json()["result"]
    assert res["isError"] is False, res
    assert "claude-x" in res["content"][0]["text"]
    # read: 同样的写操作被中间件 403, 以工具错误返回 (不是协议错误)
    r = _rpc(c, read, "tools/call", {"name": "zentrade_api", "arguments": {
        "method": "POST", "path": "/api/settings/ai/models", "body": {"name": "x", "model": "y"}}})
    res = r.json()["result"]
    assert res["isError"] is True and "403" in res["content"][0]["text"]
    # 读: 列表能看到刚写的
    r = _rpc(c, read, "tools/call", {"name": "zentrade_api", "arguments": {"method": "GET", "path": "/api/settings/ai/models"}})
    assert "Claude" in r.json()["result"]["content"][0]["text"]
    # MCP 也不能绕去管密钥
    r = _rpc(c, full, "tools/call", {"name": "zentrade_api", "arguments": {"method": "GET", "path": "/api/settings/api-keys"}})
    assert r.json()["result"]["isError"] is True


def test_mcp_endpoints_tool_and_errors(iso: Path) -> None:
    c = _client()
    h = {"Authorization": f"Bearer {_make_key('read')}"}
    r = _rpc(c, h, "tools/call", {"name": "zentrade_endpoints", "arguments": {"keyword": "watchlist"}})
    data = json.loads(r.json()["result"]["content"][0]["text"])
    paths = {e["path"] for e in data["endpoints"]}
    assert "/api/watchlist/batch" in paths and not any(p.startswith("/api/settings/api-keys") for p in paths)
    assert _rpc(c, h, "nope/method").json()["error"]["code"] == -32601
    assert _rpc(c, h, "tools/call", {"name": "nope"}).json()["error"]["code"] == -32602
    r = _rpc(c, h, "tools/call", {"name": "watchlist_add", "arguments": {}})
    assert r.json()["result"]["isError"] is True
    r = _rpc(c, h, "tools/call", {"name": "zentrade_api", "arguments": {"method": "GET", "path": "/health"}})
    assert r.json()["result"]["isError"] is True
    assert c.post("/api/mcp", headers=h, content=b"{oops").json()["error"]["code"] == -32700
    assert c.get("/api/mcp", headers=h).status_code == 405
    # 批量
    r = c.post("/api/mcp", headers=h, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                            {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert r.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


# ── 模型库 ────────────────────────────────────────────────────────────

def test_model_library_crud_and_task_reference(iso: Path) -> None:
    from app.services import ai_provider
    secrets_store.save({"ai_api_key": "global-key", "ai_base_url": "https://global/v1", "ai_model": "global-m",
                        "ai_provider": "openai_compat"})
    c = _client()
    _login(c)
    r = c.post("/api/settings/ai/models", json={"name": "DeepSeek 翻译", "base_url": "https://api.deepseek.com",
                                                "model": "deepseek-chat", "api_key": "placeholder-ds"})
    ds = r.json()["created"]
    r = c.post("/api/settings/ai/models", json={"name": "Claude", "base_url": "https://claude-gw/v1", "model": "claude-x"})
    cl = r.json()["created"]
    models = c.get("/api/settings/ai/models").json()["models"]
    assert [m["id"] for m in models] == [ds, cl]
    assert models[0]["has_api_key"] and "placeholder-ds" not in json.dumps(models)
    assert not models[1]["has_api_key"]

    assert c.post("/api/settings/ai/tasks/news_translate", json={"model_ref": ds, "thinking": "off"}).status_code == 200
    assert c.post("/api/settings/ai/tasks/assistant", json={"model_ref": cl}).status_code == 200
    assert c.post("/api/settings/ai/tasks/assistant", json={"model_ref": "ghost"}).status_code == 400

    p = ai_provider.task_profile("news_translate")
    assert (p["model"], p["base_url"], p["api_key"], p["thinking"]) == (
        "deepseek-chat", "https://api.deepseek.com", "placeholder-ds", "off")
    p = ai_provider.task_profile("assistant")
    assert (p["model"], p["api_key"]) == ("claude-x", "global-key")  # 库模型未填 key → 全局 key
    p = ai_provider.task_profile("stock_analysis")
    assert p["model"] == "global-m" and p["model_ref"] == ""

    rows = {t["task"]: t for t in c.get("/api/settings/ai/tasks").json()["tasks"]}
    assert rows["news_translate"]["effective"]["model_ref_name"] == "DeepSeek 翻译"
    assert rows["news_translate"]["override"]["model_ref"] == ds

    # 被引用的模型不能删; 取消引用后可删
    r = c.delete(f"/api/settings/ai/models/{ds}")
    assert r.status_code == 409 and "新闻翻译" in r.json()["detail"]
    assert c.post("/api/settings/ai/tasks/news_translate", json={"model_ref": "", "thinking": "off"}).status_code == 200
    assert c.delete(f"/api/settings/ai/models/{ds}").status_code == 200
    # 更新: api_key=None 不动, "" 清除
    r = c.put(f"/api/settings/ai/models/{cl}", json={"name": "Claude 助手", "base_url": "https://claude-gw/v1",
                                                     "model": "claude-y", "api_key": "placeholder-cl"})
    assert r.json()["models"][0]["has_api_key"]
    r = c.put(f"/api/settings/ai/models/{cl}", json={"name": "Claude 助手", "model": "claude-y"})
    assert r.json()["models"][0]["has_api_key"]
    assert ai_provider.task_profile("assistant")["api_key"] == "placeholder-cl"


def test_dangling_model_ref_falls_back_and_routes_like_before(iso: Path, monkeypatch) -> None:
    from app.services import ai_provider
    secrets_store.save({"ai_api_key": "g", "ai_model": "global-m", "ai_provider": "codex_cli",
                        "ai_task_overrides": {"assistant": {"model_ref": "deleted-one"}}})
    monkeypatch.setattr(app_config.settings, "ai_provider", "codex_cli")
    p = ai_provider.task_profile("assistant")
    assert p["model_ref_missing"] is True and p["model"] == "global-m"
    assert ai_provider.task_route("assistant") == ai_provider.CODEX_CLI_PROVIDER


def test_model_ref_routes_to_api_even_when_global_is_codex(iso: Path, monkeypatch) -> None:
    from app.services import ai_provider
    secrets_store.save({"ai_provider": "codex_cli", "ai_models": [{"id": "ds", "name": "DS", "model": "deepseek-chat",
                                                                   "base_url": "https://x", "api_key": "k"}],
                        "ai_task_overrides": {"news_translate": {"model_ref": "ds"}}})
    monkeypatch.setattr(app_config.settings, "ai_provider", "codex_cli")
    assert ai_provider.task_route("news_translate") == ai_provider.API_ROUTE
    assert ai_provider.task_route("assistant") == ai_provider.CODEX_CLI_PROVIDER


# ── 新版本检查 ────────────────────────────────────────────────────────

def test_update_check_release_then_version_fallback_and_cache(iso: Path, monkeypatch) -> None:
    from app.services import update_check
    update_check._cache.clear()
    monkeypatch.setattr(update_check, "__version__", "0.44.0")
    calls = []

    def fake_json(url):
        calls.append(url)
        return 200, {"tag_name": "v0.45.1", "html_url": "https://github.com/o/r/releases/tag/v0.45.1", "body": "notes"}

    monkeypatch.setattr(update_check, "_get_json", fake_json)
    r = update_check.check(now=1000.0)
    assert r["status"] == "update_available" and r["latest"] == "v0.45.1" and r["source"] == "release"
    r2 = update_check.check(now=1000.0 + 60)
    assert r2["cached"] is True and len(calls) == 1
    update_check.check(force=True, now=1000.0 + 61)
    assert len(calls) == 2

    # 没有 Release → 读 VERSION
    update_check._cache.clear()
    monkeypatch.setattr(update_check, "_get_json", lambda url: (404, None))
    monkeypatch.setattr(update_check, "_get_text", lambda url: (200, "v0.44.0\n"))
    r = update_check.check(now=5000.0)
    assert r["status"] == "latest" and r["source"] == "version_file"

    update_check._cache.clear()
    monkeypatch.setattr(update_check, "_get_text", lambda url: (404, ""))
    r = update_check.check(now=9000.0)
    assert r["status"] == "error" and "私有" in r["error"]

    def boom(url):
        raise OSError("down")

    update_check._cache.clear()
    monkeypatch.setattr(update_check, "_get_json", boom)
    assert update_check.check(now=9500.0)["status"] == "error"


def test_update_check_disabled_and_bad_repo(iso: Path, monkeypatch) -> None:
    from app.services import update_check
    update_check._cache.clear()
    monkeypatch.setattr(app_config.settings, "update_check_enabled", False)
    assert update_check.check()["status"] == "disabled"
    monkeypatch.setattr(app_config.settings, "update_check_enabled", True)
    monkeypatch.setattr(app_config.settings, "update_check_repo", "not a repo")
    assert update_check.check()["status"] == "error"


@pytest.mark.parametrize("a,b,newer", [("v0.45.0", "0.44.9", True), ("0.44.0", "0.44.0", False),
                                       ("v1.0", "0.99.99", True), ("0.43.9", "0.44.0", False)])
def test_version_compare(a: str, b: str, newer: bool) -> None:
    from app.services.update_check import is_newer
    assert is_newer(a, b) is newer


def test_mcp_large_result_stays_valid_json_and_strategy_list_is_summarized(iso: Path) -> None:
    from app.api import mcp
    big = {"rows": ["x" * 100] * 2000}
    out = json.loads(mcp._text_result(big, False)["content"][0]["text"])
    assert out["truncated"] is True and out["original_chars"] > mcp._MAX_RESULT_CHARS
    summ = mcp._summarize_strategies({"strategies": [{"id": "a", "name": "A", "description": "d" * 500, "source": "builtin",
                                                      "params": [{"id": "n", "default": 3}], "scoring": {"huge": 1}}],
                                      "load_errors": []})
    assert summ == {"count": 1, "load_errors": [], "strategies": [{
        "id": "a", "name": "A", "description": "d" * 160, "source": "builtin", "research_only": False,
        "timeframes": None, "params": ["n"]}]}
