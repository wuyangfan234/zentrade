"""10-09 两处修复的回归:
1. 盘中不得标记官方收盘; 核心指数官方日K从股票表写入指数表。
2. 限速器锁外睡; 开盘根缺失负缓存; IEX 交叉核对与主源并发 + 分段计时。
"""
import itertools
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.plugins.alpaca import provider as alp
from app.plugins.tickflow import provider as tf
from app.services import index_sync
from app.services.quote_service import QuoteService


# ── 1a. mark_official_close 收盘前拒绝 ──────────────────────────
def _fake_qs():
    saved = []
    return SimpleNamespace(_official_close_days=set(), _save_official_close_days=lambda: saved.append(1)), saved


@pytest.mark.parametrize("hh,mm,expect", [(11, 13, False), (15, 59, False), (16, 0, True), (20, 5, True)])
def test_mark_official_close_refused_before_regular_close(monkeypatch, hh, mm, expect):
    day = date(2026, 10, 6)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 6, hh, mm, tzinfo=us_clock.tz))
    qs, saved = _fake_qs()
    assert QuoteService.mark_official_close(qs, day) is expect
    assert (day.isoformat() in qs._official_close_days) is expect
    assert bool(saved) is expect


def test_mark_official_close_refuses_future_day(monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 6, 20, tzinfo=us_clock.tz))
    qs, _ = _fake_qs()
    assert QuoteService.mark_official_close(qs, date(2026, 10, 7)) is False


def test_mark_official_close_past_day_ok(monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 7, 10, tzinfo=us_clock.tz))
    qs, _ = _fake_qs()
    assert QuoteService.mark_official_close(qs, date(2026, 10, 6)) is True


# ── 1b. 核心指数官方日K → 指数表 ───────────────────────────────
def _stock_partition(tmp_path, d, rows):
    out = tmp_path / "kline_daily" / f"date={d.isoformat()}"
    out.mkdir(parents=True)
    pl.DataFrame(rows).with_columns(pl.lit(d).alias("date")).write_parquet(out / "part.parquet")


def _row(sym, close, qts=None):
    return {"symbol": sym, "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": 1e7, "amount": close * 1e7, "quote_ts": qts}


def _repo(tmp_path):
    got = {"daily": [], "enriched": []}
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           append_index_daily=lambda df: got["daily"].append(df),
                           append_index_enriched=lambda df: got["enriched"].append(df)), got


def test_index_sync_copies_only_official_core_rows(tmp_path):
    d = date(2026, 10, 6)
    _stock_partition(tmp_path, d, [_row("DIA", 508.0), _row("SPY", 779.09), _row("NVDA", 180.0),
                                   _row("QQQ", 750.0, qts=1791299513000)])  # QQQ 是盘中快照行, 不复制
    repo, got = _repo(tmp_path)
    done = index_sync.sync_core_index_from_stock(repo, [d], now=datetime(2026, 10, 7, 9, tzinfo=us_clock.tz))
    assert done == ["2026-10-06"]
    daily = got["daily"][0]
    assert sorted(daily["symbol"].to_list()) == ["DIA", "SPY"]
    assert daily.filter(pl.col("symbol") == "DIA")["close"][0] == 508.0
    enr = got["enriched"][0]
    assert {"raw_close", "raw_high", "raw_low", "quote_ts"} <= set(enr.columns)
    assert enr["quote_ts"].null_count() == enr.height


def test_index_sync_skips_today_before_close(tmp_path):
    d = date(2026, 10, 9)
    _stock_partition(tmp_path, d, [_row("SPY", 775.0)])
    repo, got = _repo(tmp_path)
    assert index_sync.sync_core_index_from_stock(repo, [d], now=datetime(2026, 10, 9, 11, 13, tzinfo=us_clock.tz)) == []
    assert not got["daily"]


def test_index_sync_missing_partition_is_noop(tmp_path):
    repo, _got = _repo(tmp_path)
    assert index_sync.sync_core_index_from_stock(repo, [date(2026, 10, 1)],
                                                 now=datetime(2026, 10, 9, 20, tzinfo=us_clock.tz)) == []


# ── 2a. 限速器锁外睡 ───────────────────────────────────────────
def test_rate_limiter_sleeps_outside_lock(monkeypatch):
    lim = tf.RateLimiter(per_minute=60)
    held = []
    monkeypatch.setattr(tf, "_sleep", lambda s: held.append(lim._lock.locked()))
    for _ in range(3):
        lim.acquire()
    assert held and not any(held)


def test_rate_limiter_keeps_spacing_across_threads():
    lim = tf.RateLimiter(per_minute=600)  # interval 0.125 s
    stamps = []
    ts = [threading.Thread(target=lambda: (lim.acquire(), stamps.append(time.monotonic()))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert min(gaps) >= lim.interval * 0.8


# ── 2b. 开盘根缺失负缓存 ───────────────────────────────────────
@pytest.fixture
def alpaca(monkeypatch):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("test", "test"))
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 9, 29, 11, 0, tzinfo=us_clock.tz))
    return alp.AlpacaProvider()


def test_missing_open_bar_not_refetched_every_tick(alpaca, monkeypatch):
    calls = []
    monkeypatch.setattr(alp, "_get", lambda url, *a, **k: (calls.append(url), {"bars": {}})[1])
    assert alpaca.official_opens(["TH"]) == {}
    n = len(calls)
    assert n >= 2  # IEX + SIP 各一次
    assert alpaca.official_opens(["TH"]) == {}
    assert len(calls) == n  # 5 分钟内不再重拉
    alpaca._open_retry_at["TH"] = 0  # 冷却到期
    alpaca.official_opens(["TH"])
    assert len(calls) > n


def test_open_bar_before_sip_window_still_retried(alpaca, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 9, 29, 9, 40, tzinfo=us_clock.tz))
    calls = []
    monkeypatch.setattr(alp, "_get", lambda url, *a, **k: (calls.append(url), {"bars": {}})[1])
    alpaca.official_opens(["TH"])
    alpaca.official_opens(["TH"])
    assert len(calls) == 2  # 09:46 前不负缓存, 每轮仍查 IEX


# ── 2c. 交叉核对并发 + 分段计时 ────────────────────────────────
def test_cross_check_runs_concurrently_and_reports_timing(monkeypatch):
    from app.services.trading_loop import TradingLoop
    now = datetime(2026, 9, 30, 11, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    ms = int(now.timestamp() * 1000) - 3000

    class Slow:
        def __init__(self, feed):
            self.feed, self.name = feed, feed

        def get_execution_quotes(self, symbols, **kw):
            time.sleep(0.3)
            return [{"symbol": "AAPL", "price": 100.0, "timestamp": ms, "feed": self.feed}]

    loop = TradingLoop.__new__(TradingLoop)
    loop.execution_source = {"active": None, "primary": None, "primary_error": None, "cross_checked": 0,
                             "deviations": [], "timing": {}}
    loop.trader = SimpleNamespace(quote_feed=None)
    monkeypatch.setattr(loop, "_iex_provider", lambda: Slow("iex"), raising=False)
    t0 = time.monotonic()
    kept, rejected = loop._execution_quotes(Slow("tickflow"), ["AAPL"])
    took = time.monotonic() - t0
    assert len(kept) == 1 and not rejected
    assert took < 0.55, took  # 串行约 0.6 s
    tm = loop.execution_source["timing"]
    assert tm["primary_s"] >= 0.25 and tm["iex_s"] >= 0.25
