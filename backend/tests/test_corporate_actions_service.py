"""公司行动服务：Alpaca 拉取（分页、打桩）、解析、原子缓存、失败不覆盖、状态。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.market_clock import us_clock
from app.services import corporate_actions as ca

FIXTURE = json.loads((Path(__file__).parent / "fixtures_corporate_actions_alpaca.json").read_text())
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=us_clock.tz)


def test_parse_forward_reverse_dividend_and_flagged_types():
    payload = {"corporate_actions": {
        **FIXTURE["hist_2024"]["corporate_actions"],
        **FIXTURE["mergers_2025"]["corporate_actions"],
        "reverse_splits": [{"id": "rv1", "symbol": "GRML", "ex_date": "2026-10-02",
                            "new_rate": 1, "old_rate": 20, "process_date": "2026-10-02"}],
        "name_changes": [{"id": "nc1", "old_symbol": "OLDX", "new_symbol": "NEWX",
                          "process_date": "2026-10-05"}],
    }}
    acts = {a["id"]: a for a in ca.parse_actions(payload)}
    avgo = acts["ab22e5df-2b84-427e-84c7-c0e6e24c4696"]
    assert avgo["type"] == "forward_split" and avgo["symbol"] == "AVGO"
    assert avgo["factor"] == 10 and avgo["ex_date"] == "2024-07-15" and avgo["auto"] is True
    assert acts["rv1"]["type"] == "reverse_split" and acts["rv1"]["factor"] == pytest.approx(0.05)
    div = acts["0cc501cc-789c-45ef-841f-f43a4f7e7a3c"]
    assert div["type"] == "cash_dividend" and div["rate"] == 5.25 and div["special"] is False
    assert div["payable_date"] == "2024-06-28" and div["auto"] is True
    merger = acts["c42e17a3-2902-42ca-9fc6-6dc3393c9261"]
    assert merger["type"] == "cash_merger" and merger["symbol"] == "096ESC017"
    assert merger["ex_date"] == "2025-06-26" and merger["auto"] is False
    spin = acts["8231af08-24ad-4712-bb17-ee649c2d9402"]
    assert spin["type"] == "spin_off" and spin["symbol"] == "218ESC010" and spin["auto"] is False
    assert acts["nc1"]["symbol"] == "OLDX" and acts["nc1"]["ex_date"] == "2026-10-05"
    assert acts["081cb55a-d959-4e84-b79d-482b75a13374"]["symbol"] == "218ESC010"


def _paged_getter(calls):
    pages = [
        {"corporate_actions": FIXTURE["hist_2024"]["corporate_actions"], "next_page_token": "tok2"},
        {"corporate_actions": FIXTURE["mergers_2025"]["corporate_actions"], "next_page_token": None},
    ]

    def getter(url):
        calls.append(url)
        q = parse_qs(urlparse(url).query)
        return pages[1] if q.get("page_token") == ["tok2"] else pages[0]
    return getter


def test_fetch_follows_pagination_and_sends_window(tmp_path):
    calls = []
    acts = ca.fetch_actions(["AVGO", "CMG"], date(2026, 9, 21), date(2026, 10, 31), http_get=_paged_getter(calls))
    assert len(calls) == 2
    q = parse_qs(urlparse(calls[0]).query)
    assert calls[0].startswith("https://data.alpaca.markets/v1/corporate-actions?")
    assert q["symbols"] == ["AVGO,CMG"] and q["start"] == ["2026-09-21"] and q["end"] == ["2026-10-31"]
    assert parse_qs(urlparse(calls[1]).query)["page_token"] == ["tok2"]
    assert len(acts) == 5 + 5


def test_fetch_no_symbols_never_calls_source():
    def boom(url):
        raise AssertionError("must not call")
    assert ca.fetch_actions([], date(2026, 9, 21), date(2026, 10, 31), http_get=boom) == []


def test_refresh_writes_atomic_cache_and_connected_status(tmp_path):
    svc = ca.CorporateActionService(tmp_path)
    calls = []
    svc.refresh(["AVGO"], NOW, http_get=_paged_getter(calls))
    cache = tmp_path / "reference" / "corporate_actions" / "actions.json"
    data = json.loads(cache.read_text())
    assert data["fetched_at"] == NOW.isoformat() and data["symbols"] == ["AVGO"]
    assert data["window"] == {"start": (NOW.date() - timedelta(days=10)).isoformat(),
                              "end": (NOW.date() + timedelta(days=30)).isoformat()}
    assert not list(cache.parent.glob("*.tmp"))
    assert svc.status(NOW)["status"] == "connected"


def test_refresh_failure_keeps_cache_and_marks_unavailable(tmp_path):
    svc = ca.CorporateActionService(tmp_path)
    svc.refresh(["AVGO"], NOW, http_get=_paged_getter([]))
    cache = tmp_path / "reference" / "corporate_actions" / "actions.json"
    before = cache.read_bytes()

    def fail(url):
        raise OSError("network down")
    later = NOW + timedelta(hours=1)
    svc.refresh(["AVGO"], later, http_get=fail)
    assert cache.read_bytes() == before
    st = svc.status(later)
    assert st["status"] == "unavailable" and "OSError" in st["error"]
    assert st["fetched_at"] == NOW.isoformat()


def test_status_stale_when_cache_old(tmp_path):
    svc = ca.CorporateActionService(tmp_path)
    svc.refresh(["AVGO"], NOW, http_get=_paged_getter([]))
    assert svc.status(NOW + timedelta(hours=30))["status"] == "stale"


def test_status_unavailable_without_cache(tmp_path):
    assert ca.CorporateActionService(tmp_path).status(NOW)["status"] == "unavailable"
