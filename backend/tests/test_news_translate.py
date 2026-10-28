"""新闻翻译打标: 宽松解析、字段白名单校验、不许编造摘要、模型失败走 Google 兜底、增量重试。"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.services import news_ingest as ni
from app.services import news_translate as nt

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)


def _ev(i, headline, summary="", mentions=None, created="2026-10-02T10:00:00Z"):
    mentions = mentions or {"LITE": "headline"}
    return {"id": f"alpaca:{i}", "headline": headline, "summary": summary, "created_at": created,
            "symbols": sorted(mentions), "mentions": mentions, "sources": ["alpaca"], "refs": [], "scope": "focused"}


def test_parse_accepts_fenced_json_and_validates_fields():
    batch = [_ev(0, "Lumentum rips higher", "Optical stocks rally."), _ev(1, "Old news recap")]
    text = """```json
[{"i":0,"zh_headline":"Lumentum 股价大涨","zh_summary":"光模块股集体上涨。",
  "tags":{"LITE":{"relevance":"primary","event_type":"regulatory","direction":"bullish","importance":"high","rehash":false},
          "NVDA":{"relevance":"primary","event_type":"other","direction":"bullish","importance":"high"}}},
 {"i":1,"zh_headline":"旧闻回顾","zh_summary":"模型编的摘要",
  "tags":{"LITE":{"relevance":"passing","event_type":"made_up","direction":"up","importance":"huge","rehash":true}}},
 {"i":7,"zh_headline":"越界序号"}]
```"""
    out = nt.parse_llm_output(text, batch)
    assert set(out) == {0, 1}
    assert out[0]["tags"] == {"LITE": {"relevance": "primary", "event_type": "regulatory", "direction": "bullish",
                                       "importance": "high", "rehash": False}}  # NVDA 不在待判集合, 丢弃
    # 非法枚举值回落到保守默认, 不是整条丢掉
    assert out[1]["tags"]["LITE"] == {"relevance": "passing", "event_type": "other", "direction": "neutral",
                                      "importance": "low", "rehash": True}
    assert out[1]["zh_summary"] == ""  # 原文没有摘要, 模型补写的一律不收


def test_parse_rejects_output_without_array():
    with pytest.raises(ValueError):
        nt.parse_llm_output("抱歉，我无法完成", [_ev(0, "h")])


def test_parse_drops_tag_with_invalid_relevance():
    out = nt.parse_llm_output('[{"i":0,"zh_headline":"x","tags":{"LITE":{"relevance":"main"}}}]', [_ev(0, "h")])
    assert out[0]["tags"] == {}


def _write(tmp_path, events):
    ni.save_shard(tmp_path, NOW, {e["id"]: e for e in events})


def test_translate_pending_writes_llm_fields_and_uses_news_task(tmp_path):
    _write(tmp_path, [_ev(0, "Lumentum rips higher"), _ev(1, "Rocket Lab wins launch deal", mentions={"RKLB": "headline"})])
    seen = {}

    async def ai(messages, **kw):
        seen.update(kw)
        prompt = messages[-1]["content"]
        rows = [json.loads(line) for line in prompt.splitlines()[1:]]
        return json.dumps([{"i": r["i"], "zh_headline": f"译:{r['headline']}", "zh_summary": "",
                            "tags": {s: {"relevance": "primary", "event_type": "contract", "direction": "bullish",
                                         "importance": "medium", "rehash": False} for s in r["待判股票"]}} for r in rows])
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai))
    assert stats["llm"] == 2 and stats["pending_after"] == 0
    assert seen["task"] == "news_translate"
    shard = ni.load_shard(tmp_path, NOW)
    assert shard["alpaca:1"]["zh_headline"] == "译:Rocket Lab wins launch deal"
    assert shard["alpaca:1"]["tags"]["RKLB"]["event_type"] == "contract"
    assert shard["alpaca:1"]["tagged_by"]["mode"] == "llm"
    assert shard["alpaca:1"]["tagged_by"]["prompt_version"] == nt.PROMPT_VERSION

    # 第二轮: 已完成的事件不再送模型
    calls = []

    async def ai2(messages, **kw):
        calls.append(1)
        return "[]"
    asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai2))
    assert calls == []


def test_model_failure_falls_back_to_google_title_only_and_retries_next_run(tmp_path):
    _write(tmp_path, [_ev(0, "Lumentum rips higher")])

    async def down(messages, **kw):
        raise RuntimeError("kimi 502")
    google = lambda url: [[["Lumentum 大涨", "Lumentum rips higher"]]]  # noqa: E731
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=down, google_get=google))
    assert stats == {"llm": 0, "fallback": 1, "failed_batches": 1, "pending_after": 1}
    row = ni.load_shard(tmp_path, NOW)["alpaca:0"]
    assert row["zh_headline"] == "Lumentum 大涨" and row["tags"] == {}  # 兜底只翻译不打标
    assert row["tagged_by"]["mode"] == "fallback"

    # 下一轮模型恢复: 兜底记录会被模型结果覆盖 (补上打标)
    async def ok(messages, **kw):
        return ('[{"i":0,"zh_headline":"Lumentum 股价大涨","zh_summary":"","tags":{"LITE":'
                '{"relevance":"primary","event_type":"regulatory","direction":"bullish","importance":"high","rehash":false}}}]')
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ok))
    row = ni.load_shard(tmp_path, NOW)["alpaca:0"]
    assert stats["llm"] == 1 and row["tagged_by"]["mode"] == "llm" and row["tags"]["LITE"]["importance"] == "high"


def test_google_translate_failure_leaves_event_untranslated_not_fabricated(tmp_path):
    _write(tmp_path, [_ev(0, "Lumentum rips higher")])

    async def down(messages, **kw):
        raise RuntimeError("down")

    def google_down(url):
        raise OSError("no network")
    asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=down, google_get=google_down))
    row = ni.load_shard(tmp_path, NOW)["alpaca:0"]
    assert "zh_headline" not in row and "tagged_by" not in row


def test_batches_are_capped_per_run(tmp_path):
    _write(tmp_path, [_ev(i, f"Headline {i}", created=f"2026-10-02T{10 + i % 10:02d}:00:00Z") for i in range(45)])
    calls = []

    async def ai(messages, **kw):
        calls.append(1)
        rows = [json.loads(line) for line in messages[-1]["content"].splitlines()[1:]]
        return json.dumps([{"i": r["i"], "zh_headline": "x", "tags": {}} for r in rows])
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai, max_batches=2))
    assert len(calls) == 2 and stats["llm"] == 40 and stats["pending_after"] == 5



def test_rule_tagged_sec_events_are_never_sent_to_the_model(tmp_path):
    sec = _ev(0, "NVDA SEC 8-K filing (items 2.02)", mentions={"NVDA": "headline"})
    sec.update({"zh_headline": "NVDA 8-K 公告: 披露经营业绩", "tags": {"NVDA": {"relevance": "primary"}},
                "tagged_by": {"mode": "rule", "prompt_version": "sec-items-v1"}})
    _write(tmp_path, [sec])
    calls = []

    async def ai(messages, **kw):
        calls.append(1)
        return "[]"
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai))
    assert calls == [] and stats["pending_after"] == 0


def test_time_budget_stops_between_batches_and_leaves_rest_pending(tmp_path, monkeypatch):
    _write(tmp_path, [_ev(i, f"Headline {i}") for i in range(30)])
    import time as _t
    clock = {"t": 1000.0}
    monkeypatch.setattr(_t, "monotonic", lambda: clock["t"])

    async def slow_ai(messages, **kw):
        clock["t"] += 200.0  # 一批耗时超过预算
        rows = [json.loads(line) for line in messages[-1]["content"].splitlines()[1:]]
        return json.dumps([{"i": r["i"], "zh_headline": "x", "tags": {}} for r in rows])
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=slow_ai, max_batches=3, time_budget_s=150))
    assert stats["llm"] == 20 and stats["pending_after"] == 10   # 只跑了 1 批, 剩下的留到下一轮


def test_budget_is_checked_before_a_batch_using_expected_duration(tmp_path, monkeypatch):
    """首批之后: 已用 60 秒 + 预计 70 秒 > 120 秒预算 → 不开第二批 (而不是跑完才发现超时)。"""
    _write(tmp_path, [_ev(i, f"Headline {i}") for i in range(30)])
    import time as _t
    clock = {"t": 1000.0}
    monkeypatch.setattr(_t, "monotonic", lambda: clock["t"])

    async def ai(messages, **kw):
        clock["t"] += 60.0
        rows = [json.loads(line) for line in messages[-1]["content"].splitlines()[1:]]
        return json.dumps([{"i": r["i"], "zh_headline": "x", "tags": {}} for r in rows])
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai, max_batches=3, time_budget_s=120))
    assert stats["llm"] == 20


def test_sync_entry_reuses_one_event_loop_across_ticks(tmp_path, monkeypatch):
    """每轮复用同一个事件循环: 上一轮创建的 HTTP 客户端在活循环上关闭, 不再刷 'Event loop is closed'。"""
    _write(tmp_path, [])
    loops = []

    async def fake(data_dir, **kw):
        loops.append(asyncio.get_running_loop())
        return {"llm": 0, "fallback": 0, "failed_batches": 0, "pending_after": 0}
    monkeypatch.setattr(nt, "translate_pending", fake)
    nt.translate_pending_sync(tmp_path)
    nt.translate_pending_sync(tmp_path)
    assert len(loops) == 2 and loops[0] is loops[1] and not loops[0].is_closed()


# ── Google 兜底 (2026-10-07 性能审查): gtx 端点对 NAS 出口 IP 恒 429, 每批 20 条逐条打失败 (6h 1205 条告警);
#    改用 client=at 端点 (生产实测 6/6 → 200), 且同一轮内首个失败即熔断, 不再逐条空打。

def test_google_fallback_uses_working_endpoint():
    seen = []

    def google(url):
        seen.append(url)
        return [[["英伟达股价下跌", "Nvidia stock falls"]]]
    assert nt.google_translate("Nvidia stock falls", http_get=google) == "英伟达股价下跌"
    assert seen and "translate.google.com/translate_a/single?client=at&" in seen[0]
    assert "client=gtx" not in seen[0]


def test_google_fallback_circuit_breaks_after_first_failure_in_a_run(tmp_path):
    _write(tmp_path, [_ev(i, f"Headline {i}", created=f"2026-10-02T{10 + i % 10:02d}:00:00Z") for i in range(25)])

    async def down(messages, **kw):
        raise RuntimeError("kimi 403")
    calls = []

    def google_down(url):
        calls.append(url)
        raise OSError("HTTP 429")
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=down, google_get=google_down))
    assert len(calls) == 1  # 同一轮内首个失败后不再逐条重试 (原实现每条都打一次)
    assert stats["fallback"] == 0
    shard = ni.load_shard(tmp_path, NOW)
    assert all("zh_headline" not in row for row in shard.values())  # 失败不编造译文


def test_google_fallback_breaker_resets_next_run(tmp_path):
    _write(tmp_path, [_ev(0, "Lumentum rips higher")])

    async def down(messages, **kw):
        raise RuntimeError("kimi 403")

    def google_down(url):
        raise OSError("HTTP 429")
    asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=down, google_get=google_down))
    google_ok = lambda url: [[["Lumentum 大涨", "Lumentum rips higher"]]]  # noqa: E731
    stats = asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=down, google_get=google_ok))
    assert stats["fallback"] == 1  # 下一轮重新尝试兜底
