"""第 2 批: EDGAR 登记为财务源 + 定时同步 (盯盘台账/补跑) + Finnhub 备源 + 失败保留旧数据。"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from test_edgar_fundamentals import _facts

from app.services import edgar_fundamentals as ef
from app.services import fundamentals_sync as fs


def _tickers(*pairs):
    return json.dumps({str(i): {"cik_str": c, "ticker": t} for i, (t, c) in enumerate(pairs)})


def test_registry_financial_routes_to_sec_edgar_and_copy_updated():
    from app.data_providers import capabilities as caps
    from app.data_providers import custom
    fin = next(c for c in caps.CAPABILITY_REGISTRY if c["id"] == "financial")
    assert "美股暂无可用源" not in fin["desc"] and "SEC EDGAR" in fin["desc"]
    assert fin["default"] == "sec_edgar"
    plug = next(p for p in custom.list_plugins() if p["name"] == "sec_edgar")
    assert plug["available"] and "financial" in plug["datasets"]
    m = caps.build_capability_matrix({"financial_data_provider": "sec_edgar"})
    assert next(c for c in m["capabilities"] if c["id"] == "financial")["usable"] is True


def test_financial_provider_defaults_to_sec_edgar(monkeypatch):
    from app.services import preferences
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_financial_provider() == "sec_edgar"


def test_job_watched_and_catchup():
    from app.services import ops_watch
    assert fs.JOB_ID in ops_watch.WATCHED and fs.JOB_ID in ops_watch.CATCHUP


def test_finnhub_fallback_for_symbols_edgar_cannot_serve(tmp_path: Path):
    def http_get(url, headers):
        if url.endswith("company_tickers.json"):
            return _tickers(("AAPL", 320193))
        if "finnhub.io/api/v1/stock/metric" in url:
            assert "token=T" in url
            return json.dumps({"metric": {}, "series": {"annual": {
                "roe": [{"period": "2025-12-31", "v": 0.12}],
                "grossMargin": [{"period": "2025-12-31", "v": 0.4}],
            }}})
        if "finnhub.io/api/v1/stock/profile2" in url:
            return json.dumps({"shareOutstanding": 123.5})  # 百万股
        return json.dumps(_facts())

    out = fs.run(tmp_path, ["AAPL", "TSM"], http_get=http_get, finnhub_token="T", sleep=lambda s: None,
                 today="2026-10-12", watchlist={"TSM"})
    assert out["edgar_ok"] == 1 and out["finnhub_ok"] == 1
    m = pl.read_parquet(tmp_path / "financials/metrics/part.parquet").filter(pl.col("symbol") == "TSM").row(0, named=True)
    assert m["roe"] == pytest.approx(0.12) and m["source"] == "finnhub"
    assert m["announce_date"] == "2026-03-31"  # 无申报日 → 年报保守取期末 +90 天
    sh = pl.read_parquet(tmp_path / "financials/shares/part.parquet").filter(pl.col("symbol") == "TSM").row(0, named=True)
    assert sh["total_shares"] == pytest.approx(123.5e6) and sh["source"] == "finnhub"
    # 无申报日: 取「期末 +45 天 ≤ 今天」的最近季末 → 2026-06-30 期, 2026-08-14 生效
    assert sh["period_end"] == "2026-06-30" and sh["announce_date"] == "2026-08-14"


def test_failure_keeps_old_data_and_raises_for_alert(tmp_path: Path):
    def ok_get(url, headers):
        return _tickers(("AAPL", 320193)) if url.endswith("company_tickers.json") else json.dumps(_facts())
    fs.run(tmp_path, ["AAPL"], http_get=ok_get, finnhub_token=None, sleep=lambda s: None, today="2026-10-12")
    before = (tmp_path / "financials/metrics/part.parquet").read_bytes()

    def down(url, headers):
        raise OSError("sec down")
    with pytest.raises(fs.SyncFailedError):
        fs.run(tmp_path, ["AAPL"], http_get=down, finnhub_token=None, sleep=lambda s: None, today="2026-10-13")
    assert (tmp_path / "financials/metrics/part.parquet").read_bytes() == before


def test_partial_refresh_merges_keeps_other_symbols(tmp_path: Path):
    def get(url, headers):
        return _tickers(("AAPL", 320193), ("MSFT", 789019)) if url.endswith("company_tickers.json") else json.dumps(_facts())
    fs.run(tmp_path, ["AAPL", "MSFT"], http_get=get, finnhub_token=None, sleep=lambda s: None, today="2026-10-12")
    fs.run(tmp_path, ["AAPL"], http_get=get, finnhub_token=None, sleep=lambda s: None, today="2026-10-13")
    syms = set(pl.read_parquet(tmp_path / "financials/metrics/part.parquet")["symbol"])
    assert syms == {"AAPL", "MSFT"}


def test_pick_batch_prioritises_missing_then_oldest(tmp_path: Path):
    state = {"AAPL": "2026-10-01", "MSFT": "2026-09-01"}
    assert fs.pick_batch(["AAPL", "MSFT", "NVDA"], state, limit=2) == ["NVDA", "MSFT"]


def test_scheduler_registers_job(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    job = sched.get_job(fs.JOB_ID)
    assert job is not None and type(job.trigger).__name__ == "CronTrigger"


def test_edgar_module_still_exposes_tables():
    assert ef.TABLES == ("income", "balance_sheet", "cash_flow", "metrics", "shares")


def _fh_get(tickers, facts_for):
    def http_get(url, headers):
        if url.endswith("company_tickers.json"):
            return _tickers(*tickers)
        if "stock/metric" in url:
            return json.dumps({"metric": {}, "series": {}})
        if "stock/profile2" in url:
            return json.dumps({"shareOutstanding": 2000.0})
        return json.dumps(facts_for(url))
    return http_get


def _shares(tmp_path, sym):
    return pl.read_parquet(tmp_path / "financials/shares/part.parquet").filter(pl.col("symbol") == sym)


def test_watchlist_multi_class_gets_finnhub_shares_marked(tmp_path: Path):
    get = _fh_get([("META", 1326801)], lambda u: _facts())
    fs.run(tmp_path, ["META"], http_get=get, finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist={"META"}, multi_class={"META"})
    sh = _shares(tmp_path, "META")
    fh = sh.filter(pl.col("source") == "finnhub").row(0, named=True)
    assert fh["float_shares"] == pytest.approx(2e9) and fh["announce_date"] == "2026-08-14"
    m = pl.read_parquet(tmp_path / "financials/metrics/part.parquet").filter(pl.col("symbol") == "META")
    assert m["bps"].is_null().all()


def test_non_watchlist_multi_class_stays_null(tmp_path: Path):
    get = _fh_get([("META", 1326801)], lambda u: _facts())
    fs.run(tmp_path, ["META"], http_get=get, finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist=set(), multi_class={"META"})
    sh = _shares(tmp_path, "META")
    assert sh["float_shares"].is_null().all() and (sh["source"] != "finnhub").all()


def test_non_watchlist_foreign_gets_no_shares(tmp_path: Path):
    get = _fh_get([("AAPL", 320193)], lambda u: _facts())
    fs.run(tmp_path, ["AAPL", "SKHY"], http_get=get, finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist=set())
    p = tmp_path / "financials/shares/part.parquet"
    assert pl.read_parquet(p).filter(pl.col("symbol") == "SKHY").is_empty()


def test_watchlist_single_class_with_edgar_shares_not_overridden(tmp_path: Path):
    get = _fh_get([("AAPL", 320193)], lambda u: _facts())
    fs.run(tmp_path, ["AAPL"], http_get=get, finnhub_token="T", sleep=lambda s: None, today="2026-10-12",
           watchlist={"AAPL"})
    assert (_shares(tmp_path, "AAPL")["source"] == "sec_edgar").all()


def test_load_watchlist_symbols(tmp_path: Path):
    d = tmp_path / "user_data"
    d.mkdir()
    pl.DataFrame({"symbol": ["META", "AAPL"]}).write_parquet(d / "watchlist.parquet")
    assert fs.load_watchlist(tmp_path) == {"META", "AAPL"}
    assert fs.load_watchlist(tmp_path / "nope") == set()


def test_quarter_rule():
    assert fs.finnhub_share_period("2026-10-12") == ("2026-06-30", "2026-08-14")
    assert fs.finnhub_share_period("2026-08-13") == ("2026-03-31", "2026-05-15")
