"""来源扩容、缓存救回与可计算因子抽取的回归测试。"""
from __future__ import annotations

import json

from app.services import factor_intake as fi

NEW_SOURCE_IDS = {"aligrithm", "alphanume", "quantinsti", "priceactionlab", "financialhacker", "nberap"}


def test_default_watch_list_has_verified_expansion_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "_public_http_url", lambda url: url)
    sources = fi.load_sources(tmp_path)
    assert {s["id"] for s in sources} >= NEW_SOURCE_IDS
    assert len(sources) == 24
    assert sum(bool(s["enabled"]) for s in sources) == 23


def test_source_migration_preserves_user_switches_and_custom_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "_public_http_url", lambda url: url)
    path = tmp_path / "research" / "intake" / "sources.json"
    path.parent.mkdir(parents=True)
    existing = [
        {"id": "qstrat", "name": "用户关闭", "kind": "site", "channel": "RSS", "url": "https://quantifiedstrategies.substack.com/feed", "enabled": False, "builtin": True},
        {"id": "custom", "name": "自定义", "kind": "site", "channel": "RSS", "url": "https://example.com/feed", "enabled": True, "builtin": False},
    ]
    path.write_text(json.dumps(existing))
    sources = fi.load_sources(tmp_path)
    assert next(s for s in sources if s["id"] == "qstrat")["name"] == "用户关闭"
    assert not next(s for s in sources if s["id"] == "qstrat")["enabled"]
    assert sum(s["id"] == "custom" for s in sources) == 1
    assert {s["id"] for s in sources} >= NEW_SOURCE_IDS
    assert len({s["id"] for s in sources}) == len(sources)


def test_check_items_keeps_valid_factor_and_records_failed_sibling():
    item = {"title": "CLV", "body": "F_t uses weighted CLV. Entry rank uses the same formula."}
    raw = [
        {"name_zh": "加权CLV", "quote": "F_t uses weighted CLV", "dsl": "ts_sum(log(1 + 1e-12) * close, 20)", "direction": "high"},
        {"name_zh": "坏规则", "quote": "Entry rank uses the same formula", "dsl": "unknown_fn(close)", "direction": "high"},
    ]
    out = fi._check_items(item, raw, set())
    assert [f["name"] for f in out] == ["加权CLV"]
    rejected = item["rejected"]
    assert isinstance(rejected, list)
    first = rejected[0]
    assert isinstance(first, dict)
    assert first.get("name") == "坏规则"
    assert first.get("compiler_code") == "E002"


def test_retry_revalidates_cached_model_result_without_another_model_call(tmp_path):
    raw = json.dumps({"summary_zh": "加权CLV", "items": [
        {"name_zh": "加权CLV", "quote": "F_t uses weighted CLV", "dsl": "ts_sum(log(1 + 1e-12) * close, 20)", "direction": "high"}
    ]})
    item_id = "cached"
    items = {item_id: {
        "id": item_id, "source": "manual", "source_name": "投递", "title": "CLV", "body": "F_t uses weighted CLV",
        "fetched_at": "2026-11-05T00:00:00+00:00",
        "stage": "archived", "failure": "dsl_compile", "reason": "公式编译失败 E014",
        "attempts": 4, "retry_count": 3, "last_model_text": raw,
    }}
    fi._save_items(tmp_path, items)
    out = fi.retry(tmp_path, item_id)
    assert out["stage"] == "to_screen" and out["recovered"]
    assert out["retry_count"] == 3
    saved = fi.load_items(tmp_path)[item_id]
    assert saved["formulas"][0]["name"] == "加权CLV"
    assert saved["last_model_text"] == raw
    assert saved["attempts"] == 4


def test_prompt_teaches_rsi_er_sma_and_preserves_stock_leg_with_spy_limit():
    assert "ts_rsi(close, 2)" in fi._EXTRACT_PROMPT
    assert "ER(" in fi._EXTRACT_PROMPT
    assert "ts_mean(close, 20)" in fi._EXTRACT_PROMPT
    assert "跨标的" in fi._EXTRACT_PROMPT
    assert "不要因为一个闸门" in fi._EXTRACT_PROMPT
