"""Degradation wiring: weekly job evaluates + notifies; API lists and manually restores."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.market_clock import us_clock
from app.services import setup_status as ss


def _seed_losing_trips(tmp_path, n=10):
    from app.ledger.core import Account, Ledger
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    day = date(2026, 9, 21)
    for i in range(n):
        sym = f"S{i}"
        lg.append("fill", {"symbol": sym, "side": "buy", "qty": 10, "price": 100.0, "fee": 1.0, "card_id": f"c{i}",
                           "setup_type": "breakout", "risk_per_share": 5.0, "trade_date": day.isoformat()},
                  at=f"{day}T10:00:00-04:00")
        lg.append("close", {"symbol": sym, "qty": 10, "price": 95.0, "fee": 1.0, "card_id": f"c{i}",
                            "reason": "invalidation", "trade_date": (day + timedelta(days=2)).isoformat()},
                  at=f"{day + timedelta(days=2)}T09:31:00-04:00")


def test_weekly_job_degrades_losing_setup_and_enqueues_notice(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline as dp
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    _seed_losing_trips(tmp_path)
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 9, 27))
    queued = []
    from app.services import notify_opportunity
    monkeypatch.setattr(notify_opportunity, "enqueue_weekly_review", lambda d, rep: True)
    monkeypatch.setattr(notify_opportunity, "enqueue_setup_degraded", lambda d, setups, st: queued.append(setups) or True)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    sched = dp.start_scheduler(state.repo, MagicMock())
    sched.get_job("zen_weekly_review").func()
    assert ss.SetupStatusStore(tmp_path).is_degraded("breakout")
    assert queued == [["breakout"]]


def _request(tmp_path):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))


def test_api_lists_and_restores_a_degraded_setup(tmp_path, monkeypatch):
    from app.api import trading
    now = datetime(2026, 9, 29, 10, tzinfo=us_clock.tz)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    ss.SetupStatusStore(tmp_path).degrade("breakout", {"samples": 11, "avg_r": -0.3}, at="2026-09-27T09:00:00-04:00")
    listed = trading.setup_status(_request(tmp_path))
    assert {s["setup"]: s["status"] for s in listed["setups"]} == {"breakout": "degraded", "pullback": "active"}
    result = trading.restore_setup("breakout", trading.RestoreBody(note="人工复核"), _request(tmp_path))
    assert result["setup"]["status"] == "active"
    assert not ss.SetupStatusStore(tmp_path).is_degraded("breakout")
    with pytest.raises(HTTPException) as err:
        trading.restore_setup("unknown", trading.RestoreBody(), _request(tmp_path))
    assert err.value.status_code == 404
