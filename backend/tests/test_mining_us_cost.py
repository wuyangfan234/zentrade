"""Factor mining uses the US cost model: per-share commission + minimum + slippage, no stamp tax.

Historical runs recorded with commission_pct / stamp_tax_pct must still decode, project and replay
with their original legacy costs.
"""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.api.mining import MiningAutoStartRequest, MiningStartRequest, _request_summary
from app.backtest import mining_runtime as rt
from app.backtest.engine import MatcherConfig
from app.backtest.strategy import StrategyBacktestConfig
from app.services.mining_candidates import cost_fields_for_promotion


def test_new_requests_reject_stamp_tax_and_pct_commission():
    for model in (MiningStartRequest, MiningAutoStartRequest):
        with pytest.raises(ValidationError):
            model.model_validate({"factor_names": ["rsi_14"], "stamp_tax_pct": 0.0005})
        with pytest.raises(ValidationError):
            model.model_validate({"factor_names": ["rsi_14"], "commission_pct": 0.0002})


def test_new_request_defaults_are_us_per_share():
    body = MiningStartRequest.model_validate({"factor_names": ["rsi_14"]}).model_dump(mode="json")
    assert body["cost_model"] == "us_per_share_v1"
    assert body["commission_per_share"] == 0.005 and body["commission_min"] == 1.0
    assert "stamp_tax_pct" not in body and "commission_pct" not in body


def test_us_request_builds_a_us_per_share_backtest_config():
    cfg = rt.backtest_cost_kwargs({"cost_model": "us_per_share_v1", "commission_per_share": 0.01,
                                   "commission_min": 2.0, "slippage_bps": 3.0})
    backtest = StrategyBacktestConfig(strategy_id="s", symbols=None, start=date(2025, 1, 2), end=date(2025, 6, 2), **cfg)
    assert backtest.cost_model == "us_per_share_v1"
    assert backtest.stamp_tax_pct is None and backtest.commission_pct is None
    matcher = MatcherConfig(**cfg)                     # same kwargs the strategy engine forwards
    assert matcher.cost_model == "us_per_share_v1"
    assert matcher.commission(100, 50.0) == 2.0        # max(100*0.01, 2.0)
    assert matcher.sell_cost_pct() == pytest.approx(3.0 / 10000)


def test_legacy_request_keeps_its_recorded_costs():
    cfg = rt.backtest_cost_kwargs({"commission_pct": 0.0002, "stamp_tax_pct": 0.0005, "slippage_bps": 5.0})
    backtest = StrategyBacktestConfig(strategy_id="s", symbols=None, start=date(2025, 1, 2), end=date(2025, 6, 2), **cfg)
    assert backtest.stamp_tax_pct == 0.0005
    assert MatcherConfig(**cfg).cost_model == "legacy_pct"


def test_factor_round_trip_cost_has_no_stamp_tax_for_us_requests():
    us = rt.factor_cost_kwargs({"cost_model": "us_per_share_v1", "slippage_bps": 5.0})
    assert us["stamp_tax_pct"] == 0.0 and us["commission_pct"] == 0.0 and us["slippage_bps"] == 5.0
    legacy = rt.factor_cost_kwargs({"commission_pct": 0.0002, "stamp_tax_pct": 0.0005, "slippage_bps": 5.0})
    assert legacy["stamp_tax_pct"] == 0.0005 and legacy["commission_pct"] == 0.0002


def test_request_summary_projects_both_cost_models():
    us = _request_summary({"request": {"cost_model": "us_per_share_v1", "commission_per_share": 0.005,
                                       "commission_min": 1.0, "slippage_bps": 5.0}})
    assert us["cost_model"] == "us_per_share_v1" and us["commission_per_share"] == 0.005
    assert us["stamp_tax_pct"] is None
    legacy = _request_summary({"request": {"commission_pct": 0.0002, "stamp_tax_pct": 0.0005}})
    assert legacy["cost_model"] == "legacy_pct" and legacy["stamp_tax_pct"] == 0.0005


def test_promoted_strategy_inherits_the_run_cost_model():
    us = cost_fields_for_promotion({"cost_model": "us_per_share_v1", "commission_per_share": 0.005,
                                    "commission_min": 1.0, "slippage_bps": 5.0})
    assert us == {"cost_model": "us_per_share_v1", "commission_per_share": 0.005, "commission_min": 1.0,
                  "slippage_bps": 5.0}
    legacy = cost_fields_for_promotion({"commission_pct": 0.0002, "stamp_tax_pct": 0.0005, "slippage_bps": 5.0})
    assert legacy == {"commission_pct": 0.0002, "stamp_tax_pct": 0.0005, "slippage_bps": 5.0}
