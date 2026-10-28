# ruff: noqa: RUF001
"""准入漏斗: 拦截落盘去重、按天汇总、回补成交单列 (2026-10-12)。"""
import json
from datetime import date, datetime

import pytest

from app.market_clock import us_clock
from app.services import entry_funnel as ef

ET = us_clock.tz
MON = date(2026, 10, 12)
# 0.26.0: 漏斗只统计机会板的卡 (卡 id 含 -book-swing-); 这里的卡都用机会板卡 id
C1, C2, C3 = ("opp-2026-10-09-AAPL-book-swing-1", "opp-2026-10-09-NVDA-book-swing-2", "opp-2026-10-09-MSFT-book-swing-3")


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    ef._SEEN.clear()


def at(h, m, s=0):
    return datetime(2026, 10, 12, h, m, s, tzinfo=ET)


def _events(tmp_path, rows):
    d = tmp_path / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


# ── 落盘去重 ──
def test_blockers_deduped_per_day_symbol_reason(tmp_path):
    b = [{"symbol": "AAPL", "reason": "earnings_window", "card_id": C1}, {"symbol": "*", "reason": "circuit_breaker_day_halt"}]
    assert ef.record_blockers(tmp_path, at(10, 0), b) == 2
    assert ef.record_blockers(tmp_path, at(10, 0, 15), b) == 0            # 同一轮重复, 不再写
    assert ef.record_blockers(tmp_path, at(10, 1), [{"symbol": "AAPL", "reason": "valuation_incomplete", "card_id": C1}]) == 1
    rows = (tmp_path / "funnel" / "2026-10-12.jsonl").read_text().strip().splitlines()
    assert len(rows) == 3


def test_dedupe_survives_restart(tmp_path):
    ef.record_blockers(tmp_path, at(10, 0), [{"symbol": "NVDA", "reason": "earnings_window", "card_id": C2}])
    ef._SEEN.clear()                                                       # 模拟重启
    assert ef.record_blockers(tmp_path, at(10, 30), [{"symbol": "NVDA", "reason": "earnings_window", "card_id": C2}]) == 0


def test_empty_and_malformed_blockers_are_safe(tmp_path):
    assert ef.record_blockers(tmp_path, at(10, 0), []) == 0
    assert ef.record_blockers(tmp_path, at(10, 0), [{"symbol": "X"}]) == 0  # 没有 reason, 不记
    assert not (tmp_path / "funnel" / "2026-10-12.jsonl").exists()


# ── 汇总 ──
def test_summarize_counts_each_stage(tmp_path):
    _events(tmp_path, [
        {"kind": "opportunity", "state": "active", "card_id": C1, "symbol": "AAPL", "trade_date": "2026-10-12"},
        {"kind": "opportunity", "state": "active", "card_id": C2, "symbol": "NVDA", "trade_date": "2026-10-12"},
        {"kind": "opportunity", "state": "watch", "card_id": C3, "symbol": "MSFT", "trade_date": "2026-10-12"},
        {"kind": "entry_intent", "card_id": C1, "symbol": "AAPL", "status": "pending", "trade_date": "2026-10-12"},
        {"kind": "entry_intent", "card_id": C2, "symbol": "NVDA", "status": "blocked", "reason": "same_sector_cap", "trade_date": "2026-10-12"},
        {"kind": "fill", "card_id": C1, "symbol": "AAPL", "trade_date": "2026-10-12"},
        {"kind": "fill", "card_id": "opp-2026-10-09-TSLA-book-swing-4", "symbol": "TSLA", "trade_date": "2026-10-12", "backfill": True},
        {"kind": "opportunity", "state": "active", "card_id": "opp-2026-10-09-AMD-desk-9", "symbol": "AMD", "trade_date": "2026-10-12"},
    ])
    ef.record_blockers(tmp_path, at(10, 0), [{"symbol": "MSFT", "reason": "setup_degraded:breakout", "card_id": C3},
                                             {"symbol": "*", "reason": "circuit_breaker_day_halt"}])
    d = ef.summarize(tmp_path, days=1, end=MON)["days"][0]
    assert (d["day"], d["active"], d["touched"], d["filled"], d["backfill"]) == ("2026-10-12", 2, 2, 1, 1)
    # 逐卡拦截 1 条落盘 + 1 条 blocked intent; 全局拦截 (*) 单列在 global, 不计入逐卡计数
    assert d["blocked"] == 2
    assert {r["reason"] for r in d["reasons"]} == {"setup_degraded:breakout", "same_sector_cap"}
    assert d["global"] == [{"reason": "circuit_breaker_day_halt", "label": "当日熔断", "since": "10:00"}]
    assert next(r["label"] for r in d["reasons"] if r["reason"].startswith("setup_degraded")) == "规则已降级（breakout）"


def test_summarize_window_is_trading_days_and_newest_first(tmp_path):
    _events(tmp_path, [])
    days = [d["day"] for d in ef.summarize(tmp_path, days=4, end=date(2026, 10, 13))["days"]]
    assert days == ["2026-10-13", "2026-10-12", "2026-10-09", "2026-10-08"]   # 跳过周末
    assert all(d["active"] == 0 and d["blocked"] == 0 for d in ef.summarize(tmp_path, days=2, end=MON)["days"])


def test_summarize_ignores_other_days(tmp_path):
    _events(tmp_path, [{"kind": "fill", "card_id": C1, "symbol": "AAPL", "trade_date": "2026-10-09"}])
    d = ef.summarize(tmp_path, days=1, end=MON)["days"][0]
    assert d["filled"] == 0


def test_prune_removes_old_files(tmp_path):
    ef.record_blockers(tmp_path, at(10, 0), [{"symbol": "A", "reason": "earnings_window", "card_id": C1}])
    old = tmp_path / "funnel" / "2020-01-02.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    assert ef.prune(tmp_path, keep_days=120) == 1
    assert not old.exists() and (tmp_path / "funnel" / "2026-10-12.jsonl").exists()
