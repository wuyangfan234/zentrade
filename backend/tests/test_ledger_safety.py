"""批 A: 账户恢复、回合隔离、只读查询及写前校验。全部落盘到 tmp_path。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from threading import Event

import pytest

from app.ledger.core import Account, Ledger


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    return lg


def buy(lg, **overrides):
    payload = {
        "symbol": "AAPL", "side": "buy", "qty": 10, "price": 100.0,
        "fee": 1.0, "trade_date": "2026-09-21", "card_id": "old-card",
        "stop_price": 95.0,
    }
    payload.update(overrides)
    return lg.append("fill", payload, at="2026-09-21T10:00:00-04:00")


@pytest.mark.parametrize("exit_kind", ["close", "partial_close", "fill"])
@pytest.mark.parametrize("metadata", [{"card_id": "new-card", "stop_price": 190.0}, {}])
def test_reopen_starts_fresh_round(ledger, exit_kind, metadata):
    buy(ledger)
    ledger.append(exit_kind, {
        "symbol": "AAPL", "side": "sell", "qty": 10, "price": 110.0,
    }, at="2026-09-22T10:00:00-04:00")
    assert ledger.position("AAPL") is None
    ledger.append("fill", {
        "symbol": "AAPL", "side": "buy", "qty": 5, "price": 200.0,
        "trade_date": "2026-09-29", **metadata,
    }, at="2026-09-29T10:00:00-04:00")

    for lg in (ledger, Ledger(ledger.data_dir)):
        if lg is not ledger:
            lg.replay(Account())
        pos = lg.position("AAPL")
        assert pos is not None
        assert pos.entry_date == "2026-09-29"
        assert pos.card_id == metadata.get("card_id")
        assert pos.stop_price == metadata.get("stop_price")
        assert pos.avg_cost == 200.0
        assert pos.qty == 5


def test_account_from_file_preserves_all_saved_parameters(tmp_path):
    saved = Account(
        initial_equity=43210.0, risk_per_trade_pct=0.003, max_positions=2,
        max_portfolio_risk_pct=0.02, commission_per_share=0.0,
        min_commission=0.0, slippage_bps=0.0, max_holding_days=3,
    )
    path = tmp_path / "account.json"
    path.write_text(json.dumps({"version": 987, **asdict(saved)}))
    before = path.read_bytes()
    assert Account.from_file(path) == saved
    assert path.read_bytes() == before


def test_account_from_file_defaults_only_missing_fields(tmp_path):
    path = tmp_path / "account.json"
    path.write_text(json.dumps({"version": 1, "initial_equity": 0.0}))
    assert Account.from_file(path) == Account(initial_equity=0.0)


@pytest.mark.parametrize("content", ["{broken", '{"unexpected_setting": 9}'])
def test_account_from_file_does_not_silently_reset_bad_file(tmp_path, content):
    path = tmp_path / "account.json"
    path.write_text(content)
    with pytest.raises((ValueError, TypeError)):
        Account.from_file(path)
    assert path.read_text() == content


def test_account_from_file_does_not_create_missing_account(tmp_path):
    path = tmp_path / "missing" / "account.json"
    with pytest.raises(FileNotFoundError):
        Account.from_file(path)
    assert not path.parent.exists()


def disk_state(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    }


def test_replay_can_restore_without_writing_materialized_file(ledger):
    buy(ledger)
    (ledger.data_dir / "positions.json").write_text('{"stale": true}')
    before = disk_state(ledger.data_dir)
    restored = Ledger(ledger.data_dir)
    restored.replay(Account(), persist=False)
    assert restored.cash == ledger.cash
    assert asdict(restored.position("AAPL")) == asdict(ledger.position("AAPL"))
    assert disk_state(ledger.data_dir) == before


def test_read_only_ledger_does_not_create_missing_directory(tmp_path):
    root = tmp_path / "missing" / "ledger"
    lg = Ledger(root, read_only=True)
    lg.replay(Account(), persist=False)
    assert lg.cash == Account().initial_equity
    assert lg.open_positions == []
    assert not root.exists()


@pytest.mark.parametrize("operation", ["init", "append", "nav"])
def test_read_only_ledger_refuses_writers(tmp_path, operation):
    root = tmp_path / "ledger"
    lg = Ledger(root, read_only=True)
    with pytest.raises(PermissionError):
        if operation == "init":
            lg.init_account(Account())
        elif operation == "append":
            buy(lg)
        else:
            lg.write_nav_daily(date(2026, 9, 21), {})
    assert not root.exists()


def test_read_only_ledger_never_persists_even_with_default_replay(ledger):
    buy(ledger)
    (ledger.data_dir / "positions.json").unlink()
    lg = Ledger(ledger.data_dir, read_only=True)
    lg.replay(Account())
    assert lg.cash == ledger.cash
    assert not (ledger.data_dir / "positions.json").exists()


def test_snapshot_is_detached_but_position_remains_live(ledger):
    buy(ledger)
    pos = ledger.position("AAPL")
    before = disk_state(ledger.data_dir)
    snap = ledger.snapshot()
    assert disk_state(ledger.data_dir) == before
    ledger.append("partial_close", {
        "symbol": "AAPL", "qty": 5, "price": 120.0,
    }, at="2026-09-22T10:00:00-04:00")
    assert ledger.position("AAPL") is pos
    assert pos.qty == 5
    assert snap["positions"][0]["qty"] == 10
    assert snap["cash"] == 98999.0
    assert snap["open_risk"] == pytest.approx(51.0)
    assert len(snap["events"]) == snap["seq"] == 1
    assert snap["nav_daily"] == []
    snap["positions"][0]["qty"] = 123
    snap["events"][0]["qty"] = 123
    assert pos.qty == 5
    assert ledger.read_events()[0]["qty"] == 10


@pytest.mark.parametrize("operation", ["replay", "nav", "snapshot", "events"])
def test_operations_wait_for_existing_ledger_lock(ledger, operation):
    buy(ledger)
    started, done = Event(), Event()

    def run():
        started.set()
        try:
            if operation == "replay":
                ledger.replay(Account())
            elif operation == "nav":
                ledger.write_nav_daily(date(2026, 9, 21), {"AAPL": 110.0})
            elif operation == "snapshot":
                ledger.snapshot()
            else:
                ledger.read_events()
        finally:
            done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with ledger._lock:
            future = pool.submit(run)
            assert started.wait(2)
            assert not done.wait(0.1), f"{operation} bypassed the ledger lock"
        future.result(timeout=2)


def test_public_lock_is_same_reentrant_lock(ledger):
    assert ledger.lock is ledger._lock
    with ledger.lock, ledger.lock:
        buy(ledger)
    assert ledger.position("AAPL").qty == 10


def test_concurrent_daily_nav_is_written_once(ledger):
    buy(ledger)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ledger.write_nav_daily(date(2026, 9, 21), {}), range(16)))
    rows = (ledger.data_dir / "nav" / "daily.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["nav"] == 100000.0


def test_payload_cannot_override_event_envelope(ledger):
    event = ledger.append("entry_intent", {
        "seq": 999, "kind": "fill", "at": "1900-01-01", "card_id": "c1",
    }, at="2026-09-21T10:00:00-04:00")
    assert event == {
        "seq": 1, "kind": "entry_intent", "at": "2026-09-21T10:00:00-04:00",
        "card_id": "c1",
    }
    assert ledger.read_events() == [event]


@pytest.mark.parametrize("kind", ["entry_intent", "exit_intent", "future_event_kind"])
def test_unknown_events_remain_appendable_and_replayable(ledger, kind):
    buy(ledger)
    cash = ledger.cash
    pos = asdict(ledger.position("AAPL"))
    event = ledger.append(kind, {"card_id": "new-card", "symbol": "AAPL"}, at="later")
    assert event["seq"] == 2
    ledger.replay(Account())
    assert ledger.cash == cash
    assert asdict(ledger.position("AAPL")) == pos
    assert ledger.read_events()[-1] == event


@pytest.mark.parametrize("kind,side", [
    ("fill", "buy"), ("fill", "sell"), ("close", "sell"), ("partial_close", "sell"),
])
@pytest.mark.parametrize("field,value", [
    ("qty", 0), ("qty", -1), ("qty", float("nan")), ("qty", float("inf")),
    ("qty", "nan"), ("qty", None), ("qty", True),
    ("price", 0), ("price", -1), ("price", float("nan")), ("price", "inf"),
    ("fee", -1), ("fee", float("nan")), ("fee", float("inf")),
])
def test_invalid_trade_rejected_before_disk_or_state_change(ledger, kind, side, field, value):
    buy(ledger)
    before = disk_state(ledger.data_dir)
    cash, pos = ledger.cash, asdict(ledger.position("AAPL"))
    payload = {"symbol": "AAPL", "side": side, "qty": 1, "price": 100.0, "fee": 1.0}
    payload[field] = value
    with pytest.raises(ValueError):
        ledger.append(kind, payload, at="2026-09-21T11:00:00-04:00")
    assert disk_state(ledger.data_dir) == before
    assert ledger.cash == cash
    assert asdict(ledger.position("AAPL")) == pos
    assert buy(ledger)["seq"] == 2


@pytest.mark.parametrize("kind", ["fill", "close", "partial_close"])
@pytest.mark.parametrize("symbol,qty", [("AAPL", 11), ("MISSING", 1)])
def test_oversell_rejected_before_append(ledger, kind, symbol, qty):
    buy(ledger)
    before = disk_state(ledger.data_dir)
    with pytest.raises(ValueError):
        ledger.append(kind, {
            "symbol": symbol, "side": "sell", "qty": qty, "price": 100.0,
        }, at="2026-09-21T11:00:00-04:00")
    assert disk_state(ledger.data_dir) == before
    assert ledger.position("AAPL").qty == 10
    assert ledger.cash == 98999.0


@pytest.mark.parametrize("overrides", [
    {"side": "typo"}, {"symbol": ""}, {"stop_price": float("nan")},
    {"qty": 1e308, "price": 1e308},
    {"qty": 5e-324, "price": 100.0, "fee": 1.0},
])
def test_invalid_fill_metadata_or_overflow_does_not_dirty_events(ledger, overrides):
    before = disk_state(ledger.data_dir)
    with pytest.raises(ValueError):
        buy(ledger, **overrides)
    assert disk_state(ledger.data_dir) == before
    assert ledger.cash == Account().initial_equity
    assert ledger.open_positions == []
    assert buy(ledger)["seq"] == 1


def test_serialization_failure_does_not_consume_seq(ledger):
    with pytest.raises(TypeError):
        ledger.append("entry_intent", {"unserializable": object()}, at="now")
    assert ledger.read_events() == []
    assert buy(ledger)["seq"] == 1
