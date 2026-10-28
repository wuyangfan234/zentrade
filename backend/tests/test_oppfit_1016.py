"""0.24.1 机会页适配修复 (oppfit-1016): 板=簿、灰色历史、30 分钟刷新、实时价新鲜度、推送对齐、确定性 bug。"""
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.services import entry_guard
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv

CN = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


# ---------- IntentIndex: 与旧 cancel_pending 等价, 一轮只读一次台账 ----------

class _Ledger:
    def __init__(self, events):
        self.events = list(events)
        self.reads = 0

    def read_events(self):
        self.reads += 1
        return list(self.events)

    def append(self, kind, payload, *, at):
        e = {"seq": len(self.events) + 1, "kind": kind, "at": at, **payload}
        self.events.append(e)
        return e


def test_intent_index_cancels_pending_once_and_reads_ledger_once():
    lg = _Ledger([
        {"kind": "entry_intent", "card_id": "A", "status": "pending", "symbol": "A"},
        {"kind": "entry_intent", "card_id": "B", "status": "pending", "symbol": "B"},
        {"kind": "fill", "card_id": "B"},
        {"kind": "entry_intent", "card_id": "C", "status": "blocked", "symbol": "C"},
    ])
    now = datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz)
    idx = entry_guard.IntentIndex(lg)
    for cid in ("A", "B", "C", "D", "A"):
        entry_guard.cancel_pending(lg, SimpleNamespace(id=cid), now, "x", idx)
    assert lg.reads == 1
    new = lg.events[4:]
    assert [(e["card_id"], e["status"]) for e in new] == [("A", "blocked")]    # 第二次 A 不重复写


def test_cancel_pending_without_index_unchanged():
    lg = _Ledger([{"kind": "entry_intent", "card_id": "A", "status": "pending"}])
    entry_guard.cancel_pending(lg, SimpleNamespace(id="A"), datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz), "r")
    assert lg.events[-1]["status"] == "blocked" and lg.events[-1]["reason"] == "r"


# ---------- 实时价: 旧热报价不算 live ----------

def test_live_prices_ignore_stale_tick(monkeypatch, cal):
    now = datetime(2026, 10, 16, 8, 0, tzinfo=us_clock.tz)          # 盘前
    old_ms = datetime(2026, 10, 15, 15, 59, tzinfo=us_clock.tz).timestamp() * 1000
    fresh_ms = (now.timestamp() - 30) * 1000
    import app.api.portfolio as pf
    monkeypatch.setattr(pf, "_live_marks", lambda syms: {"OLD": (100.0, old_ms, "tickflow"), "NEW": (50.0, fresh_ms, "tickflow")})
    from app.services import card_volume as cv
    monkeypatch.setattr(cv, "premarket_prices", lambda syms, **k: {s: {"price": 98.0} for s in syms})
    out = sd._live_prices(None, {"OLD", "NEW"}, now.date(), now)
    assert out["NEW"] == (50.0, "live")
    assert out["OLD"] == (98.0, "premarket")


# ---------- 刷新窗: 北京 20:00 起每 30 分钟 ----------

@pytest.mark.parametrize("cn,due", [
    (datetime(2026, 10, 16, 19, 30, tzinfo=CN), False),     # 北京 19:30 = 美东 07:30 (夏令时)
    (datetime(2026, 10, 16, 20, 0, tzinfo=CN), True),       # 北京 20:00 = 美东 08:00
    (datetime(2026, 10, 17, 4, 0, tzinfo=CN), True),        # 美东 10-16 16:00 收盘
    (datetime(2026, 10, 17, 4, 30, tzinfo=CN), False),
    (datetime(2026, 10, 17, 20, 0, tzinfo=CN), False),      # 周六
    (datetime(2026, 11, 2, 20, 0, tzinfo=CN), True),        # 冬令时: 北京 20:00 = 美东 07:00
    (datetime(2026, 11, 2, 19, 30, tzinfo=CN), False),
])
def test_refresh_window_starts_beijing_2000(cn, due, cal):
    assert sd.refresh_due(cn) is due


# ---------- 板 = 簿, 灰色历史, 快照变化 ----------

def _ledger_stub(monkeypatch, positions=(), events=()):
    lg = SimpleNamespace(open_positions=list(positions), read_events=lambda: list(events))
    from app.services import desk_overview as ov
    monkeypatch.setattr(ov, "_account", lambda loop, data_dir: (lg, None))


def test_live_board_history_expired_and_closed(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    d0 = date(2026, 10, 6)
    _snapshot(tmp_path, d0, [_row("macd_golden", "OLD"), _row("phantom_4h", "OLD")], {"OLD"}, {"OLD": 100.0}, {"OLD": 2.0})
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA"),
                                   _row("macd_golden", "CLS"), _row("phantom_4h", "CLS")],
              {"AAA", "CLS"}, {"AAA": 100.0, "CLS": 50.0}, {"AAA": 2.0, "CLS": 1.0})
    closed_id = wb.card_id("CLS", "swing", "long", SIG_DAY)
    _ledger_stub(monkeypatch, events=[{"kind": "close", "card_id": closed_id, "symbol": "CLS", "at": "2026-10-09T12:00:00-04:00"}])
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert [c["symbol"] for c in out["long"]] == ["AAA"]
    hist = {c["symbol"]: c for c in out["history"]}
    assert hist["OLD"]["state"] == "expired" and hist["OLD"]["void_reason"] == "entry_window_over"
    assert hist["CLS"]["state"] == "closed"
    assert out["long"][0]["blocked"] is None


def test_live_board_book_excluded_card_goes_to_history(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA"), _row("macd_golden", "BBB"), _row("phantom_4h", "BBB")]
    _snapshot(tmp_path, SIG_DAY, rows, {"AAA", "BBB"}, {"AAA": 100.0, "BBB": 100.0}, {"AAA": 2.0, "BBB": 2.0})
    built = wb.build_day(tmp_path, _repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch={"AAA", "BBB"},
                         pm=wv.permission(tmp_path))
    built["cards"] = [c for c in built["cards"] if c["symbol"] == "AAA"]
    built["skipped"] = [{"symbol": "BBB", "side": "long", "reason": "not_pending:touched"}]
    wb.save_day(tmp_path, built)
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz))
    out = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))
    assert [c["symbol"] for c in out["long"]] == ["AAA"]
    assert out["history"][0]["symbol"] == "BBB" and out["history"][0]["void_reason"] == "not_pending:touched"


def test_live_board_marks_cooldown_and_beyond_stop(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    _ledger_stub(monkeypatch, events=[{"kind": "close", "card_id": "legacy-x", "symbol": "AAA", "trade_date": "2026-10-08"}])
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {"AAA": (96.0, "premarket")})
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz))
    c = sd.build_live(tmp_path, repo=object(), today=date(2026, 10, 9))["long"][0]
    assert c["blocked"] == "cooldown"                # 10-08 平仓, 10-09 在 3 日冷却内
    assert c["beyond_stop"] is True and c["price_source"] == "premarket" and c["state"] == "pending"


def test_snapshot_records_state_changes(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    _ledger_stub(monkeypatch)
    monkeypatch.setattr(sd, "_earnings_blocked", lambda *a: set())
    px = {"v": 110.0}
    monkeypatch.setattr(sd, "_live_prices", lambda repo, syms, today, now=None: {"AAA": (px["v"], "premarket")})
    t1 = datetime(2026, 10, 9, 8, 0, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: t1)
    assert sd.snapshot(tmp_path, repo=object(), now=t1)["written"] is True
    assert sd.snapshot(tmp_path, repo=object(), now=t1)["written"] is False          # 同一分钟不重复
    px["v"] = 99.2
    t2 = datetime(2026, 10, 9, 8, 30, tzinfo=us_clock.tz)
    sd.snapshot(tmp_path, repo=object(), now=t2)
    raw = json.loads((sd.live_dir(tmp_path) / "2026-10-09.json").read_text())
    assert len(raw["snapshots"]) == 2
    assert raw["changes"][0]["from"] == "pending" and raw["changes"][0]["to"] == "in_zone"
    info = sd.refresh_info(tmp_path, date(2026, 10, 9))
    assert info["count"] == 2 and info["last_at"].startswith("2026-10-09T08:30")


# ---------- 推送 = 机会板 ----------

def test_daily_push_uses_swing_board(tmp_path, monkeypatch, cal):
    _setup(tmp_path, monkeypatch)
    _snapshot(tmp_path, SIG_DAY, [_row("macd_golden", "AAA"), _row("phantom_4h", "AAA")], {"AAA"}, {"AAA": 100.0}, {"AAA": 2.0})
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "_repo_for_push", lambda: object())
    got = {}
    monkeypatch.setattr(no, "_enqueue", lambda d, k, m: got.update(key=k, msg=m) or True)
    assert no._enqueue_v4_daily(tmp_path, SIG_DAY, {"nav": {"nav": 101000.0}})
    body = got["msg"]["body"]
    assert "AAA 多 挂单 99.00-99.50 止损 97.50 目标 103.50" in body and "101,000.00" in body
    assert "R44" not in body


def test_preopen_swing_message_numbers():
    from app.services import premarket_gap as pg
    board = {"long": [{"symbol": "AAA", "side": "long", "state": "pending", "entry_zone": [99.0, 99.5], "stop": 97.5,
                       "target": 103.5, "blocked": "cooldown", "beyond_stop": False}], "short": []}
    msg = pg.swing_preopen_message(board, {"gaps": {"AAA": -1.2, "ZZZ": 5.0}}, date(2026, 10, 9),
                                   datetime(2026, 10, 9, 9, 0, tzinfo=us_clock.tz))
    assert "AAA 多 不下单 平仓冷却 挂单 99.00-99.50 止损 97.50 目标 103.50 盘前 -1.2%" in msg["body"]   # 0.27.0: 裁决在前
    assert "ZZZ +5.0%" in msg["body"]


# ---------- 确定性 bug ----------

def test_settings_monitor_migration_reads_dict_strategies():
    import inspect

    from app.api import settings as st
    src = inspect.getsource(st)
    assert 's["id"]: s.get("name", s["id"]) for s in strategy_engine.list_strategies()' in src


def test_ai_draft_removed_when_reload_fails(tmp_path):
    from app.strategy.ai_iterator import AIStrategyIterator

    class Eng:
        n = 0

        def reload(self):
            self.n += 1
            if self.n == 1:
                raise ValueError("strategy reload failed")

    with pytest.raises(ValueError):
        AIStrategyIterator._save_draft(Eng(), str(tmp_path), "ai_x", "META = {'id': 'x'}\n", {"id": "ai_x"})
    assert not (tmp_path / "strategies" / "ai" / "ai_x.py").exists()


def test_hot_pool_uses_cached_execution_symbols_without_ledger():
    from app.services import hot_quotes as hq

    class Loop:
        last_execution_symbols = ("ZZZ",)
        ledger = SimpleNamespace(open_positions=[])

        def execution_symbols(self):
            raise AssertionError("must not replay ledger in 5s tick")
    assert "ZZZ" in hq.pool(Loop())


def test_screener_slow_path_window_covers_ma200():
    import inspect

    from app.services import screener
    src = inspect.getsource(screener.ScreenerService._load_enriched_history)
    assert "int(lookback_days * 1.5) + 30" in src


def test_long_memory_rerun_keeps_shared_window_when_short(monkeypatch):
    import polars as pl

    from app.services import decision_scan_job as dj
    from app.strategy.engine import StrategyResult

    class Svc:
        def _load_enriched_history(self, as_of, n):
            return pl.DataFrame({"symbol": ["AAA"], "date": [date(2026, 10, 1)], "close": [1.0]})

    class Eng:
        def run(self, *a, **k):
            raise AssertionError("short window must not replace")

    ctx = type("C", (), {"current": pl.DataFrame({"symbol": ["AAA"], "date": [date(2026, 10, 8)]})})()
    res = {"tv_smc_structure": StrategyResult(as_of=date(2026, 10, 8), strategy_id="tv_smc_structure",
                                              rows=[{"symbol": "AAA"}])}
    monkeypatch.setattr(dj, "_watchlist", lambda: {"AAA"})
    dj._rerun_long_memory(Svc(), Eng(), date(2026, 10, 8), ctx, res, {}, ["tv_smc_structure"])
    assert res["tv_smc_structure"].rows == [{"symbol": "AAA"}]


def test_long_memory_rerun_keeps_score_order(monkeypatch):
    """Watchlist hits from the long window are merged back in score order, not appended at the end."""
    from dataclasses import dataclass, field

    import polars as pl

    from app.services import decision_scan_job as dj

    @dataclass
    class R:
        rows: list = field(default_factory=list)
        total: int = 0
        exit_signal_hits: list = field(default_factory=list)

    monkeypatch.setattr(dj, "_watchlist", lambda: {"WL"})
    from datetime import timedelta
    hist = pl.DataFrame({"symbol": ["WL"] * 500, "date": [date(2020, 1, 1) + timedelta(days=i) for i in range(500)]})
    svc = SimpleNamespace(_load_enriched_history=lambda d, n: hist)
    eng = SimpleNamespace(run=lambda sid, ctx, overrides=None: R(rows=[{"symbol": "WL", "score": 0.9}]))
    import dataclasses
    ctx = dataclasses.make_dataclass("C", [("current", object), ("history", object), ("market", object), ("cache_key", object)])(None, None, None, None)
    res = {"tv_smc_structure": R(rows=[{"symbol": "A", "score": 0.5}, {"symbol": "B", "score": 0.1}], total=2)}
    dj._rerun_long_memory(svc, eng, date(2026, 10, 1), ctx, res, {}, ["tv_smc_structure"])
    assert [r["symbol"] for r in res["tv_smc_structure"].rows] == ["WL", "A", "B"]
