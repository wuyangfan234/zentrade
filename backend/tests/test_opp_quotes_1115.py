"""Opportunity page price rulers: GET /api/opportunities/quotes (read-only live prices)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import opp_page
from app.services import swing_desk as sd

FRI = datetime(2026, 11, 13, 11, 20, tzinfo=us_clock.tz)      # trading day, in session
SUN = datetime(2026, 11, 15, 10, 0, tzinfo=us_clock.tz)       # weekend


def _client(tmp_path, now, monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    from app.api.opportunities import router
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    seen: dict = {}

    def fake_live(repo, syms, today, now=None):
        seen["syms"] = set(syms)
        seen["today"] = today
        book = {"MU": (109.6, "live"), "LLY": (1443.5, "snapshot"), "NBIS": (115.88, "premarket")}
        return {s: v for s, v in book.items() if s in syms}

    monkeypatch.setattr(sd, "_live_prices", fake_live)
    monkeypatch.setattr(opp_page, "_prev_closes", lambda repo, syms, today: {"MU": 108.46, "LLY": 1449.0})
    return seen


def test_quotes_return_price_source_and_change_for_known_symbols(tmp_path, monkeypatch, calls):
    r = _client(tmp_path, FRI, monkeypatch).get("/api/opportunities/quotes", params={"symbols": "mu, LLY,NBIS,ZZZZ"})
    assert r.status_code == 200
    body = r.json()
    assert body["contract"] == "opp-quotes-v1"
    assert body["at"].startswith("2026-11-13T11:20")
    q = body["quotes"]
    assert set(q) == {"MU", "LLY", "NBIS"}                        # no price -> omitted, page keeps its own value
    assert q["MU"] == {"price": 109.6, "src": "live", "chg": 1.05}
    assert q["LLY"]["src"] == "snapshot" and q["LLY"]["chg"] == -0.38
    assert q["NBIS"]["chg"] is None                                # no previous close -> no change, not 0
    assert calls["syms"] == {"MU", "LLY", "NBIS", "ZZZZ"}


def test_quotes_reject_bad_symbols_and_cap_the_list(tmp_path, monkeypatch, calls):
    many = ",".join(f"S{i:02d}" for i in range(80))
    c = _client(tmp_path, FRI, monkeypatch)
    c.get("/api/opportunities/quotes", params={"symbols": f"{many},../etc,a b,$X,1AB"})
    assert len(calls["syms"]) == 60
    assert all(s.startswith("S") for s in calls["syms"])


def test_quotes_are_empty_on_non_trading_days_and_for_empty_input(tmp_path, monkeypatch, calls):
    r = _client(tmp_path, SUN, monkeypatch).get("/api/opportunities/quotes", params={"symbols": "MU"})
    assert r.json()["quotes"] == {}
    assert "syms" not in calls                                     # no price read on weekends
    r = _client(tmp_path, FRI, monkeypatch).get("/api/opportunities/quotes")
    assert r.json()["quotes"] == {}


def test_quotes_survive_previous_close_failure(tmp_path, monkeypatch, calls):
    def boom(repo, syms, today):
        raise RuntimeError("daily store unavailable")

    monkeypatch.setattr(opp_page, "_prev_closes", boom)
    q = _client(tmp_path, FRI, monkeypatch).get("/api/opportunities/quotes", params={"symbols": "MU"}).json()["quotes"]
    assert q == {"MU": {"price": 109.6, "src": "live", "chg": None}}
