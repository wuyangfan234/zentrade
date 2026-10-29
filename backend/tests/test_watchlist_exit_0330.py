"""0.33.0 (2026-10-22 产品决策 B): 持仓票移出自选 -> 收盘定版记离场意向, 下一交易日开盘平仓。

复用收盘破止损的同一条执行路径 (exit_intent -> execute_pending_exit 次日开盘价成交)。
读不到自选 / 自选为空不动 (fail-safe: 名单读失败不能把全部持仓平掉); 已有收盘破止损意向的不重复记。
"""
from datetime import date, datetime

import pytest
from test_trade_fixes_0161 import PC_DAY, _intents, _pc_loop

from app.market_clock import us_clock
from app.services import decision_scan_job
from app.services.trading_loop import WATCHLIST_EXIT

ET = us_clock.tz
NEXT = date(2026, 9, 30)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _close(loop, official):
    loop.repo.official = official
    return loop.post_close_manage(PC_DAY, confirmed_official=True)


def test_removed_symbol_gets_exit_intent_kept_symbol_does_not(tmp_path, monkeypatch, cal):
    loop = _pc_loop(tmp_path, monkeypatch, official={"A": 100., "B": 100.}, cached={})
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"B", "X"})
    _close(loop, {"A": 100., "B": 100.})
    it = _intents(loop)
    assert [(e["symbol"], e["reason"]) for e in it] == [("A", WATCHLIST_EXIT)]
    assert it[0]["signal_price"] == 100. and it[0]["trade_date"] == PC_DAY.isoformat()
    assert it[0]["card_id"] == "held-A"


def test_next_open_executes_watchlist_exit(tmp_path, monkeypatch, cal):
    loop = _pc_loop(tmp_path, monkeypatch, official={"A": 100., "B": 100.}, cached={})
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"B"})
    _close(loop, {"A": 100., "B": 100.})
    at = datetime(2026, 9, 30, 9, 45, tzinfo=ET).isoformat()
    ev = loop.trader.execute_pending_exit("A", open_price=97., trade_date=NEXT, at=at)
    assert ev is not None and ev["kind"] == "close" and ev["reason"] == WATCHLIST_EXIT
    assert loop.ledger.position("A") is None and loop.ledger.position("B") is not None
    # 同日不执行 (信号日当天开盘不算 "下一交易日")
    assert loop.trader.execute_pending_exit("B", open_price=97., trade_date=NEXT, at=at) is None


def test_empty_or_unreadable_watchlist_never_closes(tmp_path, monkeypatch, cal):
    loop = _pc_loop(tmp_path, monkeypatch, official={"A": 100., "B": 100.}, cached={})
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: set())
    _close(loop, {"A": 100., "B": 100.})
    assert _intents(loop) == []


def test_stop_intent_not_duplicated(tmp_path, monkeypatch, cal):
    loop = _pc_loop(tmp_path, monkeypatch, official={"A": 94., "B": 100.}, cached={})
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"B"})
    _close(loop, {"A": 94., "B": 100.})
    it = _intents(loop)
    assert [(e["symbol"], e["reason"]) for e in it] == [("A", "invalidation")]   # 收盘破止损优先, 不重复记


def test_missing_official_close_skips_until_next_close(tmp_path, monkeypatch, cal):
    loop = _pc_loop(tmp_path, monkeypatch, official={"B": 100.}, cached={})
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"B"})
    _close(loop, {"B": 100.})
    assert _intents(loop) == []
