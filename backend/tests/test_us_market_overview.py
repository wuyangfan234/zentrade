"""美股总览数值/缺失口径及现存 HTTP、助手调用链的离线回归。"""
from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import polars as pl
import pytest

from app.api import overview as api
from app.custom.assistant import tools
from app.services import market_overview_builder as overview

DAY = date(2026, 9, 25)


class PoisonDepth:
    def __getattribute__(self, name):
        pytest.fail("美股总览不应读取 depth_service")


@pytest.fixture
def make_overview(monkeypatch, tmp_path):
    def make(frame, latest=DAY):
        class Screener:
            def __init__(self, repo):
                pass

            def latest_date(self):
                return latest

            def _load_enriched_for_date(self, day):
                assert day == DAY
                return frame

        monkeypatch.setattr(overview, "ScreenerService", Screener)
        repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                               execute_all=lambda *a: [])
        return repo
    return make


def test_partial_values_use_valid_denominators_and_preserve_unknowns(make_overview):
    repo = make_overview(pl.DataFrame({
        "symbol": ["AAPL", "MSFT", "NVDA", "META", "AMD", "TSLA"],
        "change_pct": [0.1, -0.02, 0.0, None, float("nan"), float("inf")],
        "amount": [100.0, 0.0, None, float("nan"), float("inf"), -1.0],
        "volume": [0.0, 1.0, 0.0, None, 0.0, 0.0],
        "close": [110.0, 90.0, 100.0, 100.0, None, 100.0],
        "ma20": [100.0, 100.0, None, float("nan"), 100.0, 0.0],
    }))
    data = overview.build_market_overview(repo, depth_service=PoisonDepth())
    breadth = data["breadth"]
    assert data["data_status"] == "partial"
    assert (breadth["total"], breadth["valid"], breadth["unknown"]) == (6, 3, 3)
    assert (breadth["up"], breadth["down"], breadth["flat"]) == (1, 1, 1)
    assert breadth["up_pct"] == pytest.approx(100 / 3)
    assert breadth["avg_pct"] == pytest.approx(0.08 / 3)
    assert breadth["median_pct"] == 0.0
    assert data["amount"]["total"] == 100.0
    assert data["amount"]["avg"] == 50.0
    assert (data["amount"]["valid"], data["amount"]["unknown"]) == (2, 4)
    assert data["trend"]["above_ma20"] == 1
    assert data["trend"]["above_ma20_pct"] == 50.0
    assert data["trend"]["ma20_valid"] == 2
    assert data["trend"]["above_ma5"] is None
    assert data["trend"]["above_ma5_pct"] is None
    assert data["trend"]["new_high"] is None
    assert data["activity"]["avg_turnover"] is None
    assert data["activity"]["vol_ratio"] is None
    assert sum(b["count"] for b in data["distribution"]) == 3
    assert all("board" not in row for row in data["top_gainers"])
    assert data["indices_status"]["status"] == "unavailable"
    assert data["industry_rank"]["status"] == "unavailable"
    assert "缺失" in tools.summarize_tool_result("get_market_overview", {"ok": True, "result": data})
    json.dumps(data, allow_nan=False)


@pytest.mark.parametrize("frame", [pl.DataFrame(), pl.DataFrame({"symbol": ["AAPL"]})],
                         ids=["empty-frame", "missing-columns"])
def test_dated_empty_or_missing_columns_are_unavailable(make_overview, frame):
    data = overview.build_market_overview(make_overview(frame))
    assert data["as_of"] == DAY.isoformat()
    assert data["data_status"] == "unavailable"
    assert data["breadth"]["avg_pct"] is None
    assert data["breadth"]["up_pct"] is None
    assert data["amount"]["total"] is None
    assert data["distribution"] == []
    assert data["activity"]["vol_ratio"] is None
    assert "缺失" in data["note"]
    assert "缺失" in tools.summarize_tool_result("get_market_overview", {"ok": True, "result": data})
    json.dumps(data, allow_nan=False)


def test_genuine_zero_is_not_missing_and_even_median_is_averaged(make_overview):
    data = overview.build_market_overview(make_overview(pl.DataFrame({
        "symbol": ["AAPL", "MSFT"], "change_pct": [-0.02, 0.0],
        "amount": [0.0, 0.0], "volume": [0.0, 0.0],
    })))
    assert data["data_status"] == "available"
    assert data["breadth"]["flat"] == 1
    assert data["breadth"]["unknown"] == 0
    assert data["breadth"]["median_pct"] == -0.01
    assert data["amount"]["total"] == 0.0
    assert data["amount"]["status"] == "available"


async def test_http_and_assistant_share_us_schema_and_ignore_legacy_depth(make_overview):
    repo = make_overview(pl.DataFrame({
        "symbol": ["AAPL"], "change_pct": [0.01], "amount": [100.0],
    }))
    app = FastAPI()
    app.state.repo = repo
    app.state.depth_service = PoisonDepth()
    app.include_router(api.router)
    api.invalidate_overview_cache()
    try:
        with TestClient(app) as client:
            response = client.get("/api/overview/market", params={"as_of": DAY.isoformat()})
        assert response.status_code == 200
        via_tool = await tools.execute_assistant_tool("get_market_overview", {},
            tools.ToolContext.build(repo=repo, depth_service=PoisonDepth()))
        assert via_tool["ok"] is True
        assert response.json() == via_tool["result"]
        assert response.json()["market"] == "US"
        assert response.json()["currency"] == "USD"
        assert "limit" not in response.json()
    finally:
        api.invalidate_overview_cache()


def test_explicit_date_never_uses_live_index_cache(make_overview):
    repo = make_overview(pl.DataFrame())
    queries = []

    def query(sql, params):
        queries.append(params)
        return [("SPY", DAY, 102.0, 100.0)]

    repo.execute_all = query

    class Quotes:
        def get_index_quotes(self, symbols):
            pytest.fail("历史请求不能混入实时ETF缓存")

    rows = overview._index_quotes(repo, Quotes(), DAY)
    assert queries[0][-2:] == [DAY, DAY]
    spy = next(row for row in rows if row["symbol"] == "SPY")
    assert spy["change_pct"] == 2.0  # ETF展示为百分数，不是股票小数制
    assert spy["date"] == DAY.isoformat()
    assert spy["source"] == "daily"
    assert next(row for row in rows if row["symbol"] == "QQQ")["last_price"] is None


def test_dimension_missing_amount_is_not_zero(tmp_path, monkeypatch):
    config = SimpleNamespace(id="sector", fields=[SimpleNamespace(name="sector", label="行业")],
                             symbol_map={}, code_map={})
    monkeypatch.setattr(overview.ExtConfigStore, "load_all", lambda self: [config])
    monkeypatch.setattr(overview, "_read_ext_rows", lambda *a: [{"symbol": "AAPL", "sector": "Tech"}])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    group = overview._dimension_rank([{"symbol": "AAPL", "change_pct": 0.0}], repo, "industry")
    assert group["leading"][0]["amount"] is None
