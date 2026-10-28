"""持仓页策略来源显示中文名 + 方向 (10-10 review 待确认 1, 用户 10-11 批复修)。

只加展示字段 setup_label; 台账/存储里的 setup_type 原样不动。名称复用机会卡同款映射
(引擎策略 META 名 + card_replay.SHORT_NAMES 看空名), 方向由持仓方向决定; 映射不到 → None, 前端回退原 id。
"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.portfolio import router
from app.ledger.core import Account, Ledger
from app.market_clock import us_clock

NOW = datetime(2026, 10, 9, 11, 0, tzinfo=us_clock.tz)
AT = "2026-10-09T10:00:00-04:00"


def _fill(led, symbol, side, setup):
    led.append("fill", {"symbol": symbol, "side": side, "qty": 10, "price": 100.0, "fee": 0.0, "trade_date": "2026-10-09",
                        "card_id": f"c-{symbol}", "setup_type": setup, "stop_price": 105.0 if side == "short" else 95.0,
                        "risk_per_share": 5.0, "max_holding_days": 7, "quote_ts": AT}, at=AT)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "today", lambda: NOW.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    _fill(led, "ADI", "short", "strategy:tv_ut_bot")
    _fill(led, "AMAT", "short", "strategy:phantom_4h_short")
    _fill(led, "PLTR", "buy", "strategy:tv_ut_bot")
    _fill(led, "AAA", "buy", "breakout")
    _fill(led, "ZZZ", "buy", "strategy:unknown_rule_x")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 8)], "close": [100.0]}))
    engine = SimpleNamespace(list_strategies=lambda: [{"id": "tv_ut_bot", "name": "UT Bot 翻多"}])
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.strategy_engine = engine
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    return TestClient(app)


def test_positions_carry_chinese_setup_label_with_direction(client):
    pos = {p["symbol"]: p for p in client.get("/api/portfolio/live").json()["positions"]}
    assert pos["ADI"]["setup_label"] == "UT Bot 翻空（空）"
    assert pos["AMAT"]["setup_label"] == "Phantom 4h 空（空）"
    assert pos["PLTR"]["setup_label"] == "UT Bot 翻多（多）"
    assert pos["AAA"]["setup_label"] == "S1 突破（多）"
    assert pos["ZZZ"]["setup_label"] is None                       # 映射不到 → 前端回退原 id
    assert pos["ADI"]["setup_type"] == "strategy:tv_ut_bot"         # 原字段不变
