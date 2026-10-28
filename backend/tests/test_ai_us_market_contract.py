"""美股 AI 运行时提示词与助手市场工具，不外呼模型/行情。"""
from datetime import date
import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.custom.assistant import prompt, tools
from app.services import market_overview_builder as overview
from app.strategy.ai_generator import AIStrategyGenerator, GUIDE_PATH, _SYSTEM_PREFIX
from app.strategy.prompt_builder import build_step2


@pytest.mark.parametrize("text", [_SYSTEM_PREFIX + AIStrategyGenerator()._get_guide(),
                                  build_step2("META = {}", "调整策略")],
                         ids=["generate", "modify"])
def test_strategy_runtime_prompts_are_us_only(text):
    assert "美股" in text
    assert "美元" in text
    assert "财务" in text and "缺失" in text
    for forbidden in ("你是A股", "signal_limit_", "signal_broken_limit_up", "consecutive_limit_", '"exclude_st": True'):
        assert forbidden not in text


def test_builder_step1_reference_no_longer_teaches_price_limit_signals():
    text = (GUIDE_PATH.parent / "strategy-builder-step1.md").read_text()
    assert "美股" in text
    assert "signal_limit_" not in text
    assert "上次涨停价" not in text


def test_assistant_prompt_does_not_invent_financial_tool_or_limit_mechanism():
    text = prompt.build_system_prompt(None)
    assert "美元" in text and "美东" in text
    assert "缺失" in text and "财务" in text
    assert "get_financials" not in text  # 注册表中没有这个工具
    assert "触及涨跌停直接写" not in text
    assert "元 →" not in text


class FakeScreener:
    frame = pl.DataFrame()
    def __init__(self, repo):
        pass
    def latest_date(self):
        return date(2026, 9, 25)
    def _load_enriched_for_date(self, day):
        return self.frame


def setup_overview(monkeypatch, tmp_path, frame):
    monkeypatch.setattr(overview, "ScreenerService", FakeScreener)
    monkeypatch.setattr(FakeScreener, "frame", frame)
    monkeypatch.setattr(overview, "_index_quotes", lambda *a: [
        {"symbol": "SPY", "last_price": 500, "change_pct": 1.2}])
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))


def test_overview_uses_us_breadth_and_does_not_read_depth_or_report_limit_stats(monkeypatch, tmp_path):
    frame = pl.DataFrame({"symbol": ["AAPL", "MSFT", "NVDA"], "close": [110, 100, 150],
                          "change_pct": [0.1, -0.02, None], "volume": [1000, 2000, 500],
                          "amount": [110000, 200000, 75000], "ma20": [100, 110, 140],
                          "signal_limit_up": [True, False, False], "consecutive_limit_ups": [4, 0, 0]})
    repo = setup_overview(monkeypatch, tmp_path, frame)
    class PoisonDepth:
        def get_sealed_map(self, *args, **kwargs):
            pytest.fail("美股总览不应查询封板")
    result = overview.build_market_overview(repo, depth_service=PoisonDepth())
    assert result["market"] == "US"
    assert result["currency"] == "USD"
    assert result["breadth"]["up"] == 1
    assert result["breadth"]["down"] == 1
    assert result["breadth"]["flat"] == 0
    assert result["breadth"]["unknown"] == 1
    assert result["breadth"]["up_pct"] == 50
    assert result["financials"]["status"] == "unavailable"
    assert "缺失" in result["financials"]["note"]
    for field in ("limit", "boards", "emotion", "radar"):
        assert field not in result
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert "其他板" not in text
    assert "涨停" not in text


def test_empty_overview_marks_missing_not_fabricated_neutral_values(monkeypatch, tmp_path):
    repo = setup_overview(monkeypatch, tmp_path, pl.DataFrame())
    monkeypatch.setattr(FakeScreener, "latest_date", lambda self: None)
    result = overview.build_market_overview(repo)
    assert result["data_status"] == "unavailable"
    assert result["amount"]["total"] is None
    assert result["breadth"]["avg_pct"] is None
    assert "emotion" not in result


def test_assistant_overview_summary_has_us_wording():
    summary = tools.summarize_tool_result("get_market_overview", {"ok": True, "result": {"breadth": {"up": 2, "down": 1}}})
    assert "美股" in summary
    assert "涨停" not in summary
