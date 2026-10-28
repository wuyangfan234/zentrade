"""Earnings calendar: Nasdaq primary, Finnhub fallback, atomic cache, staleness."""
import json
from datetime import date, datetime, timedelta

import pytest

pytestmark = pytest.mark.real_earnings_calendar

from app.market_clock import us_clock
from app.services import earnings_calendar as ec

NOW = datetime(2026, 9, 30, 8, 30, tzinfo=us_clock.tz)


@pytest.fixture(autouse=True)
def weekday_calendar(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def nasdaq_payload(day: str):
    rows = {"2026-09-30": [{"symbol": "MU", "time": "time-after-hours"}, {"symbol": "JBL", "time": "time-pre-market"}],
            "2026-10-01": [{"symbol": "NKE", "time": "time-after-hours"}, {"symbol": "ACN", "time": "time-pre-market"},
                           {"symbol": "HTLM", "time": "time-not-supplied"}]}.get(day)
    return {"data": {"asOf": day, "rows": rows}, "status": {"rCode": 200}}


def test_nasdaq_rows_are_parsed_with_timing_and_weekends_skipped():
    seen = []

    def http(url, headers=None):
        seen.append(url)
        return nasdaq_payload(url.rsplit("=", 1)[1])
    rows = ec.fetch_nasdaq(date(2026, 9, 30), date(2026, 10, 3), http_get=http)
    assert {(r["symbol"], r["date"], r["timing"]) for r in rows} == {
        ("MU", "2026-09-30", "amc"), ("JBL", "2026-09-30", "bmo"),
        ("NKE", "2026-10-01", "amc"), ("ACN", "2026-10-01", "bmo"), ("HTLM", "2026-10-01", "unknown")}
    assert not any("2026-10-03" in u for u in seen), "Saturday must not be requested"


def test_finnhub_is_used_only_when_nasdaq_fails(tmp_path):
    def nasdaq_down(url, headers=None):
        raise RuntimeError("nasdaq 503")

    def finnhub(url, headers=None):
        return {"earningsCalendar": [{"symbol": "MU", "date": "2026-09-30", "hour": "amc"},
                                     {"symbol": "XYZ", "date": "2026-10-01", "hour": ""}]}
    svc = ec.EarningsCalendar(tmp_path)
    status = svc.refresh(NOW, nasdaq_get=nasdaq_down, finnhub_get=finnhub, finnhub_token="t")
    assert status["status"] == "connected" and status["source"] == "finnhub"
    assert svc.event_for("MU") == {"date": "2026-09-30", "timing": "amc"}
    assert svc.event_for("XYZ") == {"date": "2026-10-01", "timing": "unknown"}


def test_both_sources_failing_keeps_last_good_cache_and_reports_unavailable(tmp_path):
    svc = ec.EarningsCalendar(tmp_path)
    svc.refresh(NOW, nasdaq_get=lambda u, headers=None: nasdaq_payload(u.rsplit("=", 1)[1]), use_finnhub=False)
    before = (tmp_path / "reference" / "earnings" / "calendar.json").read_bytes()

    def down(url, headers=None):
        raise RuntimeError("down")
    status = svc.refresh(NOW + timedelta(hours=1), nasdaq_get=down, finnhub_get=down, finnhub_token="t")
    assert (tmp_path / "reference" / "earnings" / "calendar.json").read_bytes() == before
    assert status["status"] == "unavailable" and status["error"]


def test_status_goes_stale_when_next_scheduled_refresh_is_missed(tmp_path):
    # 10-11 起按交易日应刷新时点判定 (08:30 / 17:00 ET + 60 分钟宽限), 详见 test_earnings_calendar_trading_day_stale
    svc = ec.EarningsCalendar(tmp_path)
    svc.refresh(NOW, nasdaq_get=lambda u, headers=None: nasdaq_payload(u.rsplit("=", 1)[1]), use_finnhub=False)
    assert svc.status(NOW + timedelta(hours=9, minutes=29))["status"] == "connected"   # 17:59, 盘后刷新宽限内
    assert svc.status(NOW + timedelta(hours=9, minutes=31))["status"] == "stale"       # 18:01, 17:00 那次没成功
    assert ec.EarningsCalendar(tmp_path / "empty").status(NOW)["status"] == "unavailable"


def test_finnhub_token_is_never_written_to_disk(tmp_path):
    def finnhub(url, headers=None):
        return {"earningsCalendar": []}

    def down(url, headers=None):
        raise RuntimeError("down")
    ec.EarningsCalendar(tmp_path).refresh(NOW, nasdaq_get=down, finnhub_get=finnhub, finnhub_token="SECRET-TOKEN")
    for p in (tmp_path / "reference" / "earnings").iterdir():
        assert "SECRET-TOKEN" not in p.read_text()
