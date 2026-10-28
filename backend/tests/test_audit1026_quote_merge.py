"""审计 2026-10-26 M-3: TickFlow 快照里 >120 秒没有新成交的票被 normalize_record 丢弃,
旧实现整区覆写当日 kline_daily 与 enriched → 这些票已写入的当日行被抹掉 (生产 10-24: 7161 只 → 3 只)。
现行: 股票/ETF 快照按 symbol 合并, 本轮没报价的票保留上一份当日值。"""
from datetime import datetime, timedelta

import polars as pl
import pytest

from app.market_clock import us_clock
from app.plugins.tickflow.provider import TickFlowProvider
from app.services.quote_service import QuoteService
from app.storage.repository import DataStore, KlineRepository

T0 = datetime(2026, 10, 26, 10, 30, 0, tzinfo=us_clock.tz)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _records(now, ages):
    prov = TickFlowProvider()
    out = []
    for i, (sym, age) in enumerate(ages.items()):
        price = 100.0 + i
        q = {"symbol": f"{sym}.US", "last_price": price, "prev_close": price * 0.99, "open": price * 0.995,
             "high": price * 1.01, "low": price * 0.98, "volume": 1_000_000.0, "amount": price * 1_000_000.0,
             "timestamp": _ms(now - timedelta(seconds=age)), "ext": {"name": sym, "change_pct": 0.0101}}
        out.append(prov._quote_row(q, now))
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"t": T0}
    monkeypatch.setattr(us_clock, "now", lambda: clock["t"])
    monkeypatch.setattr(us_clock, "_calendar_provider",
                        lambda s, e: [d for d in (s + timedelta(days=i) for i in range((e - s).days + 1)) if d.weekday() < 5])
    repo = KlineRepository(DataStore(tmp_path))
    day = T0.date() - timedelta(days=40)
    while day < T0.date():
        if day.weekday() < 5:
            repo.flush_live_daily(pl.DataFrame({
                "symbol": ["AAA", "BBB", "CCC"], "date": [day] * 3, "open": [99.0] * 3, "high": [102.0] * 3,
                "low": [98.0] * 3, "close": [100.0] * 3, "volume": [1e6] * 3, "amount": [1e8] * 3}))
        day += timedelta(days=1)
    monkeypatch.setattr(repo, "get_live_agg", lambda: pl.DataFrame())
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (pl.DataFrame(), None))
    svc = QuoteService()
    svc._repo = repo
    monkeypatch.setattr(svc, "_evaluate_monitors", lambda *a, **k: None)
    return svc, repo, clock, tmp_path / "kline_daily" / f"date={T0.date()}" / "part.parquet"


def test_stale_symbol_keeps_its_today_row(env):
    svc, repo, clock, part = env
    svc._process_full_market_records(_records(clock["t"], {"AAA": 5, "BBB": 10, "CCC": 30}), t0=0.0, now_ts=0.0)
    assert sorted(pl.read_parquet(part)["symbol"].to_list()) == ["AAA", "BBB", "CCC"]
    clock["t"] = T0 + timedelta(seconds=96)
    svc._process_full_market_records(_records(clock["t"], {"AAA": 3, "BBB": 4, "CCC": 126}), t0=0.0, now_ts=0.0)
    disk = pl.read_parquet(part).sort("symbol")
    assert disk["symbol"].to_list() == ["AAA", "BBB", "CCC"]           # CCC 不被抹掉
    assert sorted(repo._enriched_cache["symbol"].to_list()) == ["AAA", "BBB", "CCC"]
    assert disk.filter(pl.col("symbol") == "CCC")["close"][0] == pytest.approx(102.0)  # 保留上一份当日值


def test_etf_snapshot_skipped_on_official_close_day(monkeypatch):
    """复审: 官方收盘日 (管道已按 SIP 定版) ETF 快照同样不落盘, 与股票同一守卫。"""
    calls = []

    class _Repo:
        def get_index_symbol_set(self):
            return set()

        def get_etf_instruments(self):
            return pl.DataFrame({"symbol": ["SPY"]})

        def merge_live_daily_asset(self, asset_type, df):
            calls.append(asset_type)

        def flush_live_daily_asset(self, asset_type, df):
            calls.append(f"flush:{asset_type}")

    import app.services.quote_service as qs_module
    svc = QuoteService()
    svc._repo = _Repo()
    monkeypatch.setattr(us_clock, "now", lambda: T0)
    monkeypatch.setattr(qs_module, "_persist_last_fetch", lambda ms: None)
    monkeypatch.setattr(svc, "_update_volume_delta", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_broadcast_quote_updated", lambda: None)
    monkeypatch.setattr(svc, "is_official_close", lambda d: True)
    rec = {"symbol": "SPY", "last_price": 500.0, "open": 499.0, "high": 501.0, "low": 498.0, "volume": 1e6,
           "amount": 5e8, "timestamp": _ms(T0)}
    svc._process_full_market_records([rec], t0=0.0, now_ts=0.0)
    assert calls == []
