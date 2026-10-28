"""/api/portfolio/live: per-position entry / live price / P&L / stop & target distance / days held / source,
closed round trips, and summary (approved 2026-10-02: 持仓要有入场价/出场价/盈利率, 5 秒刷新)."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock

NOW = datetime(2026, 10, 2, 11, 0, tzinfo=us_clock.tz)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "today", lambda: NOW.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    at = "2026-09-30T10:00:00-04:00"
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10.0, "fee": 1.0, "trade_date": "2026-09-30",
                        "card_id": "opp-2026-09-29-AAA-breakout-1", "setup_type": "breakout", "stop_price": 9.0,
                        "invalidation": 9.0, "target_zone": [12.0, 13.0], "risk_per_share": 1.0, "max_holding_days": 7,
                        "quote_ts": at}, at=at)
    led.append("fill", {"symbol": "BBB", "side": "buy", "qty": 50, "price": 20.0, "fee": 1.0, "trade_date": "2026-09-30",
                        "card_id": "opp-2026-09-29-BBB-pullback-1", "setup_type": "pullback", "stop_price": 19.0,
                        "invalidation": 19.0, "target_zone": [22.0, 24.0], "risk_per_share": 1.0, "max_holding_days": 7,
                        "quote_ts": at}, at=at)
    led.append("close", {"symbol": "BBB", "qty": 50, "price": 22.5, "fee": 1.0, "reason": "target_zone_upper",
                         "card_id": "opp-2026-09-29-BBB-pullback-1", "entry_seq": 2, "r_multiple": 2.46,
                         "trade_date": "2026-10-01"}, at="2026-10-01T14:00:00-04:00")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [10.5]}))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {"AAA": (11.0, int(NOW.timestamp() * 1000) - 5000, "tickflow")})
    return TestClient(app)


def test_live_positions_have_entry_price_pnl_distance_and_days(client):
    body = client.get("/api/portfolio/live").json()
    (p,) = body["positions"]
    assert p["symbol"] == "AAA" and p["entry_price"] == 10.0 and p["last_price"] == 11.0
    assert p["price_source"] == "tickflow" and p["price_age_s"] == pytest.approx(5, abs=1)
    assert p["unrealized_pnl"] == pytest.approx(100 * (11.0 - 10.01), abs=0.02)  # avg_cost includes entry fee
    assert p["unrealized_pct"] == pytest.approx((11.0 / 10.01 - 1) * 100, abs=0.01)
    assert p["r_now"] == pytest.approx((11.0 - 10.01) / 1.0, abs=0.01)
    assert p["stop_price"] == 9.0 and p["stop_distance_pct"] == pytest.approx((9.0 / 11.0 - 1) * 100, abs=0.01)
    assert p["target_zone"] == [12.0, 13.0] and p["target_distance_pct"] == pytest.approx((12.0 / 11.0 - 1) * 100, abs=0.01)
    assert p["days_held"] == 3 and p["max_holding_days"] == 7
    assert p["setup_type"] == "breakout" and p["card_id"].startswith("opp-")


def test_closed_round_trips_have_entry_exit_return_and_reason(client):
    body = client.get("/api/portfolio/live").json()
    (c,) = body["closed"]
    assert c["symbol"] == "BBB" and c["entry_price"] == 20.0 and c["exit_price"] == 22.5
    assert c["entry_date"] == "2026-09-30" and c["exit_date"] == "2026-10-01"
    assert c["exit_reason"] == "target_zone_upper" and c["exit_reason_label"] == "到达目标上沿"
    assert c["pnl"] == pytest.approx(50 * 22.5 - 1 - (50 * 20 + 1), abs=0.01)
    assert c["return_pct"] == pytest.approx(c["pnl"] / (50 * 20 + 1) * 100, abs=0.01)
    assert c["r_multiple"] == 2.46 and c["fees"] == pytest.approx(2.0)


def test_summary_realized_unrealized_and_total_return(client):
    s = client.get("/api/portfolio/live").json()["summary"]
    assert s["realized_pnl"] == pytest.approx(123.0, abs=0.01)
    assert s["closed_trades"] == 1 and s["win_rate"] == 1.0 and s["avg_r"] == 2.46
    assert s["initial_equity"] == 100_000
    assert s["total_return_pct"] == pytest.approx((s["nav"] / 100_000 - 1) * 100, abs=0.001)
    assert s["refresh_seconds"] == 5


@pytest.mark.parametrize("accounting", ["contract", None])
def test_summary_realized_includes_partial_exits_and_dividends_of_open_rounds(tmp_path, monkeypatch, accounting):
    """bughunt-1015 B3: 「已实现」必须含仍持有回合里已减仓 (目标下沿减半 / 财报前减半) 的已实现盈亏与分红。
    生产 10-15: TSM/RKLB 减半已实现 +$329 只进了现金, 汇总「已实现 + 未实现」比总收益少 $328.75。
    恒等式: 已实现 + 未实现 = 净值 - 初始权益 (股票式/合约式都成立)。"""
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "today", lambda: NOW.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    at = "2026-09-30T10:00:00-04:00"
    led.append("fill", {"symbol": "CCC", "side": "buy", "qty": 100, "price": 10.0, "fee": 1.0, "trade_date": "2026-09-30",
                        "card_id": "opp-2026-09-29-CCC-book-x-1", "setup_type": "strategy:x", "stop_price": 9.0,
                        "invalidation": 9.0, "target_zone": [12.0, 13.0], "risk_per_share": 1.0, "max_holding_days": 7,
                        "quote_ts": at, "risk_pct": 0.01, **({"accounting": accounting} if accounting else {})}, at=at)
    led.append("partial_close", {"symbol": "CCC", "qty": 50, "price": 12.0, "fee": 1.0, "reason": "target_zone_lower",
                                 "card_id": "opp-2026-09-29-CCC-book-x-1", "entry_seq": 1, "trade_date": "2026-10-01"},
               at="2026-10-01T11:00:00-04:00")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 1)], "close": [11.0]}))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    s = TestClient(app).get("/api/portfolio/live").json()["summary"]
    # 减半: 50 x (12 - 10.01) - 1 = 98.5 已实现 (均价含开仓费 0.01/股)
    assert s["closed_trades"] == 0
    assert s["realized_pnl"] == pytest.approx(98.5, abs=0.01)
    assert s["realized_pnl"] + s["unrealized_pnl"] == pytest.approx(s["nav"] - s["initial_equity"], abs=0.01)
