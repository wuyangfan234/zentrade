"""audit1026 b3c: L-8 交易日历缓存刷新 / L-10 自选加入日美东口径 / L-11 CSV 名称列 Class A/B /
L-16 持仓来源机会板中文名 / L-17 台账事件区过滤机会流水 + 空头止损方向 / L-19 逐卡明细 pass_signal 退出原因筛选"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import calendar as cal
from app.market_clock import us_clock

# ── L-8 交易日历磁盘缓存 ───────────────────────────────────


def _weekdays(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


@pytest.fixture
def frozen_cal(tmp_path, monkeypatch):
    """calendar 模块的 today 冻结 + 缓存落 tmp + 失败退避清零; 返回 (set_today, fetches, write_cache)。"""
    state = {"today": date(2026, 10, 26)}

    class _D(date):
        @classmethod
        def today(cls):
            return state["today"]

    monkeypatch.setattr(cal, "date", _D)
    monkeypatch.setattr(cal, "_cache_path", lambda: tmp_path / "cal.json")
    monkeypatch.setattr(cal, "_last_failed_fetch", None, raising=False)
    monkeypatch.setattr(cal, "_early_closes", None)
    fetches: list[tuple[date, date]] = []

    def write_cache(days: list[date], warmed_at: date, **extra):
        (tmp_path / "cal.json").write_text(json.dumps({
            "trading_days": [d.isoformat() for d in days], "early_closes": {},
            "warmed_at": warmed_at.isoformat(), **extra,
        }))

    def set_today(d: date):
        state["today"] = d

    return set_today, fetches, write_cache


def test_l8_cache_near_expiry_refetches(frozen_cal, monkeypatch):
    """生产形态: 缓存止于 2028-10-13, 今天走到 2028-10-16 → 必须重拉, 不能把每天判休市"""
    set_today, fetches, write_cache = frozen_cal
    write_cache(_weekdays(date(2025, 10, 16), date(2028, 10, 13)), date(2026, 10, 16))
    set_today(date(2028, 10, 16))

    def fetch(s, e):
        fetches.append((s, e))
        return [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in _weekdays(s, e)]

    monkeypatch.setattr(cal, "_fetch_calendar_rows", fetch)
    assert cal.alpaca_trading_days(date(2028, 10, 16), date(2028, 10, 17)) == [date(2028, 10, 16), date(2028, 10, 17)]
    assert len(fetches) == 1


def test_l8_aged_cache_refetches_and_learns_new_closure(frozen_cal, monkeypatch):
    """覆盖还远, 但超过 30 天没刷新 → 重拉; 新增的临时休市 (11-02) 要学到"""
    _set_today, fetches, write_cache = frozen_cal
    write_cache(_weekdays(date(2025, 10, 16), date(2028, 10, 13)), date(2026, 9, 1))
    closure = date(2026, 11, 2)

    def fetch(s, e):
        fetches.append((s, e))
        return [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in _weekdays(s, e) if d != closure]

    monkeypatch.setattr(cal, "_fetch_calendar_rows", fetch)
    days = cal.alpaca_trading_days(date(2026, 10, 30), date(2026, 11, 3))
    assert closure not in days and date(2026, 11, 3) in days
    assert len(fetches) == 1
    cal.alpaca_trading_days(date(2026, 10, 30), date(2026, 11, 3))   # 刚刷新过, 不再出网
    assert len(fetches) == 1


def test_l8_refetch_failure_keeps_old_cache_and_fails_open_beyond(frozen_cal, monkeypatch):
    """重拉失败: 不抛, 覆盖内沿用旧缓存 (含已知休市), 覆盖外按工作日 fail-open"""
    set_today, fetches, write_cache = frozen_cal
    holiday = date(2028, 10, 9)
    write_cache([d for d in _weekdays(date(2025, 10, 16), date(2028, 10, 13)) if d != holiday], date(2026, 10, 16))
    set_today(date(2028, 10, 16))

    def boom(s, e):
        fetches.append((s, e))
        raise RuntimeError("network down")

    monkeypatch.setattr(cal, "_fetch_calendar_rows", boom)
    days = cal.alpaca_trading_days(date(2028, 10, 6), date(2028, 10, 17))
    assert holiday not in days
    assert date(2028, 10, 13) in days
    assert date(2028, 10, 16) in days and date(2028, 10, 17) in days   # 覆盖外按工作日
    assert date(2028, 10, 14) not in days                               # 周末仍休
    assert len(fetches) == 1
    cal.alpaca_trading_days(date(2028, 10, 16), date(2028, 10, 16))     # 失败退避: 不立刻重试
    assert len(fetches) == 1


def test_l8_market_clock_beyond_cache_is_trading_day(frozen_cal, monkeypatch):
    """MarketClock 层: 覆盖终点之后的工作日 is_trading_day=True (原来全判休市)"""
    set_today, _fetches, write_cache = frozen_cal
    write_cache(_weekdays(date(2025, 10, 16), date(2028, 10, 13)), date(2026, 10, 16))
    set_today(date(2028, 10, 16))
    monkeypatch.setattr(cal, "_fetch_calendar_rows", lambda s, e: (_ for _ in ()).throw(RuntimeError("down")))
    from app.market_clock import clock as clock_mod
    monkeypatch.setattr(us_clock, "_calendar_provider", clock_mod._default_us_calendar)   # conftest 默认关掉, 这里用生产入口
    us_clock._cal_cache.clear()
    try:
        assert us_clock.is_trading_day(date(2028, 10, 17)) is True
    finally:
        us_clock._cal_cache.clear()


def test_l8_partial_refetch_keeps_earlier_cached_days(frozen_cal, monkeypatch):
    """复审: 上游从中间某天才开始返回时, 不抹掉之前缓存的交易日"""
    set_today, _fetches, write_cache = frozen_cal
    write_cache(_weekdays(date(2024, 10, 16), date(2028, 10, 13)), date(2026, 9, 1))      # 超龄 -> 重拉
    set_today(date(2026, 10, 26))
    monkeypatch.setattr(cal, "_fetch_calendar_rows", lambda s, e: [
        {"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in _weekdays(date(2026, 11, 1), e)])
    days = cal.alpaca_trading_days(date(2026, 1, 5), date(2026, 1, 9))
    assert days == _weekdays(date(2026, 1, 5), date(2026, 1, 9))


def test_l8_malformed_payload_is_a_failure_with_backoff(frozen_cal, monkeypatch):
    """复审: HTTP 200 但格式不对 -> 按失败处理 (沿用旧缓存 + 退避), 不每次都打网络"""
    set_today, fetches, write_cache = frozen_cal
    holiday = date(2026, 11, 26)
    write_cache([d for d in _weekdays(date(2024, 10, 16), date(2028, 10, 13)) if d != holiday], date(2026, 9, 1))
    set_today(date(2026, 10, 26))

    def bad(s, e):
        fetches.append((s, e))
        return {"message": "unexpected"}
    monkeypatch.setattr(cal, "_fetch_calendar_rows", bad)
    assert holiday not in cal.alpaca_trading_days(date(2026, 11, 23), date(2026, 11, 27))
    cal.alpaca_trading_days(date(2026, 11, 23), date(2026, 11, 27))
    assert len(fetches) == 1


def test_l8_concurrent_refresh_only_one_thread_hits_network(frozen_cal, monkeypatch):
    """复审: 已有缓存时并发调用只有一个线程打网络, 其余直接用旧缓存不等待"""
    import threading
    import time as _t
    set_today, fetches, write_cache = frozen_cal
    write_cache(_weekdays(date(2024, 10, 16), date(2028, 10, 13)), date(2026, 9, 1))
    set_today(date(2026, 10, 26))
    gate = threading.Event()

    def slow(s, e):
        fetches.append((s, e))
        gate.wait(2)
        return [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in _weekdays(s, e)]
    monkeypatch.setattr(cal, "_fetch_calendar_rows", slow)
    first = threading.Thread(target=cal.alpaca_trading_days, args=(date(2026, 10, 26), date(2026, 10, 26)))
    first.start()
    _t.sleep(0.1)
    t0 = _t.monotonic()
    assert cal.alpaca_trading_days(date(2026, 10, 26), date(2026, 10, 26)) == [date(2026, 10, 26)]
    assert _t.monotonic() - t0 < 0.5
    gate.set()
    first.join()
    assert len(fetches) == 1


# ── L-10 自选加入日: 美东日 ─────────────────────────────────

def test_l10_added_date_is_et_day():
    from app.api.watchlist import _added_dates_et
    out = _added_dates_et([
        {"symbol": "ASTS", "added_at": "2026-10-01T19:35:19"},    # 美东 10-01 15:35 (北京已是 10-02)
        {"symbol": "LATE", "added_at": "2026-10-02T03:30:00"},    # 美东 10-01 23:30
        {"symbol": "BAD", "added_at": "x"},
    ])
    assert out == {"ASTS": date(2026, 10, 1), "LATE": date(2026, 10, 1)}


# ── L-11 CSV 名称列 Class A/B ───────────────────────────────

def test_l11_class_share_words_not_tickers():
    from app.services.watchlist_csv import extract_tickers, parse_csv_rows
    rows = parse_csv_rows("Symbol,Name\nGOOGL,Alphabet Inc. Class A Common Stock\nBRK.B,BERKSHIRE HATHAWAY Class B\n")
    flat = [t for r in rows for t in r]
    assert "A" not in flat and "B" not in flat
    assert "GOOGL" in flat and "BRK.B" in flat
    assert extract_tickers("A, B") == ["A", "B"]   # 单独写的 A/B 仍是代码


# ── L-16 / L-17 持仓页 ──────────────────────────────────────

NOW = datetime(2026, 10, 22, 11, 0, tzinfo=us_clock.tz)
AT = "2026-10-22T10:00:00-04:00"


@pytest.fixture
def portfolio_client(tmp_path, monkeypatch):
    from app.api.portfolio import router
    from app.ledger.core import Account, Ledger
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "today", lambda: NOW.date())
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(tmp_path / "ledger")
    led.init_account(Account())
    led.append("fill", {"symbol": "IREN", "side": "short", "qty": 10, "price": 30.0, "fee": 0.0,
                        "trade_date": "2026-10-20", "card_id": "opp-2026-10-16-IREN-book-swing-short-f68caf03",
                        "setup_type": "strategy:swing", "stop_price": 33.0, "risk_per_share": 3.0,
                        "max_holding_days": 5, "quote_ts": AT}, at=AT)
    led.append("fill", {"symbol": "CRCL", "side": "buy", "qty": 10, "price": 80.0, "fee": 0.0,
                        "trade_date": "2026-10-20", "card_id": "opp-2026-10-16-CRCL-book-swing-aa",
                        "setup_type": "strategy:swing", "stop_price": 76.0, "risk_per_share": 4.0,
                        "max_holding_days": 5, "quote_ts": AT}, at=AT)
    led.append("stop_move", {"symbol": "IREN", "stop_price": 30.0, "reason": "trailing_1.2R",
                             "trade_date": "2026-10-22"}, at=AT)
    led.append("stop_move", {"symbol": "CRCL", "stop_price": 80.0, "reason": "trailing_1.0R",
                             "trade_date": "2026-10-22"}, at=AT)
    for i in range(250):
        led.append("opportunity", {"card_id": f"opp-x-{i}", "symbol": "SQQQ", "state": "expired",
                                   "reason": "门槛未通过", "trade_date": "2026-10-22"}, at=AT)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [date(2026, 10, 21)], "close": [28.0]}))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.strategy_engine = SimpleNamespace(list_strategies=lambda: [])
    monkeypatch.setattr("app.api.portfolio._live_marks", lambda symbols: {})
    return TestClient(app)


def test_l17_overview_events_drop_opportunity_noise(portfolio_client):
    ev = portfolio_client.get("/api/portfolio/overview").json()["events"]
    kinds = [e["kind"] for e in ev]
    assert "opportunity" not in kinds
    assert kinds.count("fill") == 2 and kinds.count("stop_move") == 2


def test_l17_stop_move_carries_position_side(portfolio_client):
    ev = portfolio_client.get("/api/portfolio/overview").json()["events"]
    side = {e["symbol"]: e.get("position_side") for e in ev if e["kind"] == "stop_move"}
    assert side == {"IREN": "short", "CRCL": "long"}


def test_l16_swing_source_label_is_chinese(portfolio_client):
    pos = {p["symbol"]: p for p in portfolio_client.get("/api/portfolio/live").json()["positions"]}
    assert pos["IREN"]["setup_label"] == "机会板（空）"  # noqa: RUF001
    assert pos["CRCL"]["setup_label"] == "机会板（多）"  # noqa: RUF001
    assert pos["IREN"]["setup_type"] == "strategy:swing"   # 原字段不变


# ── L-19 逐卡明细 pass_signal 行按 signal_exit_reason 筛选 ────

def test_l19_settlement_cards_filter_reads_signal_exit_reason(tmp_path):
    from app.api import opportunities as opp_api
    d = tmp_path / "research" / "settlement"
    d.mkdir(parents=True)
    rows = [
        {"key": "k1", "card_id": "c1", "record_kind": "settled", "outcome": "HIT", "rule": "r1",
         "signal_day": "2026-10-01", "exit_reason": "time_stop", "r": 0.4},
        {"key": "k2", "card_id": "c2", "record_kind": "pass_void", "outcome": "PASS", "rule": "r1",
         "signal_day": "2026-10-02"},
        {"key": "k2", "card_id": "c2", "record_kind": "pass_signal", "outcome": "PASS", "rule": "r1",
         "signal_day": "2026-10-02", "signal_r": -0.02, "signal_exit_date": "2026-10-09",
         "signal_exit_reason": "trailing_stop"},
    ]
    (d / "cards.zt-settle-v1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    app = FastAPI()
    app.include_router(opp_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    c = TestClient(app)
    b = c.get("/api/opportunities/settlement/cards").json()
    assert b["exit_reasons"] == ["time_stop", "trailing_stop"]
    b = c.get("/api/opportunities/settlement/cards", params={"exit_reason": "trailing_stop"}).json()
    assert [(r["card_id"], r["record_kind"]) for r in b["rows"]] == [("c2", "pass_signal")]
