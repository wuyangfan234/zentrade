"""EDGAR companyfacts → data/financials/* 点时对齐 (2026-10-12 用户批准 EDGAR+Finnhub 第 1 批)。"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from app.backtest.fundamentals import load_fundamental_snapshot
from app.services import edgar_fundamentals as ef


def _fact(val, start, end, filed, form="10-Q", fp="Q1", fy=2025):
    d = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp, "fy": fy, "accn": f"a-{filed}-{end}"}
    if start:
        d["start"] = start
    return d


def _facts():
    def usd(rows):
        return {"units": {"USD": rows}}

    return {
        "cik": 320193, "entityName": "Apple",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                _fact(1000, None, "2025-04-20", "2025-05-02"),
                _fact(990, None, "2026-04-20", "2026-05-01"),
            ]}}},
            "us-gaap": {
                "Revenues": usd([
                    _fact(100, "2025-01-01", "2025-03-31", "2025-05-02"),
                    _fact(130, "2026-01-01", "2026-03-31", "2026-05-01"),
                    # 重述: 次年 10-Q 再报一次去年同期, 更晚 filed → 点时口径必须保留首报值 100
                    _fact(105, "2025-01-01", "2025-03-31", "2026-05-01"),
                ]),
                "GrossProfit": usd([_fact(40, "2026-01-01", "2026-03-31", "2026-05-01")]),
                "NetIncomeLoss": usd([
                    _fact(20, "2025-01-01", "2025-03-31", "2025-05-02"),
                    _fact(26, "2026-01-01", "2026-03-31", "2026-05-01"),
                ]),
                "Assets": usd([_fact(1000, None, "2026-03-31", "2026-05-01")]),
                "Liabilities": usd([_fact(600, None, "2026-03-31", "2026-05-01")]),
                "StockholdersEquity": usd([_fact(400, None, "2026-03-31", "2026-05-01")]),
                "NetCashProvidedByUsedInOperatingActivities": usd([_fact(50, "2026-01-01", "2026-03-31", "2026-05-01")]),
                "PaymentsToAcquirePropertyPlantAndEquipment": usd([_fact(10, "2026-01-01", "2026-03-31", "2026-05-01")]),
            },
        },
    }


def test_parse_point_in_time_first_reported_and_metrics():
    t = ef.parse_companyfacts("AAPL", _facts())
    inc = t["income"].sort("period_end")
    q25 = inc.filter(pl.col("period_end") == "2025-03-31").row(0, named=True)
    assert q25["revenue"] == 100 and q25["announce_date"] == "2025-05-02"  # 首报值, 不吃重述
    q26 = inc.filter(pl.col("period_end") == "2026-03-31").row(0, named=True)
    assert q26["period_type"] == "Q" and q26["announce_date"] == "2026-05-01"
    m = t["metrics"].filter(pl.col("period_end") == "2026-03-31").row(0, named=True)
    assert m["revenue_yoy"] == pytest.approx(0.30)
    assert m["net_income_yoy"] == pytest.approx(0.30)
    assert m["gross_margin"] == pytest.approx(40 / 130)
    assert m["net_margin"] == pytest.approx(0.2)
    assert m["debt_to_asset_ratio"] == pytest.approx(0.6)
    assert m["roe"] == pytest.approx(26 * 4 / 400, rel=0.02)  # 季度年化
    assert m["bps"] == pytest.approx(400 / 990)
    cf = t["cash_flow"].filter(pl.col("period_end") == "2026-03-31").row(0, named=True)
    assert cf["free_cash_flow"] == 40
    sh = t["shares"].sort("period_end")
    assert sh["float_shares"].to_list() == [1000, 990]  # 总股本近似流通股 (用户批准)
    assert sh["announce_date"].to_list() == ["2025-05-02", "2026-05-01"]


def test_missing_values_stay_null_never_zero():
    f = _facts()
    del f["facts"]["us-gaap"]["GrossProfit"]
    m = ef.parse_companyfacts("AAPL", f)["metrics"].filter(pl.col("period_end") == "2026-03-31").row(0, named=True)
    assert m["gross_margin"] is None


def test_write_tables_and_fundamentals_loader_reads_it(tmp_path: Path):
    t = ef.parse_companyfacts("AAPL", _facts())
    ef.write_tables(tmp_path, [t])
    for name in ef.TABLES:
        assert (tmp_path / "financials" / name / "part.parquet").exists()
    snap = load_fundamental_snapshot(tmp_path)
    assert snap is not None and set(snap["symbol"]) == {"AAPL"}


def test_sync_uses_user_agent_and_rate_limit(tmp_path: Path):
    calls: list[tuple[str, dict]] = []

    def http_get(url, headers):
        calls.append((url, headers))
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL"}, "1": {"cik_str": 1, "ticker": "ZZZ"}})
        assert url.endswith("CIK0000320193.json")
        return json.dumps(_facts())

    sleeps: list[float] = []
    out = ef.sync(tmp_path, ["AAPL", "MSFT"], http_get=http_get, sleep=sleeps.append, clock=lambda: 0.0)
    assert out["ok"] == 1 and out["missing_cik"] == ["MSFT"]
    assert all(h["User-Agent"] == ef.user_agent() for _, h in calls)
    assert ef.MIN_INTERVAL >= 0.1  # ≤10 次/秒
    assert (tmp_path / "financials" / "metrics" / "part.parquet").exists()


def test_sync_isolates_per_symbol_failure(tmp_path: Path):
    def http_get(url, headers):
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL"}, "1": {"cik_str": 789019, "ticker": "MSFT"}})
        if "789019" in url:
            raise OSError("boom")
        return json.dumps(_facts())

    out = ef.sync(tmp_path, ["AAPL", "MSFT"], http_get=http_get, sleep=lambda s: None, clock=lambda: 0.0)
    assert out["ok"] == 1 and out["failed"] == ["MSFT"]


def test_dotted_ticker_maps_to_sec_dash():
    assert ef.sec_ticker("BRK.B") == "BRK-B"


def test_clear_data_also_clears_shares_table():
    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "data.py").read_text(encoding="utf-8")
    assert '("metrics", "income", "balance_sheet", "cash_flow", "shares")' in src
