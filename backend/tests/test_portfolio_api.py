"""Portfolio GET 只读契约：已存参数、ET 日期、运行中台账一致快照。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock


@pytest.fixture
def client(tmp_path, monkeypatch):
    calls = []

    def get_daily(symbol, start, end):
        calls.append((symbol, start, end))
        return pl.DataFrame({"close": [120.0]})

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), get_daily=get_daily, calls=calls,
    )
    monkeypatch.setattr(us_clock, "today", lambda: date(2001, 1, 2))
    return TestClient(app)


def seed(tmp_path):
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account(initial_equity=25000.0, slippage_bps=0.0))
    lg.append("fill", {
        "symbol": "AAPL", "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0,
        "card_id": "c1", "stop_price": 95.0, "trade_date": "2001-01-02",
    }, at="2001-01-02T10:00:00-05:00")
    lg.write_nav_daily(date(2001, 1, 2), {"AAPL": 120.0})
    return lg


def disk_state(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    }


def test_uninitialized_get_does_not_create_ledger(client, tmp_path):
    response = client.get("/api/portfolio/overview")
    assert response.status_code == 200
    assert response.json() == {
        "cash": 0, "nav": 0, "positions": [], "open_risk": 0,
        "events": [], "nav_daily": [], "note": "台账未初始化",
    }
    assert not (tmp_path / "ledger").exists()


def test_overview_replays_saved_equity_not_defaults(client, tmp_path):
    seed(tmp_path)
    response = client.get("/api/portfolio/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["cash"] == 23999.0
    assert body["nav"] == 25199.0
    assert body["positions"] == [{
        "symbol": "AAPL", "qty": 10, "avg_cost": 100.1,
        "entry_date": "2001-01-02", "card_id": "c1", "stop_price": 95.0,
    }]
    assert body["open_risk"] == 51.0
    assert body["events"][0]["seq"] == 1
    assert body["nav_daily"][0]["nav"] == 25199.0


def test_overview_marks_use_market_clock_date(client, tmp_path):
    seed(tmp_path)
    response = client.get("/api/portfolio/overview")
    assert response.status_code == 200
    [(symbol, start, end)] = client.app.state.repo.calls
    assert symbol == "AAPL"
    assert end == date(2001, 1, 2)          # 窗口终点 = 市场时钟 (美东) 日期
    assert start < end                      # 向前回看, 取最近一个已有收盘


def test_overview_marks_with_latest_close_before_todays_bar_exists(client, tmp_path, monkeypatch):
    """美东零点后、当日首根日K出现前: 净值按最近收盘估值, 不回落到成本价。"""
    seed(tmp_path)
    monkeypatch.setattr(us_clock, "today", lambda: date(2001, 1, 3))
    closes = {date(2001, 1, 2): 120.0}

    def get_daily(symbol, start, end):
        days = sorted(d for d in closes if start <= d <= end)
        return pl.DataFrame({"date": days, "close": [closes[d] for d in days]},
                            schema={"date": pl.Date, "close": pl.Float64})

    client.app.state.repo.get_daily = get_daily
    body = client.get("/api/portfolio/overview").json()
    assert body["nav"] == 25199.0            # 23999 现金 + 10 股 × 120 (最近收盘)


@pytest.mark.parametrize("materialized_exists", [True, False])
def test_overview_does_not_write_any_ledger_file(client, tmp_path, materialized_exists):
    lg = seed(tmp_path)
    path = lg.data_dir / "positions.json"
    if materialized_exists:
        path.write_text('{"stale_materialization": true}')
    else:
        path.unlink()
    before = disk_state(lg.data_dir)
    for _ in range(2):
        assert client.get("/api/portfolio/overview").status_code == 200
    assert disk_state(lg.data_dir) == before


def test_overview_reuses_booted_loop_without_replay(client, tmp_path, monkeypatch):
    lg = seed(tmp_path)
    client.app.state.trading_loop = SimpleNamespace(ledger=lg, _boot=True)

    def no_replay(*args, **kwargs):
        pytest.fail("GET must snapshot the running ledger, never replay it")

    monkeypatch.setattr(Ledger, "replay", no_replay)
    before = disk_state(lg.data_dir)
    body = client.get("/api/portfolio/overview").json()
    assert body["cash"] == lg.cash
    assert disk_state(lg.data_dir) == before


def test_unbooted_loop_is_not_initialized_by_get(client, tmp_path, monkeypatch):
    stored = seed(tmp_path)
    unbooted = Ledger(stored.data_dir)

    def no_boot(*args, **kwargs):
        pytest.fail("GET must not boot or replay the shared writer")

    client.app.state.trading_loop = SimpleNamespace(ledger=unbooted, _boot=False, boot=no_boot)
    monkeypatch.setattr(unbooted, "replay", no_boot)
    before = disk_state(stored.data_dir)
    body = client.get("/api/portfolio/overview").json()
    assert body["cash"] == stored.cash
    assert unbooted.cash == 0.0
    assert unbooted.open_positions == []
    assert disk_state(stored.data_dir) == before


def test_overview_holds_detached_snapshot_while_marks_are_loaded(client, tmp_path):
    lg = seed(tmp_path)
    client.app.state.trading_loop = SimpleNamespace(ledger=lg, _boot=True)

    def get_daily(symbol, start, end):
        # 行情查询不能一直持台账锁，也不能把查询期间的新现金混入旧持仓。
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lg.append, "partial_close", {
                "symbol": "AAPL", "qty": 5, "price": 120.0, "fee": 1.0,
            }, at="2001-01-02T11:00:00-05:00").result(timeout=2)
        return pl.DataFrame({"close": [120.0]})

    client.app.state.repo.get_daily = get_daily
    response = client.get("/api/portfolio/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["cash"] == 23999.0
    assert body["positions"][0]["qty"] == 10
    assert body["nav"] == 25199.0
    assert body["open_risk"] == 51.0
    assert len(body["events"]) == 1
    assert lg.position("AAPL").qty == 5
    assert len(lg.read_events()) == 2


def test_bad_saved_account_is_not_replaced_by_default(client, tmp_path):
    lg = seed(tmp_path)
    (lg.data_dir / "account.json").write_text("{broken")
    before = disk_state(lg.data_dir)
    with TestClient(client.app, raise_server_exceptions=False) as fail_client:
        response = fail_client.get("/api/portfolio/overview")
    assert response.status_code == 500
    assert disk_state(lg.data_dir) == before
