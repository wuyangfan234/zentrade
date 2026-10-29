"""0.18.0 信号中心第一批 (产品决策 2026-10-14): 持仓初始止损/目标 + 止损调整记录 (只从台账推算), 结算逐卡明细 (只读 jsonl)。"""
import hashlib
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import opportunities as opp_api
from app.api.portfolio import router as pf_router
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock

NOW = datetime(2026, 10, 2, 11, 0, tzinfo=us_clock.tz)
AT = "2026-09-30T10:00:00-04:00"


def _fill(sym, stop):
    return {"symbol": sym, "side": "buy", "qty": 10, "price": 10.0, "fee": 0.0, "trade_date": "2026-09-30",
            "card_id": f"opp-2026-09-29-{sym}-x-1", "setup_type": "x", "stop_price": stop, "invalidation": stop,
            "target_zone": [12.0, 13.0], "risk_per_share": 1.0, "max_holding_days": 7, "quote_ts": AT}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "today", lambda: NOW.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    # 旧回合: CCC 开仓→调止损→平仓→重开; 旧回合的 stop_move 不得带入新回合
    led.append("fill", _fill("CCC", 8.0), at=AT)
    led.append("stop_move", {"symbol": "CCC", "stop_price": 8.5, "reason": "trailing_1.0R", "trade_date": "2026-09-30",
                             "card_id": "opp-2026-09-29-CCC-x-1"}, at=AT)
    led.append("close", {"symbol": "CCC", "qty": 10, "price": 9.0, "fee": 0.0, "reason": "invalidation",
                         "card_id": "opp-2026-09-29-CCC-x-1", "trade_date": "2026-09-30"}, at=AT)
    led.append("fill", _fill("AAA", 9.0), at=AT)
    led.append("fill", _fill("BBB", 9.0), at=AT)
    led.append("fill", _fill("CCC", 9.2), at=AT)
    led.append("stop_move", {"symbol": "AAA", "stop_price": 10.0, "reason": "trailing_1.0R", "trade_date": "2026-10-01",
                             "card_id": "opp-2026-09-29-AAA-x-1"}, at="2026-10-01T15:00:00-04:00")
    led.append("stop_move", {"symbol": "AAA", "stop_price": 10.5, "reason": "trailing_1.2R", "trade_date": "2026-10-02",
                             "card_id": "opp-2026-09-29-AAA-x-1"}, at="2026-10-02T10:00:00-04:00")
    ev = tmp_path / "ledger" / "events.jsonl"
    before = hashlib.sha256(ev.read_bytes()).hexdigest()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda s, a, b: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [10.8]}))
    app = FastAPI()
    app.include_router(pf_router)
    app.state.repo = repo
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    return TestClient(app), ev, before


def test_live_stop_initial_target_initial_and_moves(live):
    c, ev, before = live
    pos = {p["symbol"]: p for p in c.get("/api/portfolio/live").json()["positions"]}
    a = pos["AAA"]
    assert a["stop_initial"] == 9.0 and a["stop_price"] == 10.5
    assert a["target_initial"] == [12.0, 13.0]
    assert [m["stop_price"] for m in a["stop_moves"]] == [10.0, 10.5]
    assert [m["reason"] for m in a["stop_moves"]] == ["trailing_1.0R", "trailing_1.2R"]
    assert a["stop_moves"][0]["trade_date"] == "2026-10-01" and a["stop_moves"][0]["from"] == 9.0
    assert a["stop_moves"][1]["from"] == 10.0
    assert pos["BBB"]["stop_initial"] == 9.0 and pos["BBB"]["stop_moves"] == []


def test_live_reopened_round_does_not_inherit_old_moves(live):
    c, _, _ = live
    p = {p["symbol"]: p for p in c.get("/api/portfolio/live").json()["positions"]}["CCC"]
    assert p["stop_initial"] == 9.2 and p["stop_moves"] == [] and p["stop_price"] == 9.2


def test_live_is_read_only_on_ledger(live):
    c, ev, before = live
    c.get("/api/portfolio/live")
    assert hashlib.sha256(ev.read_bytes()).hexdigest() == before


ROWS = [
    {"key": "k1", "card_id": "c1", "record_kind": "settled", "outcome": "HIT", "rule": "r1", "symbol": "AAA", "side": "long",
     "signal_day": "2026-09-29", "fill_date": "2026-09-30", "exit_date": "2026-10-08", "exit_reason": "time_stop",
     "hold_days": 7, "entry": 10.0, "r": 0.4, "excess_r": 0.1, "provenance": "live"},
    {"key": "k2", "card_id": "c2", "record_kind": "settled", "outcome": "HIT", "rule": "r2", "symbol": "BBB", "side": "long",
     "signal_day": "2026-09-29", "fill_date": "2026-09-30", "exit_date": "2026-10-01", "exit_reason": "invalidation",
     "hold_days": 2, "entry": 20.0, "r": -1.2, "excess_r": -1.0, "provenance": "backfill"},
    {"key": "k3", "card_id": "c3", "record_kind": "pass_void", "outcome": "PASS", "rule": "r1", "symbol": "CCC", "side": "short",
     "signal_day": "2026-09-30", "provenance": "live"},
]


@pytest.fixture
def settle(tmp_path):
    d = tmp_path / "research" / "settlement"
    d.mkdir(parents=True)
    f = d / "cards.zt-settle-v1.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    before = hashlib.sha256(f.read_bytes()).hexdigest()
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app), f, before


def test_settlement_cards_all_rows(settle):
    c, f, before = settle
    b = c.get("/api/opportunities/settlement/cards").json()
    assert b["available"] and b["contract"] == "zt-settle-v1"
    assert b["total"] == 3 and len(b["rows"]) == 3
    assert sorted(b["rules"]) == ["r1", "r2"] and sorted(b["exit_reasons"]) == ["invalidation", "time_stop"]
    assert hashlib.sha256(f.read_bytes()).hexdigest() == before


def test_settlement_cards_filters(settle):
    c, _, _ = settle
    b = c.get("/api/opportunities/settlement/cards", params={"rule": "r1"}).json()
    assert b["total"] == 3 and {r["card_id"] for r in b["rows"]} == {"c1", "c3"}
    b = c.get("/api/opportunities/settlement/cards", params={"exit_reason": "invalidation"}).json()
    assert [r["card_id"] for r in b["rows"]] == ["c2"] and b["rows"][0]["r"] == -1.2


def test_settlement_cards_missing_file(tmp_path):
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    b = TestClient(app).get("/api/opportunities/settlement/cards").json()
    assert b["available"] is False and b["rows"] == [] and b["total"] == 0
    assert not (tmp_path / "research").exists()
