"""Report #7: a corrupt weights timestamp cannot abort the daily desk scan."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.opportunity.engine import ScanResult
from app.services import decision_scan_job as job

AS_OF = date(2026, 10, 2)


def _write(tmp_path, content):
    path = tmp_path / "decision_desk" / "weights.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2026-02-30", 123, [], {}])
def test_invalid_as_of_is_discarded_and_refresh_due(tmp_path, stamp):
    _write(tmp_path, {"as_of": stamp, "weights": {"old": 0.5}})
    assert job._previous_weights(tmp_path, AS_OF) == (None, True)


@pytest.mark.parametrize("content", [None, [], "wrong-root", {"weights": {"old": 0.5}}])
def test_missing_as_of_is_not_a_usable_previous_snapshot(tmp_path, content):
    _write(tmp_path, content)
    assert job._previous_weights(tmp_path, AS_OF) == (None, True)


@pytest.mark.parametrize("age, due", [(0, False), (6, False), (7, True), (8, True)])
def test_valid_weekly_weight_refresh_boundary_is_unchanged(tmp_path, age, due):
    weights = {"good": 1.0, "bad": 0.0}
    _write(tmp_path, {"as_of": (AS_OF - timedelta(days=age)).isoformat(), "weights": weights})
    assert job._previous_weights(tmp_path, AS_OF) == (weights, due)


def test_missing_weights_file_still_requests_refresh(tmp_path):
    assert job._previous_weights(tmp_path, AS_OF) == (None, True)


@pytest.mark.parametrize("stamp", [None, "not-a-date"])
def test_scan_recovers_corrupt_as_of_and_publishes_refreshed_weights(tmp_path, monkeypatch, stamp):
    weights_file = _write(tmp_path, {"as_of": stamp, "weights": {"stale": 99.0}})
    monkeypatch.setattr(job, "strategy_picks", lambda *a: ({}, {}, {}))
    monkeypatch.setattr(job, "_phantom_4h_picks", lambda *a, **k: {})
    monkeypatch.setattr(job, "_watchlist", set)
    monkeypatch.setattr(job, "_news_shadow_snapshot", lambda *a: None)
    opp = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), sector_map=dict)
    params = {"revision": 3,
              "portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2, "max_portfolio_risk_pct": 6.0},
              "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}}
    result = job.run(opp, AS_OF, s1s2=ScanResult(AS_OF, gate="green", params_revision=3),
                     frames={}, params=params, excluded=set(), risk_used_pct=0.0)
    saved = json.loads(weights_file.read_text(encoding="utf-8"))
    assert saved["as_of"] == AS_OF.isoformat()
    assert "stale" not in saved["weights"]
    assert result.summary["weights_refreshed"] is True
    assert job.latest_run(tmp_path)["summary"]["weights_refreshed"] is True
