"""Public factor requests are slippage-only; persisted legacy costs remain replayable."""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.backtest.candidates import CandidateStore
from app.backtest.factor import FactorBacktestService, FactorBatchConfig, FactorConfig


class PanelEngine:
    def load_panel(self, symbols, start, end, columns, asset_type):
        rows = [
            {"symbol": symbol, "date": date(2026, 1, 5) + timedelta(days=day),
             "close": 20.0 + rank + day * (rank + 1) * 0.1,
             "open": 20.0 + rank + day * 0.1,
             "high": 25.0 + rank + day * 0.1,
             "low": 19.0 + rank + day * 0.1,
             "volume": 10000.0,
             "change_pct": 0.01 * (rank + 1) + 0.001 * day}
            for day in range(8) for rank, symbol in enumerate(("AAPL", "NVDA", "MSFT"))
        ]
        return pl.DataFrame(rows).select([c for c in columns if c in rows[0]])


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import backtest
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = PanelEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    store = CandidateStore(tmp_path)
    monkeypatch.setattr(backtest, "_candidate_store", lambda: store)
    return TestClient(app, raise_server_exceptions=False)


def payload(mode, **costs):
    key = {"factor_name": "change_pct"} if mode == "run" else {"factor_names": ["change_pct"]}
    return {**key, "start": "2026-01-05", "end": "2026-01-12", "n_groups": 3,
            "rebalance": "daily", **costs}


@pytest.mark.parametrize("mode", ["run", "batch"])
def test_public_defaults_match_explicit_zero_commission_and_save_real_costs(client, mode):
    result = client.post(f"/api/backtest/factor/{mode}", json=payload(mode)).json()
    assert result["error"] is None
    assert result["config"]["fees_pct"] == 0.0
    assert result["config"]["slippage_bps"] == 5.0
    explicit = client.post(f"/api/backtest/factor/{mode}", json=payload(mode, fees_pct=0)).json()
    if mode == "run":
        assert result["group_nav"] == explicit["group_nav"]
        assert result["long_short_nav"] == explicit["long_short_nav"]
    else:
        assert result["results"][0]["long_short_return"] == explicit["results"][0]["long_short_return"]
    config = {k: result["config"][k] for k in ("start", "end", "fees_pct", "slippage_bps")}
    config["factor_name"] = "change_pct"
    saved = client.post("/api/backtest/candidates", json={
        "kind": "factor", "name": "cost contract", "source_id": "change_pct", "config": config,
    })
    assert saved.status_code == 200
    found = next(item for item in client.get("/api/backtest/candidates").json()["items"]
                 if item["id"] == saved.json()["id"])
    assert found["config"] == config


@pytest.mark.parametrize("mode", ["run", "batch"])
@pytest.mark.parametrize("costs", [
    {"fees_pct": 0.0002}, {"fees_pct": -0.0002},
    {"commission_pct": 0.0002}, {"stamp_tax_pct": 0.0005},
    {"slippage_bps": -1}, {"slippage_bps": "NaN"}, {"fees_pct": "NaN"},
])
def test_public_requests_reject_legacy_fees_and_invalid_slippage(client, mode, costs):
    response = client.post(f"/api/backtest/factor/{mode}", json=payload(mode, **costs))
    assert response.status_code == 422


@pytest.mark.parametrize("config_type,key", [
    (FactorConfig, {"factor_name": "change_pct"}),
    (FactorBatchConfig, {"factor_names": ["change_pct"]}),
])
def test_internal_config_defaults_are_slippage_only(config_type, key):
    config = config_type(**key, symbols=None, start=date(2026, 1, 5), end=date(2026, 1, 12))
    assert config.fees_pct == 0.0
    assert FactorBacktestService._round_trip_cost(config) == pytest.approx(0.001)


def test_internal_legacy_factor_costs_remain_replayable_and_do_not_change_ic():
    service = FactorBacktestService(PanelEngine())
    config = FactorConfig(factor_name="change_pct", symbols=None, start=date(2026, 1, 5),
                          end=date(2026, 1, 12), n_groups=3, rebalance="daily", fees_pct=0.0002)
    legacy = service.run(config)
    free = service.run(replace(config, fees_pct=0, slippage_bps=0))
    assert legacy.error is None and free.error is None
    assert legacy.ic_series == free.ic_series
    assert legacy.ic_mean == free.ic_mean
    assert legacy.group_nav != free.group_nav
    assert legacy.config["fees_pct"] == 0.0002
    assert service._round_trip_cost(config) == pytest.approx(0.0014)
    assert service._round_trip_cost(replace(config, commission_pct=0.0003, stamp_tax_pct=0.0005)) == pytest.approx(0.0021)
    # Explicit zero must override the legacy fees alias, not fall back to it.
    assert service._round_trip_cost(replace(config, commission_pct=0, stamp_tax_pct=0)) == pytest.approx(0.001)


@pytest.mark.parametrize("mode", ["run", "batch"])
def test_slippage_changes_net_returns_without_changing_ic(client, mode):
    free = client.post(f"/api/backtest/factor/{mode}", json=payload(mode, slippage_bps=0)).json()
    slipped = client.post(f"/api/backtest/factor/{mode}", json=payload(mode, slippage_bps=7)).json()
    assert free["config"]["fees_pct"] == slipped["config"]["fees_pct"] == 0
    assert free["config"]["slippage_bps"] == 0
    assert slipped["config"]["slippage_bps"] == 7
    if mode == "run":
        assert free["group_nav"] != slipped["group_nav"]
        assert free["long_short_nav"] != slipped["long_short_nav"]
        assert free["ic_series"] == slipped["ic_series"]
    else:
        before, after = free["results"][0], slipped["results"][0]
        assert before["long_short_return"] != after["long_short_return"]
        for key in ("ic_mean", "ir", "t_newey_west", "q_value"):
            assert before[key] == after[key]


def test_old_candidate_read_does_not_rewrite_its_costs(client):
    config = {"factor_name": "change_pct", "fees_pct": 0.0002, "slippage_bps": 7.0}
    saved = client.post("/api/backtest/candidates", json={
        "kind": "factor", "name": "historical", "source_id": "change_pct", "config": config,
    }).json()
    found = next(item for item in client.get("/api/backtest/candidates").json()["items"]
                 if item["id"] == saved["id"])
    assert found["config"] == config
