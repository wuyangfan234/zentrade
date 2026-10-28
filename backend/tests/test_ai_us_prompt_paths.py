"""美股生成/修改真实消息路径及助手可见工具说明；模型边界全部替身。"""
from __future__ import annotations

import json
import re

import pytest

from app.api.strategy import BuildRequest, _build_prompt
from app.custom.assistant import prompt, tools
from app.services import ai_provider
from app.strategy.ai_generator import AIStrategyGenerator, GUIDE_PATH
from app.strategy.prompt_builder import build_step1


CODE = '''import polars as pl
META = {"id": "ai_us", "params": [], "scoring": {}}
ENTRY_SIGNALS = ["signal_ma20_breakout"]
def filter(df, params):
    return pl.col("close") > pl.col("ma20")
'''
RETIRED = ("你是A股", "signal_limit_", "signal_broken_limit_up",
           "consecutive_limit_", '"exclude_st": True', "上次涨停价")


def assert_us_contract(text):
    for phrase in ("美股", "美元", "美东", "09:30", "16:00", "无午休", "财务", "缺失"):
        assert phrase in text, phrase
    for token in RETIRED:
        assert token not in text, token


@pytest.mark.parametrize("filename", [
    "strategy-guide-compact.md", "strategy-guide.md", "strategy-builder-step1.md",
    "strategy-builder-step2.md", "strategy-example.md",
])
def test_referenced_guides_teach_the_same_us_contract(filename):
    assert_us_contract((GUIDE_PATH.parent / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ["generate", "modify", "repair", "stream"])
async def test_real_model_message_paths_use_us_contract(monkeypatch, path):
    messages_seen = []

    async def fake_generate(messages, **kwargs):
        messages_seen.append(messages)
        return CODE

    async def fake_stream(messages, **kwargs):
        messages_seen.append(messages)
        yield CODE

    monkeypatch.setattr(ai_provider, "generate_ai_text", fake_generate)
    monkeypatch.setattr(ai_provider, "stream_ai_text", fake_stream)
    gen = AIStrategyGenerator()
    if path == "stream":
        assert "".join([chunk async for chunk in gen.stream("生成均线策略")]) == CODE
    elif path == "repair":
        assert (await gen.repair_code("META = {}", "找不到策略入口"))["valid"]
    else:
        req = (BuildRequest(step=2, current_code=CODE, instruction="调整均线")
               if path == "modify" else BuildRequest(step=1, name="均线", rules="收盘站上MA20"))
        assert (await gen.generate(_build_prompt(req)))["valid"]
    assert len(messages_seen) == 1
    assert_us_contract(messages_seen[0][0]["content"])
    if path == "modify":
        assert_us_contract(messages_seen[0][1]["content"])


@pytest.mark.parametrize("legacy", [{"exclude_st": True}, {"boards": ["沪主板"]}],
                         ids=["st-filter", "cn-boards"])
def test_step1_rejects_incompatible_saved_defaults_without_silent_changes(legacy):
    with pytest.raises(ValueError, match="美股"):
        build_step1("均线", "", "long", "收盘站上MA20", basic_filter=legacy)


@pytest.mark.parametrize("field", ["signal_limit_up", "signal_broken_limit_up",
                                    "consecutive_limit_ups", "consecutive_limit_downs"])
def test_generated_legacy_signal_or_feature_is_invalid(field):
    code = CODE.replace('pl.col("close") > pl.col("ma20")', f'pl.col("{field}") > 0')
    result = AIStrategyGenerator().validate_code(code)
    assert result["valid"] is False
    assert "美股" in result["error"]


def test_assistant_only_recommends_registered_tools_and_us_examples():
    schemas = tools.build_tool_schemas()
    text = prompt.build_system_prompt(None)
    registered = {s["function"]["name"] for s in schemas}
    mentioned = set(re.findall(r"\b(?:get|list|run)_[a-z_]+\b", text))
    assert mentioned <= registered
    visible = json.dumps(schemas + tools.QUICK_SUGGESTS, ensure_ascii=False)
    for token in ("涨停", "连板", "情绪雷达", "MCD", "上证", "get_financials"):
        assert token not in visible
    assert "美股" in visible and "USD" in visible
    assert "缺失" in text and "无午休" in text
