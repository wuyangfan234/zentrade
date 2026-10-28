"""Scheduled runs explicitly opt into US costs; omitted old models stay legacy."""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.api.mining import MiningStartRequest, _request_summary
from app.backtest import mining_runtime as rt
from app.backtest.factor import FACTOR_COLUMNS
from app.backtest.engine import MatcherConfig
from app.services import mining_pool, mining_schedule
from app.services.mining_candidates import cost_fields_for_promotion


@pytest.fixture
def runtime_dates(monkeypatch):
    dates = [date(2022, 1, 3) + timedelta(days=i) for i in range(1500)]
    monkeypatch.setattr(rt, "enriched_partition_dates", lambda *_args: dates)
    return dates


@pytest.fixture(params=["balanced", "strict"])
def scheduled_request(tmp_path, monkeypatch, runtime_dates, request):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           latest_enriched_date=lambda _asset: runtime_dates[-1])
    monkeypatch.setattr(mining_pool, "build_mining_pool", lambda *_args: mining_pool.MiningPool(
        symbols=["AAPL", "NVDA"], max_symbols=200, by_source={"watchlist": 2}))
    return mining_schedule.build_default_request(repo, request.param)


def test_schedule_matches_manual_costs_through_runtime_and_promotion(scheduled_request, tmp_path):
    manual = MiningStartRequest(factor_names=["change_pct"]).model_dump(mode="json")
    keys = ("cost_model", "commission_per_share", "commission_min", "slippage_bps")
    expected = {k: manual[k] for k in keys}
    assert {k: scheduled_request.get(k) for k in keys} == expected
    decoded = rt._decode_runtime_request({"run_id": "weekly-test", "request": scheduled_request}, tmp_path, SimpleNamespace())
    assert decoded.costs == expected
    assert decoded.commission_pct == decoded.stamp_tax_pct == 0
    assert cost_fields_for_promotion(scheduled_request) == expected
    matcher = MatcherConfig(**rt.backtest_cost_kwargs(decoded.costs))
    assert matcher.cost_model == "us_per_share_v1"
    assert matcher.commission(100, 50) == 1
    assert matcher.sell_cost_pct() == pytest.approx(0.0005)
    summary = _request_summary({"request": scheduled_request})
    assert {k: summary[k] for k in keys} == expected
    assert summary["commission_pct"] is summary["stamp_tax_pct"] is None


def test_schedule_filters_original_first48_without_backfilling(scheduled_request):
    original = [item["id"] for item in FACTOR_COLUMNS[:48]]
    excluded = {"turnover_rate", "turnover_ratio_5d"}
    assert excluded <= set(original)
    assert scheduled_request["factor_names"] == [name for name in original if name not in excluded]
    assert len(scheduled_request["factor_names"]) == 46
    assert scheduled_request["symbols"] == ["AAPL", "NVDA"]


@pytest.mark.parametrize("costs,expected", [
    ({}, {"commission_pct": 0.0002, "stamp_tax_pct": 0.0005, "slippage_bps": 5.0}),
    ({"commission_pct": 0, "stamp_tax_pct": 0, "slippage_bps": 0},
     {"commission_pct": 0, "stamp_tax_pct": 0, "slippage_bps": 0}),
    ({"commission_pct": 0.0003, "slippage_bps": 7},
     {"commission_pct": 0.0003, "stamp_tax_pct": 0.0005, "slippage_bps": 7}),
])
def test_missing_historical_model_has_same_summary_replay_and_promotion_costs(
    tmp_path, runtime_dates, costs, expected,
):
    request = {"factor_names": ["change_pct"], "budget_profile": "balanced", **costs}
    manifest = {"run_id": "old-weekly", "request": request}
    before = deepcopy(manifest)
    decoded = rt._decode_runtime_request(manifest, tmp_path, SimpleNamespace())
    assert decoded.costs == expected
    assert cost_fields_for_promotion(request) == expected
    summary = _request_summary(manifest)
    assert summary["cost_model"] == "legacy_pct"
    assert {k: summary[k] for k in expected} == expected
    assert summary["commission_per_share"] is summary["commission_min"] is None
    matcher = MatcherConfig(**rt.backtest_cost_kwargs(decoded.costs))
    assert matcher.cost_model == "legacy_pct"
    assert manifest == before


def test_summary_uses_actual_us_defaults_when_only_model_was_saved():
    summary = _request_summary({"request": {"cost_model": "us_per_share_v1"}})
    assert summary["commission_per_share"] == 0.005
    assert summary["commission_min"] == 1
    assert summary["slippage_bps"] == 5
    assert summary["commission_pct"] is summary["stamp_tax_pct"] is None
