from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import settings as settings_api
from app.api import strategy as strategy_api
from app.config import Settings
from app.custom.assistant import streaming as assistant_streaming
from app.services import ai_provider, market_mood, news_translate, qveris_data
from app.services import factor_intake as fi


def test_openai_entrypoints_default_to_900_seconds():
    for fn in (
        ai_provider.generate_ai_text,
        ai_provider.generate_ai_text_with_tools,
        ai_provider.stream_ai_text,
        assistant_streaming.stream_openai_round,
    ):
        assert inspect.signature(fn).parameters["timeout"].default == 900.0


def test_default_output_budget_allows_verified_qwen_xhigh_completion():
    assert Settings.model_fields["ai_max_output_tokens"].default == 32768


@pytest.mark.asyncio
async def test_nonstream_length_finish_is_not_returned_as_valid_text(monkeypatch):
    monkeypatch.setattr(secrets_store, "load", lambda: {
        "ai_provider": "openai_compat", "ai_api_key": "test-key", "ai_model": "qwen",
    })
    message = SimpleNamespace(content='{"items":[', tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="length")

    class _Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[choice], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(ai_provider, "_openai_client", lambda *a, **k: client)

    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        await ai_provider._run_openai_message_once(
            [{"role": "user", "content": "extract"}], temperature=0.0, max_tokens=16384, timeout=900.0,
        )


@pytest.mark.asyncio
async def test_global_ai_test_uses_reasoning_safe_budget(monkeypatch):
    seen = {}

    async def fake_generate(messages, **kwargs):
        seen.update(kwargs)
        return "OK"

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    result = await strategy_api.ai_test(SimpleNamespace())
    assert result["ok"] is True
    assert seen["timeout"] == 900.0
    assert seen["max_tokens"] >= 1024


@pytest.mark.asyncio
async def test_library_model_test_uses_reasoning_safe_budget(monkeypatch):
    seen = {}

    class _Completions:
        async def create(self, **kwargs):
            seen.update(kwargs)
            message = SimpleNamespace(content="OK")
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    def fake_client(api_key, timeout, base_url=None):
        seen["timeout"] = timeout
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    monkeypatch.setattr(ai_provider, "ai_model_entry", lambda model_id: {"id": model_id})
    monkeypatch.setattr(ai_provider, "model_profile", lambda entry: {
        "api_key": "test-key", "base_url": "https://example.test/v1", "model": "qwen",
    })
    monkeypatch.setattr(ai_provider, "_openai_client", fake_client)
    result = await settings_api.test_ai_model("qwen")
    assert result["ok"] is True
    assert seen["timeout"] == 900.0
    assert seen["max_tokens"] >= 1024


@pytest.mark.asyncio
async def test_library_model_test_rejects_empty_success_response(monkeypatch):
    class _Completions:
        async def create(self, **kwargs):
            message = SimpleNamespace(content="")
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(ai_provider, "ai_model_entry", lambda model_id: {"id": model_id})
    monkeypatch.setattr(ai_provider, "model_profile", lambda entry: {
        "api_key": "test-key", "base_url": "https://example.test/v1", "model": "qwen",
    })
    monkeypatch.setattr(ai_provider, "_openai_client", lambda *a, **k: client)
    result = await settings_api.test_ai_model("qwen")
    assert result["ok"] is False
    assert "正文" in result["error"]


def test_factor_triage_uses_reasoning_safe_budget(tmp_path, monkeypatch):
    now = datetime.now(UTC).isoformat()
    item = {
        "id": "qwen-triage", "source": "test", "source_name": "Test", "title": "Long article",
        "link": "", "published": now, "fetched_at": now, "prefilter": "llm", "stage": "queued",
        "body": "momentum close signal for stocks",
    }
    fi._save_items(tmp_path, {item["id"]: item})
    budgets = []

    def fake_call(data_dir, task, prompt, *, max_tokens, counted=True):
        budgets.append(max_tokens)
        return '{"market":"other","kind":"none","paywall":false}'

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(tmp_path)
    assert budgets == [4096]


def test_factor_extract_uses_verified_32k_budget(tmp_path, monkeypatch):
    now = datetime.now(UTC).isoformat()
    item = {
        "id": "qwen-long", "source": "test", "source_name": "Test", "title": "Long article",
        "link": "", "published": now, "fetched_at": now, "prefilter": "llm", "stage": "to_extract",
        "body": "momentum close signal for stocks",
    }
    fi._save_items(tmp_path, {item["id"]: item})
    monkeypatch.setattr(fi, "_rules_vocab", lambda data_dir: "")
    budgets = []

    def fake_call(data_dir, task, prompt, *, max_tokens, counted=True):
        budgets.append(max_tokens)
        return '{"summary_zh":"测试","items":[{"name_zh":"动量","quote":"momentum close signal","dsl":"close","direction":"high"}]}'

    monkeypatch.setattr(fi, "_call", fake_call)
    fi.process_queue(tmp_path)
    assert budgets == [32768]


@pytest.mark.asyncio
async def test_factor_ask_uses_long_timeout(monkeypatch):
    seen = {}

    async def fake_generate(messages, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    await fi._ask("factor_extract", "prompt", max_tokens=32768)
    assert seen["timeout"] == 900.0


def test_market_mood_uses_long_timeout(monkeypatch):
    seen = {}

    async def fake_generate(messages, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    try:
        market_mood._default_llm([])
    finally:
        if market_mood._LOOP is not None:
            market_mood._LOOP.close()
            market_mood._LOOP = None
    assert seen["timeout"] == 900.0


@pytest.mark.asyncio
async def test_news_translate_uses_long_timeout(monkeypatch):
    seen = {}

    async def fake_call(messages, **kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(news_translate, "parse_llm_output", lambda text, batch: {})
    result = await news_translate.translate_batch([{"id": "one"}], ai_call=fake_call)
    assert isinstance(result, dict)
    assert seen["timeout"] == 900.0
    assert seen["max_tokens"] >= 16384


@pytest.mark.asyncio
async def test_transcript_digest_uses_long_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(qveris_data.gate, "cache_get", lambda *a: None)
    monkeypatch.setattr(qveris_data, "transcript", lambda *a, **k: {
        "available": True,
        "segments": [{"speaker": "CEO", "title": "Chief Executive", "content": "Revenue grew."}],
    })
    monkeypatch.setattr(qveris_data.gate, "_cache_path", lambda *a: tmp_path / "digest.json")

    async def fake_call(messages, **kwargs):
        seen.update(kwargs)
        return "- 营收增长"

    result = await qveris_data.digest_transcript(tmp_path, "AAPL", "2026Q3", ai_call=fake_call)
    assert result["points"] == ["营收增长"]
    assert seen["timeout"] == 900.0
    assert seen["max_tokens"] >= 8192


def test_suspended_following_the_trend_is_disabled_and_html_is_clear(tmp_path):
    sources = fi.load_sources(tmp_path)
    clenow = next(s for s in sources if s["id"] == "clenow")
    assert clenow["enabled"] is False
    assert sum(1 for s in sources if s["enabled"]) == 22
    with pytest.raises(ValueError, match="HTML"):
        fi.parse_feed(b"<!DOCTYPE html><html><title>Account Suspended</title></html>")


def test_assistant_chat_service_does_not_override_long_timeout():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("app/custom/assistant/chat_service.py").read_text(encoding="utf-8")
    assert "timeout=240.0" not in src
    assert "OPENAI_REQUEST_TIMEOUT_S" in src


@pytest.mark.asyncio
async def test_assistant_stream_awaits_async_close(monkeypatch):
    closed = {"awaited": False}

    class _Stream:
        def __init__(self):
            self._chunks = [SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", delta=SimpleNamespace(content="OK", reasoning_content=None, tool_calls=None))])]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop(0)

        async def close(self):
            closed["awaited"] = True

    class _Completions:
        async def create(self, **kwargs):
            return _Stream()

    monkeypatch.setattr(assistant_streaming, "task_profile", lambda task: {
        "api_key": "k", "base_url": "https://example.test/v1", "model": "qwen", "thinking": "default"})
    monkeypatch.setattr(assistant_streaming, "_client", lambda timeout, profile=None: SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())))
    events = [e async for e in assistant_streaming.stream_openai_round([{"role": "user", "content": "hi"}], [])]
    assert events[-1]["type"] == "round_end"
    assert closed["awaited"] is True
