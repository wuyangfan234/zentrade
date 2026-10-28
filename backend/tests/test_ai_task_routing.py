"""按任务选模型: task=None 行为不变; 任务覆盖生效; 关思考时传 reasoning_effort=none 且不传 temperature。"""
import asyncio
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.config import settings
from app.services import ai_provider


class _Recorder:
    """记录 _openai_client 的构造参数与 create() 的调用参数, 返回固定正文。"""

    def __init__(self):
        self.clients = []
        self.calls = []

    def client(self, api_key, timeout, base_url=None):
        self.clients.append({"api_key": api_key, "base_url": base_url})
        rec = self

        class _Completions:
            async def create(self, **kwargs):
                rec.calls.append(kwargs)
                msg = SimpleNamespace(content="ok", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


@pytest.fixture
def env(monkeypatch):
    stored = {"ai_api_key": "global-key", "ai_base_url": "https://global.example/v1", "ai_model": "global-model",
              "ai_provider": "openai_compat"}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 8192)
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 64000)
    rec = _Recorder()
    monkeypatch.setattr(ai_provider, "_openai_client", rec.client)
    return stored, rec


def _run(**kw):
    return asyncio.run(ai_provider.generate_ai_text([{"role": "user", "content": "hi"}], **kw))


def test_no_task_keeps_global_config_and_temperature(env):
    stored, rec = env
    assert _run(temperature=0.3, max_tokens=500) == "ok"
    assert rec.clients[-1] == {"api_key": "global-key", "base_url": None}  # None → 客户端内部读全局 base_url
    call = rec.calls[-1]
    assert call["model"] == "global-model"
    assert call["temperature"] == 0.3
    assert "reasoning_effort" not in call


def test_task_without_override_uses_global_model_but_task_default_thinking(env):
    stored, rec = env
    _run(temperature=0.3, max_tokens=500, task="news_translate")
    call = rec.calls[-1]
    assert call["model"] == "global-model"
    # news_translate 默认关思考: 传 reasoning_effort=none, 不传 temperature (Kimi 固定温度会拒)
    assert call["reasoning_effort"] == "none" and "temperature" not in call


def test_stock_analysis_task_default_keeps_thinking_and_temperature(env):
    stored, rec = env
    _run(temperature=0.3, max_tokens=500, task="stock_analysis")
    call = rec.calls[-1]
    assert call["temperature"] == 0.3 and "reasoning_effort" not in call


def test_task_override_switches_model_base_url_and_key(env):
    stored, rec = env
    stored["ai_task_overrides"] = {"news_translate": {"model": "k3-256k", "base_url": "https://kimi.example/v1",
                                                      "api_key": "task-key"}}
    _run(max_tokens=500, task="news_translate")
    assert rec.clients[-1] == {"api_key": "task-key", "base_url": "https://kimi.example/v1"}
    assert rec.calls[-1]["model"] == "k3-256k"
    # 未覆盖的任务不受影响
    _run(temperature=0.3, max_tokens=500, task="stock_analysis")
    assert rec.calls[-1]["model"] == "global-model" and rec.clients[-1]["api_key"] == "global-key"


def test_task_override_can_force_thinking_back_on(env):
    stored, rec = env
    stored["ai_task_overrides"] = {"news_translate": {"thinking": "default"}}
    _run(temperature=0.3, max_tokens=500, task="news_translate")
    assert "reasoning_effort" not in rec.calls[-1] and rec.calls[-1]["temperature"] == 0.3


def test_task_profile_reports_overridden_fields_without_leaking_secret_values(env):
    stored, _ = env
    stored["ai_task_overrides"] = {"news_translate": {"model": "k3", "api_key": "task-key"}}
    prof = ai_provider.task_profile("news_translate")
    assert prof["overridden"] == ["api_key", "model"]
    assert prof["thinking"] == "off"


def test_task_override_bypasses_global_codex_cli(env, monkeypatch):
    """全局切到 Codex CLI 时, 已配置独立模型的任务仍走 OpenAI 兼容通道 (翻译不能被 CLI 接管)。"""
    stored, rec = env
    stored["ai_task_overrides"] = {"news_translate": {"model": "k3"}}
    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda provider=None: True)

    async def boom(*a, **k):
        raise AssertionError("codex cli must not be used for an overridden task")
    monkeypatch.setattr(ai_provider, "_run_codex_cli", boom)
    assert _run(max_tokens=500, task="news_translate") == "ok"


# ── 思考档位 (2026-10-19): 关 / 低 / 中 / 高 / 极高 / 模型默认; 策略生成与助手对话两行接上实际调用 ──


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh"])
def test_task_thinking_level_sends_reasoning_effort(env, level):
    stored, rec = env
    stored["ai_task_overrides"] = {"stock_analysis": {"thinking": level}}
    _run(temperature=0.3, max_tokens=500, task="stock_analysis")
    call = rec.calls[-1]
    assert call["reasoning_effort"] == level
    assert call["temperature"] == 0.3  # 开思考档不预先去温度; 模型拒绝时由 400 回退移除


def test_task_thinking_level_applies_to_streaming(env, monkeypatch):
    stored, rec = env
    stored["ai_task_overrides"] = {"stock_analysis": {"thinking": "high"}}

    async def fake_iter(stream):
        yield "ok"
    monkeypatch.setattr(ai_provider, "_iter_openai_text", fake_iter)

    async def collect():
        return [c async for c in ai_provider.stream_ai_text(
            [{"role": "user", "content": "hi"}], max_tokens=None, prefer_final_answer=True, task="stock_analysis")]
    assert asyncio.run(collect()) == ["ok"]
    assert rec.calls[-1]["reasoning_effort"] == "high" and rec.calls[-1]["stream"] is True


def test_explicit_level_lifts_deepseek_thinking_disable(env, monkeypatch):
    """个股分析默认替 DeepSeek V4 关思考; 用户显式选了档位时两者矛盾, 以档位为准。"""
    stored, rec = env
    stored.update({"ai_base_url": "https://api.deepseek.com/v1", "ai_model": "deepseek-v4-pro"})

    async def fake_iter(stream):
        yield "ok"
    monkeypatch.setattr(ai_provider, "_iter_openai_text", fake_iter)

    async def collect():
        return [c async for c in ai_provider.stream_ai_text(
            [{"role": "user", "content": "hi"}], max_tokens=None, prefer_final_answer=True, task="stock_analysis")]

    asyncio.run(collect())
    assert rec.calls[-1]["extra_body"] == {"thinking": {"type": "disabled"}}  # 未选档位: 旧行为不变
    stored["ai_task_overrides"] = {"stock_analysis": {"thinking": "medium"}}
    asyncio.run(collect())
    assert "extra_body" not in rec.calls[-1] and rec.calls[-1]["reasoning_effort"] == "medium"


@pytest.mark.parametrize("provider", ["openai_compat", "openai"])
@pytest.mark.parametrize("thinking", ["", "default", "off", "low", "medium", "high", "xhigh", "garbage"])
def test_reported_effort_matches_what_is_sent(env, provider, thinking):
    """设置页「实际」显示的档位必须等于真正发出去的 reasoning_effort ('' = 不发送)。"""
    stored, rec = env
    stored["ai_provider"] = provider
    stored["ai_reasoning_effort"] = "medium"  # 全局推理强度只在 OpenAI 官方生效
    if thinking:
        stored["ai_task_overrides"] = {"stock_analysis": {"thinking": thinking}}
    _run(temperature=0.3, max_tokens=500, task="stock_analysis")
    sent = rec.calls[-1].get("reasoning_effort", "")
    assert ai_provider.task_reasoning_effort("stock_analysis") == sent


def test_no_override_keeps_current_requests_for_all_tasks(env):
    """没有单独配置时, 各任务请求与改造前一致 (自定义网关不发 reasoning_effort)。"""
    _, rec = env
    for task in ("stock_analysis", "strategy_gen", "assistant"):
        _run(temperature=0.3, max_tokens=500, task=task)
        call = rec.calls[-1]
        assert call["model"] == "global-model" and call["temperature"] == 0.3
        assert "reasoning_effort" not in call


def test_unsupported_level_falls_back_to_model_default():
    """网关 (CLIProxyAPI) 不认某档位时报 'level ... not supported, valid levels'; 去掉档位重试, 不让任务整体失败。"""
    import httpx
    import openai

    from app.custom.assistant import streaming

    msg = 'level "xhigh" not supported, valid levels: low, medium, high'
    body = {"error": {"message": msg, "type": "invalid_request_error"}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "http://gw.example/v1/chat/completions"))
    exc = openai.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)
    kwargs = {"max_tokens": 100, "temperature": 0.3, "reasoning_effort": "xhigh"}
    assert ai_provider._openai_retry_kwargs(exc, kwargs) == {"max_tokens": 100, "temperature": 0.3}
    assert streaming._retry_kwargs(exc, kwargs) == {"max_tokens": 100, "temperature": 0.3}


def test_strategy_generation_calls_use_strategy_gen_task(monkeypatch):
    from app.strategy.ai_generator import AIStrategyGenerator

    seen = []

    async def fake_generate(messages, **kw):
        seen.append(kw.get("task"))
        return "```python\nx = 1\n```"

    async def fake_stream(messages, **kw):
        seen.append(kw.get("task"))
        yield "x"

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)
    gen = AIStrategyGenerator()
    asyncio.run(gen._call_llm("p", "guide"))

    async def drain():
        return [c async for c in gen.stream("p")]
    asyncio.run(drain())
    assert seen == ["strategy_gen", "strategy_gen"]


def test_tool_loop_honors_strategy_gen_row(env):
    stored, rec = env
    stored["ai_task_overrides"] = {"strategy_gen": {"thinking": "high", "model": "gen-model"}}

    async def exec_tool(name, args):
        return {"ok": True}

    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    out = asyncio.run(ai_provider.generate_ai_text_with_tools(
        [{"role": "user", "content": "hi"}], tools, execute_tool=exec_tool, max_tokens=500, task="strategy_gen"))
    call = rec.calls[-1]
    assert call["model"] == "gen-model" and call["reasoning_effort"] == "high" and call["tools"] == tools
    assert out[-1] == {"role": "assistant", "content": "ok"}


def _fake_openai_factory(clients, calls):
    class _Stream:
        def __init__(self):
            delta = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
            self._chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")])]

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for c in self._chunks:
                yield c

    class _Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return _Stream()

    def factory(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return factory


def test_assistant_round_uses_assistant_task_row(env, monkeypatch):
    from app.custom.assistant import streaming

    stored, _ = env
    clients, calls = [], []
    monkeypatch.setattr(streaming, "AsyncOpenAI", _fake_openai_factory(clients, calls))

    async def one_round():
        return [e async for e in streaming.stream_openai_round([{"role": "user", "content": "hi"}], [], temperature=0.3)]

    events = asyncio.run(one_round())
    assert events[-1]["type"] == "round_end"
    assert calls[-1]["model"] == "global-model" and "reasoning_effort" not in calls[-1]  # 无单独配置: 与改造前一致
    assert clients[-1]["api_key"] == "global-key" and clients[-1]["base_url"] == "https://global.example/v1"

    stored["ai_task_overrides"] = {"assistant": {"thinking": "xhigh", "model": "chat-model",
                                                 "base_url": "https://chat.example/v1", "api_key": "chat-key"}}
    asyncio.run(one_round())
    assert calls[-1]["model"] == "chat-model" and calls[-1]["reasoning_effort"] == "xhigh"
    assert clients[-1]["api_key"] == "chat-key" and clients[-1]["base_url"] == "https://chat.example/v1"

    stored["ai_task_overrides"] = {"assistant": {"thinking": "off"}}
    asyncio.run(one_round())
    assert calls[-1]["reasoning_effort"] == "none" and "temperature" not in calls[-1]


def test_thinking_only_override_keeps_global_codex_cli(env, monkeypatch):
    """只改思考档位 (没填模型/Base URL/Key) 时, 全局是 Codex CLI 就照走 Codex; 不能因为档位改走缺 Key 的接口通道。"""
    stored, rec = env
    stored["ai_task_overrides"] = {"strategy_gen": {"thinking": "high"}}
    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda provider=None: True)
    used = []

    async def fake_codex(messages, **kw):
        used.append("codex")
        return "codex-ok"
    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_codex)
    assert _run(max_tokens=500, task="strategy_gen") == "codex-ok"

    async def collect():
        return [c async for c in ai_provider.stream_ai_text([{"role": "user", "content": "hi"}], task="strategy_gen")]
    assert asyncio.run(collect()) == ["codex-ok"]
    assert used == ["codex", "codex"] and rec.calls == []
    # 设置页「实际」不能显示一个不会发出去的档位
    assert ai_provider.task_reasoning_effort("strategy_gen") == ""
    assert ai_provider.task_route("strategy_gen") == "codex_cli"
    stored["ai_task_overrides"] = {"strategy_gen": {"thinking": "high", "model": "gpt-x"}}
    assert ai_provider.task_route("strategy_gen") == "api"
    assert ai_provider.task_reasoning_effort("strategy_gen") == "high"


def test_ai_tasks_api_accepts_levels(monkeypatch):
    from fastapi import HTTPException

    from app.api import settings as settings_api
    stored = {"ai_api_key": "placeholder", "ai_base_url": "https://global.example/v1", "ai_model": "m"}

    def save(updates):
        stored.update({k: v for k, v in updates.items() if v is not None})
        return stored
    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(secrets_store, "save", save)

    out = settings_api.save_ai_task("stock_analysis", settings_api.AiTaskOverrideIn(thinking="xhigh"))
    row = next(t for t in out["tasks"] if t["task"] == "stock_analysis")
    assert row["override"]["thinking"] == "xhigh"
    assert row["effective"]["thinking"] == "xhigh" and row["effective"]["reasoning_effort"] == "xhigh"
    news = next(t for t in out["tasks"] if t["task"] == "news_translate")
    assert news["effective"]["reasoning_effort"] == "none"
    for bad in ("maybe", "HIGH", "minimal"):
        with pytest.raises(HTTPException):
            settings_api.save_ai_task("stock_analysis", settings_api.AiTaskOverrideIn(thinking=bad))
    assert stored["ai_task_overrides"]["stock_analysis"] == {"thinking": "xhigh"}


def test_assistant_status_model_follows_assistant_row(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom.assistant import routes

    stored: dict = {"ai_provider": "openai_compat", "ai_api_key": "placeholder", "ai_model": "global-model"}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(routes, "ai_configured", lambda: True)
    app = FastAPI()
    app.include_router(routes.build_router())
    c = TestClient(app)
    assert c.get("/api/custom/assistant/status").json()["model"] == "global-model"
    stored["ai_task_overrides"] = {"assistant": {"model": "chat-model"}}
    assert c.get("/api/custom/assistant/status").json()["model"] == "chat-model"
    # 全局 Codex CLI: 助手不可用, 照旧显示全局 (Codex) 模型
    stored.update({"ai_provider": "codex_cli", "ai_codex_model": "gpt-5.6-sol"})
    body = c.get("/api/custom/assistant/status").json()
    assert body["model"] == "gpt-5.6-sol" and body["supports_tools"] is False


def test_ai_tasks_view_reports_codex_route_with_codex_model(monkeypatch):
    """全局 Codex CLI 时, 没单独填模型的任务「实际」显示 Codex 模型与通道, 不显示不会用到的接口模型/档位。"""
    from app.api import settings as settings_api
    stored = {"ai_provider": "codex_cli", "ai_model": "api-model", "ai_codex_model": "gpt-5.6-sol",
              "ai_task_overrides": {"strategy_gen": {"thinking": "high"}, "news_translate": {"model": "k3"}}}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(settings, "ai_provider", "codex_cli")
    rows = {t["task"]: t["effective"] for t in settings_api.get_ai_tasks()["tasks"]}
    assert rows["strategy_gen"]["route"] == "codex_cli" and rows["strategy_gen"]["model"] == "gpt-5.6-sol"
    assert rows["strategy_gen"]["reasoning_effort"] == "" and rows["strategy_gen"]["base_url"] == ""
    assert rows["news_translate"]["route"] == "api" and rows["news_translate"]["model"] == "k3"
    assert rows["news_translate"]["reasoning_effort"] == "none"


def test_ai_tasks_api_saves_masks_and_clears(monkeypatch):
    from fastapi import HTTPException
    from app.api import settings as settings_api
    stored = {"ai_api_key": "placeholder", "ai_base_url": "https://global.example/v1", "ai_model": "k3"}

    def save(updates):
        stored.update({k: v for k, v in updates.items() if v is not None})
        return stored
    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(secrets_store, "save", save)

    view = settings_api.get_ai_tasks()
    news = next(t for t in view["tasks"] if t["task"] == "news_translate")
    assert news["effective"]["thinking"] == "off" and news["effective"]["model"] == "k3"

    out = settings_api.save_ai_task("news_translate", settings_api.AiTaskOverrideIn(
        model="k3-256k", api_key="task-secret-abcdefgh", thinking="off"))
    news = next(t for t in out["tasks"] if t["task"] == "news_translate")
    assert news["override"]["model"] == "k3-256k" and news["override"]["has_api_key"]
    assert "task-secret-abcdefgh" not in str(out)                       # key 原文不回显
    assert stored["ai_task_overrides"]["news_translate"]["api_key"] == "task-secret-abcdefgh"

    # api_key=None 不动已存 key; 空串清除字段
    settings_api.save_ai_task("news_translate", settings_api.AiTaskOverrideIn(model=""))
    assert "model" not in stored["ai_task_overrides"]["news_translate"]
    assert stored["ai_task_overrides"]["news_translate"]["api_key"] == "task-secret-abcdefgh"
    settings_api.save_ai_task("news_translate", settings_api.AiTaskOverrideIn(api_key="", thinking=""))
    assert "news_translate" not in stored["ai_task_overrides"]

    with pytest.raises(HTTPException):
        settings_api.save_ai_task("nope", settings_api.AiTaskOverrideIn())
    with pytest.raises(HTTPException):
        settings_api.save_ai_task("news_translate", settings_api.AiTaskOverrideIn(thinking="maybe"))
    with pytest.raises(HTTPException):
        settings_api.save_ai_task("news_translate", settings_api.AiTaskOverrideIn(base_url="ftp://x"))
