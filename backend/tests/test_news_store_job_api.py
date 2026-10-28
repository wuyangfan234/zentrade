"""新闻读取层/调度/推送/API/AI 分析注入。"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import news_ingest as ni
from app.services import news_job
from app.services import news_store as ns

NOW = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)


def _row(i, sym="LITE", *, hours_ago=2, tag=None, mention="headline", zh=True, extra_syms=(), base=None):
    t = ((base or NOW) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = {"id": f"alpaca:{i}", "headline": f"Headline {i}", "summary": f"Summary {i}", "created_at": t,
         "symbols": sorted({sym, *extra_syms}), "mentions": {sym: mention}, "sources": ["alpaca"],
         "refs": [{"source": "alpaca", "source_id": str(i), "url": f"https://x/{i}"}], "scope": "focused"}
    if zh:
        r["zh_headline"] = f"中文标题 {i}"
        r["zh_summary"] = f"中文摘要 {i}"
    if tag is not None:  # tag={} = 用默认打标 (primary/regulatory/neutral/low)
        r["tags"] = {sym: {"relevance": "primary", "event_type": "regulatory", "direction": "neutral",
                           "importance": "low", "rehash": False, **tag}}
        r["tagged_by"] = {"mode": "llm", "prompt_version": "news-tag-v1"}
    return r


def _save(tmp_path, rows):
    ni.save_shard(tmp_path, NOW, {r["id"]: r for r in rows})


def test_view_prefers_chinese_and_marks_untranslated(tmp_path):
    _save(tmp_path, [_row(1, tag={}), _row(2, zh=False)])
    items = ns.for_symbol(tmp_path, "LITE", now=NOW)
    by = {v["id"]: v for v in items}
    assert by["alpaca:1"]["headline"] == "中文标题 1" and by["alpaca:1"]["translated"]
    assert by["alpaca:1"]["event_label"] == "监管/调查" and by["alpaca:1"]["sources"] == ["Benzinga"]
    assert by["alpaca:2"]["headline"] == "Headline 2" and not by["alpaca:2"]["translated"] and not by["alpaca:2"]["tagged"]
    assert by["alpaca:1"]["age_hours"] == 2.0


def test_for_symbol_only_returns_events_that_mention_the_symbol(tmp_path):
    other = _row(3, sym="NVDA")
    other["symbols"] = ["LITE", "NVDA"]  # 来源标签含 LITE, 但只点名了 NVDA
    _save(tmp_path, [_row(1), other])
    assert [v["id"] for v in ns.for_symbol(tmp_path, "LITE", now=NOW)] == ["alpaca:1"]


def test_main_only_excludes_passing_and_rehash(tmp_path):
    _save(tmp_path, [_row(1, tag={}), _row(2, tag={"relevance": "passing"}), _row(3, tag={"rehash": True}),
                     _row(4, mention="summary"), _row(5, mention="headline")])
    ids = {v["id"] for v in ns.for_symbol(tmp_path, "LITE", now=NOW, main_only=True)}
    assert ids == {"alpaca:1", "alpaca:5"}  # 未打标时只认标题点名


def test_window_respects_days(tmp_path):
    _save(tmp_path, [_row(1, hours_ago=2), _row(2, hours_ago=24 * 8)])
    assert [v["id"] for v in ns.for_symbol(tmp_path, "LITE", days=7, now=NOW)] == ["alpaca:1"]


def test_summary_counts_and_flags(tmp_path):
    _save(tmp_path, [_row(1, tag={"importance": "high", "direction": "bearish"}, hours_ago=3),
                     _row(2, tag={"importance": "medium", "direction": "bullish"}, hours_ago=30),
                     _row(3, tag={"importance": "high", "direction": "bullish"}, hours_ago=60),
                     _row(4, tag={"relevance": "passing", "importance": "high", "direction": "bearish"})])
    s = ns.summarize(ns.for_symbol(tmp_path, "LITE", now=NOW))
    assert s["count_24h"] == 1
    assert s["high_bearish_48h"] is True and s["high_bullish_48h"] is False  # 60h 前的利好已出窗; passing 不算
    assert [v["id"] for v in s["top"]] == ["alpaca:1", "alpaca:2"]


def test_summary_tone_counts_48h_high_medium_primary_only(tmp_path):
    """0.16.0 机会卡「利空 N / 利多 N」: 48h 内、重要度高/中、主角 (非顺带、非旧闻重炒) 按方向计数; 中性/低重要度不计。"""
    _save(tmp_path, [_row(1, tag={"importance": "high", "direction": "bearish"}, hours_ago=3),
                     _row(2, tag={"importance": "medium", "direction": "bearish"}, hours_ago=40),
                     _row(3, tag={"importance": "medium", "direction": "bullish"}, hours_ago=10),
                     _row(4, tag={"importance": "low", "direction": "bearish"}, hours_ago=1),        # 低重要度
                     _row(5, tag={"importance": "high", "direction": "bearish"}, hours_ago=50),      # 出 48h 窗
                     _row(6, tag={"importance": "high", "direction": "bullish", "relevance": "passing"}),
                     _row(7, tag={"importance": "high", "direction": "bullish", "rehash": True}),
                     _row(8, tag={"importance": "high", "direction": "neutral"}),
                     _row(9, hours_ago=1)])                                                          # 未打标
    t = ns.summarize(ns.for_symbol(tmp_path, "LITE", now=NOW))["tone"]
    assert (t["bearish"], t["bullish"]) == (2, 1)
    assert t["bearish_heads"] == ["中文标题 1", "中文标题 2"] and t["bullish_heads"] == ["中文标题 3"]
    assert ns.summarize([])["tone"] == {"bearish": 0, "bullish": 0, "bearish_heads": [], "bullish_heads": []}


def test_universe_unions_watchlist_positions_and_live_cards(monkeypatch):
    from app.services import watchlist
    from app.opportunity.card import CardState
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": "lite"}, {"symbol": "NVDA"}])
    loop = SimpleNamespace(
        ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="SSL")]),
        opp_svc=SimpleNamespace(all_cards=lambda d: [SimpleNamespace(symbol="TH", state=CardState.WATCH),
                                                     SimpleNamespace(symbol="OLD", state=CardState.EXPIRED)]))
    assert news_job.universe(loop) == ["LITE", "NVDA", "SSL", "TH"]
    assert news_job.held_symbols(loop) == {"SSL"}


def test_universe_survives_failing_sources(monkeypatch):
    from app.services import watchlist

    def boom():
        raise RuntimeError("disk")
    monkeypatch.setattr(watchlist, "list_symbols", boom)
    loop = SimpleNamespace(ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="SSL")]), opp_svc=None)
    assert news_job.universe(loop) == ["SSL"]


def test_notify_holdings_only_pushes_held_high_bearish_primary(tmp_path, monkeypatch):
    sent = []
    import app.services.notify_opportunity as no
    monkeypatch.setattr(no, "_enqueue", lambda d, key, payload: sent.append((key, payload)) or True)
    _save(tmp_path, [
        _row(1, sym="SSL", tag={"importance": "high", "direction": "bearish"}),            # 推
        _row(2, sym="SSL", tag={"importance": "medium", "direction": "bearish"}),          # 重要度不够
        _row(3, sym="SSL", tag={"importance": "high", "direction": "bullish"}),            # 利好不推
        _row(4, sym="SSL", tag={"importance": "high", "direction": "bearish", "rehash": True}),  # 旧闻
        _row(5, sym="NVDA", tag={"importance": "high", "direction": "bearish"}),           # 非持仓
        _row(6, sym="SSL", zh=True),                                                       # 未打标
        _row(7, sym="SSL", tag={"importance": "high", "direction": "bearish"}, hours_ago=60),  # 过期
    ])
    n = news_job.notify_holdings(tmp_path, {"SSL"}, now=NOW)
    assert n == 1 and sent[0][0] == "news:alpaca:1:SSL"
    body = sent[0][1]["body"]
    assert "中文标题 1" in body and "AI 判读" in body and "美东" in body
    assert "买入" not in body and "卖出" not in body


def test_notify_holdings_cools_down_per_symbol_and_pushes_earliest_of_duplicates(tmp_path, monkeypatch):
    sent = []
    import app.services.notify_opportunity as no
    monkeypatch.setattr(no, "_enqueue", lambda d, key, payload: sent.append((key, payload)) or True)
    bad = {"importance": "high", "direction": "bearish"}
    _save(tmp_path, [_row(1, sym="SSL", tag=bad, hours_ago=2), _row(2, sym="SSL", tag=bad, hours_ago=6),
                     _row(3, sym="SSL", tag=bad, hours_ago=4)])   # 同一事件三家媒体
    assert news_job.notify_holdings(tmp_path, {"SSL"}, now=NOW) == 1
    assert sent[0][0] == "news:alpaca:2:SSL"                      # 最早一条
    assert "同类报道共 3 条" in sent[0][1]["body"]
    # 12 小时内不再推; 过了冷却期才可再推
    assert news_job.notify_holdings(tmp_path, {"SSL"}, now=NOW + timedelta(hours=11)) == 0
    assert news_job.notify_holdings(tmp_path, {"SSL"}, now=NOW + timedelta(hours=13)) == 1


def test_tick_runs_stages_independently_and_writes_state(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(news_job, "_last", {"fast": 0.0, "sec": 0.0})
    monkeypatch.setattr(news_job, "_sec_cursor", {"i": 0})
    monkeypatch.setattr(news_job, "universe", lambda loop=None, core_only=False: ["LITE"] if core_only else ["LITE", "WAT"])

    def fake_ingest(data_dir, symbols, **kw):
        calls.append((list(symbols), sorted(kw["sources"]), kw.get("finnhub_symbols")))
        return {"raw": 3, "clusters": 2, "tagged": 2, "kept": 1, "added": 1, "errors": []}
    monkeypatch.setattr(ni, "ingest", fake_ingest)
    from app.services import news_translate
    tr_calls = []

    def fake_tr(d, **kw):
        tr_calls.append(kw.get("priority"))
        return {"llm": 1, "fallback": 0, "failed_batches": 0, "pending_after": 0}
    monkeypatch.setattr(news_translate, "translate_pending_sync", fake_tr)
    res = news_job.tick(tmp_path, None, None, now=NOW, force=True)
    # 快源 (Alpaca 全量 + Finnhub 只核心集) 与 SEC 分两次独立调用
    assert calls == [(["LITE", "WAT"], ["alpaca", "finnhub"], ["LITE"]), (["LITE", "WAT"], ["sec"], None)]
    assert tr_calls == [{"LITE"}]
    assert res["translate"]["llm"] == 1
    st = json.loads((tmp_path / "news" / "state.json").read_text())
    assert st["universe"] == 2 and st["core"] == 1 and st["ingest"] == {"raw": 6, "kept": 2, "added": 2}
    # 采集未到点: 只跑翻译; 翻译也无事可做 → 返回 None
    calls.clear()
    monkeypatch.setattr(news_translate, "translate_pending_sync",
                        lambda d, **kw: {"llm": 0, "fallback": 0, "failed_batches": 0, "pending_after": 0})
    assert news_job.tick(tmp_path, None, None, now=NOW) is None and calls == []


def test_sec_stage_rotates_through_large_universe(tmp_path, monkeypatch):
    seen = []
    big = [f"S{i:03d}" for i in range(100)]
    monkeypatch.setattr(news_job, "_last", {"fast": 1e18, "sec": 0.0})  # 只让 SEC 到点
    monkeypatch.setattr(news_job, "_sec_cursor", {"i": 0})
    monkeypatch.setattr(news_job, "universe", lambda loop=None, core_only=False: ["S000"] if core_only else big)
    monkeypatch.setattr(ni, "ingest", lambda d, symbols, **kw: seen.append(list(symbols)) or
                        {"raw": 0, "kept": 0, "added": 0, "errors": []})
    from app.services import news_translate
    monkeypatch.setattr(news_translate, "translate_pending_sync",
                        lambda d, **kw: {"llm": 0, "fallback": 0, "failed_batches": 0, "pending_after": 0})
    news_job.tick(tmp_path, None, None, now=NOW)
    monkeypatch.setattr(news_job, "_last", {"fast": 1e18, "sec": 0.0})
    news_job.tick(tmp_path, None, None, now=NOW)
    assert len(seen) == 2 and all(len(b) == news_job.SEC_PER_ROUND for b in seen)
    assert all("S000" in b for b in seen)                    # 核心股票每轮都查
    assert set(seen[0]) - {"S000"} != set(seen[1]) - {"S000"}  # 其余轮转


def test_universe_cap_never_drops_core_and_keeps_freshest_watch_cards(monkeypatch):
    """审计 2026-10-26 H-1: 观察卡累积超上限时, 自选/持仓/可入场卡不能因字母序被裁掉。"""
    from datetime import date as _d

    from app.opportunity.card import CardState
    from app.services import watchlist
    monkeypatch.setattr(news_job, "UNIVERSE_MAX", 10)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": "NVDA"}, {"symbol": "TSLA"}, {"symbol": "WDC"}])
    old = [SimpleNamespace(symbol=f"A{i:02d}", state=CardState.WATCH, created_date=_d(2026, 10, 1)) for i in range(20)]
    new = [SimpleNamespace(symbol=f"Z{i:02d}", state=CardState.WATCH, created_date=_d(2026, 10, 23)) for i in range(5)]
    live = [SimpleNamespace(symbol="XOM", state=CardState.OPEN, created_date=_d(2026, 10, 1))]
    loop = SimpleNamespace(ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="IREN")]),
                           opp_svc=SimpleNamespace(all_cards=lambda d: old + new + live))
    u = news_job.universe(loop)
    assert len(u) == 10
    assert {"NVDA", "TSLA", "WDC", "IREN", "XOM"} <= set(u)            # 核心集全保留
    assert {f"Z{i:02d}" for i in range(5)} <= set(u)                   # 剩余名额给最新观察卡
    assert not any(s.startswith("A") for s in u)
    assert news_job.universe(loop, core_only=True) == ["IREN", "NVDA", "TSLA", "WDC", "XOM"]


def test_sec_batch_rotates_core_when_core_exceeds_round(monkeypatch):
    """审计 2026-10-26 H-1: 核心集 44 > 每轮 40 时旧实现 take=0, 末尾核心票永远查不到 SEC。"""
    monkeypatch.setattr(news_job, "_sec_cursor", {"i": 0, "core": 0})
    core = [f"C{i:02d}" for i in range(44)]
    rest = [f"W{i:03d}" for i in range(100)]
    syms = sorted(core + rest)
    seen: set[str] = set()
    for _ in range(4):
        b = news_job.sec_batch(syms, core)
        assert len(b) <= news_job.SEC_PER_ROUND
        assert any(x.startswith("W") for x in b)                        # 观察票也有名额
        seen |= set(b)
    assert set(core) <= seen


def test_sec_batch_full_core_each_round_when_within_cap(monkeypatch):
    """复审 M-a: 核心 <= 40 时与旧行为一致, 每轮全查核心; 核心超额时持仓每轮必查。"""
    monkeypatch.setattr(news_job, "_sec_cursor", {"i": 0, "core": 0})
    core = [f"C{i:02d}" for i in range(35)]
    syms = sorted(core + [f"W{i:03d}" for i in range(100)])
    for _ in range(3):
        b = news_job.sec_batch(syms, core)
        assert set(core) <= set(b) and len(b) == news_job.SEC_PER_ROUND
    big = [f"C{i:02d}" for i in range(60)]
    syms2 = sorted(big + [f"W{i:03d}" for i in range(100)])
    for _ in range(4):
        assert {"C59", "C58"} <= set(news_job.sec_batch(syms2, big, held={"C59", "C58"}))


def test_stock_analysis_prompt_injects_news_and_keeps_fallback_without(tmp_path):
    from app.services.stock_analyzer import _SYSTEM_PROMPT, _build_user_prompt
    assert "近 7 日新闻" in _SYSTEM_PROMPT and "不得引用列表之外" in _SYSTEM_PROMPT
    _save(tmp_path, [_row(1, tag={"importance": "high", "direction": "bearish"})])
    news = ns.for_symbol(tmp_path, "LITE", now=NOW)
    p = _build_user_prompt([], {}, {}, 10.0, "LITE", "", news=news)
    assert "近 7 日新闻" in p and "中文标题 1" in p and "监管/调查/偏利空/重要度高" in p
    p0 = _build_user_prompt([], {}, {}, 10.0, "LITE", "")
    assert "近 7 日新闻" not in p0


def test_news_api_routes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import news as news_api
    real_now = datetime.now(timezone.utc)
    row = _row(1, tag={"importance": "high", "direction": "bearish"}, hours_ago=1, base=real_now)
    ni.save_shard(tmp_path, real_now - timedelta(hours=1), {row["id"]: row})
    app = FastAPI()
    app.include_router(news_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    c = TestClient(app)
    body = c.get("/api/news/stock", params={"symbol": "lite"}).json()
    assert body["symbol"] == "LITE" and len(body["items"]) == 1 and "AI 判读" in body["disclaimer"]
    s = c.get("/api/news/summary", params={"symbols": "LITE,NVDA"}).json()["summaries"]
    assert s["LITE"]["count_24h"] == 1 and s["NVDA"]["count_24h"] == 0
    assert c.get("/api/news/stock", params={"symbol": ""}).status_code == 422
    assert "events_24h" in c.get("/api/news/status").json()


def test_universe_core_excludes_watch_cards_but_full_keeps_them(monkeypatch):
    """全量 (Alpaca/SEC) 覆盖观察卡 = 已拍板的机会卡候选; 核心集 (Finnhub 逐股限速) 不含观察卡。"""
    from app.services import watchlist
    from app.opportunity.card import CardState
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": "LITE"}])
    loop = SimpleNamespace(
        ledger=SimpleNamespace(open_positions=[SimpleNamespace(symbol="SSL")]),
        opp_svc=SimpleNamespace(all_cards=lambda d: [SimpleNamespace(symbol="TH", state=CardState.OPEN),
                                                     SimpleNamespace(symbol="WAT", state=CardState.WATCH),
                                                     SimpleNamespace(symbol="OLD", state=CardState.EXPIRED)]))
    assert news_job.universe(loop) == ["LITE", "SSL", "TH", "WAT"]
    assert news_job.universe(loop, core_only=True) == ["LITE", "SSL", "TH"]


def test_finnhub_only_queries_core_symbols_and_stops_on_429(tmp_path, monkeypatch):
    import urllib.error
    monkeypatch.setattr(ni, "alpaca_credentials", lambda: None)
    monkeypatch.setattr(ni, "finnhub_token", lambda: "t")
    asked = []

    def finnhub(url):
        sym = url.split("symbol=")[1].split("&")[0]
        asked.append(sym)
        if sym == "BBB":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return []
    r = ni.ingest(tmp_path, ["AAA", "BBB", "CCC", "WAT"], now=NOW, sources={"finnhub"},
                  finnhub_symbols=["AAA", "BBB", "CCC"], finnhub_get=finnhub)
    assert asked == ["AAA", "BBB"]                       # WAT 不在核心集; 429 后停止本轮
    assert any("429" in e for e in r["errors"]) and any("rate limited" in e for e in r["errors"])


def test_translate_prioritizes_core_symbols(tmp_path):
    import asyncio
    from app.services import news_translate as nt
    rows = [_row(i, sym=("CORE" if i == 9 else "WAT"), zh=False, hours_ago=1 if i != 9 else 30) for i in range(1, 10)]
    for r in rows:
        r.pop("tags", None)
    _save(tmp_path, rows)
    seen = []

    async def ai(messages, **kw):
        lines = [json.loads(x) for x in messages[-1]["content"].splitlines()[1:]]
        seen.append([x["待判股票"] for x in lines])
        return json.dumps([{"i": x["i"], "zh_headline": "x", "tags": {}} for x in lines])
    old = nt.BATCH_SIZE
    nt.BATCH_SIZE = 3
    try:
        asyncio.run(nt.translate_pending(tmp_path, now=NOW, ai_call=ai, max_batches=1, priority={"CORE"}))
    finally:
        nt.BATCH_SIZE = old
    assert seen[0][0] == ["CORE"]   # 更旧但属核心集的新闻排在第一批最前
