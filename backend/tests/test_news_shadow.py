"""新闻催化影子证据: 快照无前视、去重、按统一 7 日纪律回填结局、门槛判定、决策台展示且不可启用。"""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import news_ingest as ni
from app.services import news_shadow as nsh

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 2)


def _news(i, sym, *, hours_ago, direction="bearish", importance="high", relevance="primary", rehash=False):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": f"alpaca:{i}", "headline": f"h{i}", "summary": "", "created_at": t, "symbols": [sym],
            "mentions": {sym: "headline"}, "sources": ["alpaca"], "refs": [], "scope": "focused", "zh_headline": f"中{i}",
            "tags": {sym: {"relevance": relevance, "event_type": "dilution", "direction": direction,
                           "importance": importance, "rehash": rehash}},
            "tagged_by": {"mode": "llm", "prompt_version": "news-tag-v1"}}


def _cand(sym, opened=False):
    return {"symbol": sym, "entry_zone": (100.0, 101.0), "invalidation": 95.0, "target_zone": (110.0, 112.0),
            "risk_per_share": 5.5, "sources": ["trend_breakout"], "opened": opened}


def test_snapshot_flags_only_prior_high_main_news_and_dedupes(tmp_path):
    ni.save_shard(tmp_path, NOW, {r["id"]: r for r in [
        _news(1, "AAA", hours_ago=5),                                    # 命中利空
        _news(2, "BBB", hours_ago=5, direction="bullish"),               # 命中利好
        _news(3, "CCC", hours_ago=60),                                   # 超 48h
        _news(4, "DDD", hours_ago=5, importance="medium"),               # 重要度不够
        _news(5, "EEE", hours_ago=5, rehash=True),                       # 旧闻
        _news(6, "FFF", hours_ago=-3),                                   # 快照之后才发生 → 不得计入 (无前视)
    ]})
    n = nsh.record_snapshot(tmp_path, AS_OF, [_cand(s, opened=(s == "AAA")) for s in "AAA BBB CCC DDD EEE FFF".split()], now=NOW)
    assert n == 6
    rows = {r["symbol"]: r for r in nsh.load_rows(tmp_path)}
    assert rows["AAA"]["high_bearish"] and not rows["AAA"]["high_bullish"] and rows["AAA"]["opened"]
    assert rows["BBB"]["high_bullish"] and not rows["BBB"]["high_bearish"]
    for s in ("CCC", "DDD", "EEE", "FFF"):
        assert not rows[s]["high_bearish"] and not rows[s]["high_bullish"], s
    assert rows["AAA"]["news_ids"] == ["alpaca:1"]
    # 同日重复扫描不重复记账
    assert nsh.record_snapshot(tmp_path, AS_OF, [_cand("AAA")], now=NOW) == 0


def test_snapshot_failure_never_raises(tmp_path):
    assert nsh.record_snapshot(tmp_path, AS_OF, [{"symbol": "AAA"}], now=NOW) == 0  # 缺字段 → 吞掉, 返回 0


def _bars(sym, start: date, closes):
    rows = []
    d = start
    for c in closes:
        d += timedelta(days=1)
        rows.append({"symbol": sym, "date": d, "open": c, "high": c + 1, "low": c - 1, "close": c})
    return rows


def test_resolve_outcomes_uses_unified_7d_replay_after_14_days(tmp_path):
    nsh.record_snapshot(tmp_path, AS_OF, [_cand("AAA"), _cand("BBB")], now=NOW)
    # AAA 次日开在入场区并冲到目标 → closed 正 R; BBB 一路跌破失效点 → closed 负 R
    data = _bars("AAA", AS_OF, [100.5, 104, 108, 111, 112, 112, 112, 112, 112, 112, 112]) + \
           _bars("BBB", AS_OF, [100.5, 98, 96, 94, 93, 92, 91, 90, 90, 90, 90])
    repo = SimpleNamespace(get_daily_batch=lambda syms, s, e, columns=None: pl.DataFrame(data))
    assert nsh.resolve_outcomes(tmp_path, repo, today=AS_OF + timedelta(days=10)) == 0  # 未满 14 天不回填
    assert nsh.resolve_outcomes(tmp_path, repo, today=AS_OF + timedelta(days=15)) == 2
    rows = {r["symbol"]: r for r in nsh.load_rows(tmp_path)}
    assert rows["AAA"]["outcome"]["status"] == "closed" and rows["AAA"]["outcome"]["r"] > 0
    assert rows["BBB"]["outcome"]["status"] == "closed" and rows["BBB"]["outcome"]["r"] < 0
    assert nsh.resolve_outcomes(tmp_path, repo, today=AS_OF + timedelta(days=16)) == 0  # 已回填的不重复


def _write_resolved(tmp_path, n_with, n_without, r_with, r_without, months=("2026-10", "2026-11")):
    rows = []
    for k in range(n_with + n_without):
        flag = k < n_with
        m = months[k % len(months)]
        rows.append({"as_of": f"{m}-0{1 + k % 9}", "symbol": f"S{k}", "high_bearish": flag, "high_bullish": False,
                     "outcome": {"status": "closed", "r": (r_with if flag else r_without) + (k % 3) * 0.01}})
    nsh._save_rows(tmp_path, rows)


def test_evidence_stays_observe_below_sample_bar(tmp_path):
    _write_resolved(tmp_path, 10, 50, -0.8, 0.1)
    ev = nsh.evidence(tmp_path)
    assert ev["status"] == "observe" and not ev["bearish_veto"]["meets_bar"]
    assert "未到 30 例" in ev["verdict"]


def test_evidence_meets_bar_but_only_suggests_manual_approval(tmp_path):
    _write_resolved(tmp_path, 40, 60, -0.8, 0.1)
    ev = nsh.evidence(tmp_path)
    assert ev["bearish_veto"]["meets_bar"] and ev["bearish_veto"]["months_consistent"]
    assert ev["status"] == "observe" and "待你拍板" in ev["verdict"]


def test_desk_sources_lists_news_source_as_locked_observe_and_cannot_be_enabled(tmp_path):
    from fastapi import HTTPException
    from app.api import opportunities as opp_api
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=None)))
    body = opp_api.desk_sources(request)
    row = next(r for r in body["sources"] if r["id"] == "news_catalyst")
    assert row["status"] == "observe" and row["weight"] is None and row["manual"]["locked"]
    with pytest.raises(HTTPException):
        opp_api.desk_set_status("news_catalyst", opp_api.SourceStatusIn(status="enabled", note="x"), request)
