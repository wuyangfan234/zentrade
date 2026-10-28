"""Report #1: cache age and session membership must use the same ET instant."""
from datetime import UTC, datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.services import qveris_data as qd


@pytest.mark.parametrize("day", ["2026-01-05", "2026-07-06"], ids=["EST", "EDT"])
@pytest.mark.parametrize("clock", ["UTC", "ET", "naive_ET", "default"])
@pytest.mark.parametrize("age_seconds, fresh", [(899, True), (900, False), (1200, False)])
def test_afternoon_options_ttl(day, clock, age_seconds, fresh, monkeypatch):
    now = datetime.fromisoformat(f"{day}T14:00:00").replace(tzinfo=us_clock.tz)
    entry = {"fetched_ts": (now - timedelta(seconds=age_seconds)).timestamp()}

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(qd, "datetime", FrozenDatetime)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    supplied = {"UTC": now.astimezone(UTC), "ET": now,
                "naive_ET": now.replace(tzinfo=None), "default": None}[clock]
    assert qd.options_fresh(entry, now=supplied) is fresh


@pytest.mark.parametrize("day", ["2026-01-05", "2026-07-06"], ids=["EST", "EDT"])
def test_preopen_cache_cannot_cross_open_even_when_under_ttl(day):
    now = datetime.fromisoformat(f"{day}T09:31:00").replace(tzinfo=us_clock.tz)
    fetched = now.replace(hour=9, minute=29)
    assert not qd.options_fresh({"fetched_ts": fetched.timestamp()}, now=now.astimezone(UTC))


@pytest.mark.parametrize("day", ["2026-01-06", "2026-07-07"], ids=["EST", "EDT"])
def test_previous_close_cache_survives_until_next_et_open(day):
    now = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=us_clock.tz)
    fetched = (now - timedelta(days=1)).replace(hour=16, minute=30)
    assert qd.options_fresh({"fetched_ts": fetched.timestamp()}, now=now.astimezone(UTC))


def test_postclose_and_weekend_cache_uses_no_intraday_ttl():
    fetched = datetime(2026, 10, 2, 16, 30, tzinfo=us_clock.tz)
    entry = {"fetched_ts": fetched.timestamp()}
    for now in (fetched + timedelta(hours=1), fetched + timedelta(days=1)):
        assert qd.options_fresh(entry, now=now.astimezone(UTC))
