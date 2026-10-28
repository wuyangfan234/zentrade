"""财报日历缓存新鲜度按 NYSE 交易日算 (用户 2026-10-11 批准「2 执行」, 取代 36 小时裸时长)。

旧规则「距上次成功刷新超过 36 小时 → stale」在周末必然过期: 周五 17:00 ET 刷新, 周日 05:00 ET 起就
报 stale, 财报保护整个周末显示不可用。新规则同公司行动 (5ab12d0, 复用 corporate_actions 的时点函数):
应刷新时点 = 交易日 08:30 与 17:00 ET (daily_pipeline 的 zen_earnings_premarket / zen_earnings_postclose,
cron mon-fri); 到期 = 时点 + 60 分钟宽限 (= 两个任务的 misfire_grace_time 3600s)。缓存 fetched_at 不早于
「最近一个已到期的应刷新时点」即新鲜; 周末/休市日没有应刷新时点, 不过期。缓存缺失/损坏/无时间/
时间解析失败/最近一次刷新失败仍是 unavailable (fail-closed 不变)。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.services import earnings_calendar as ec

pytestmark = pytest.mark.real_earnings_calendar

TZ = us_clock.tz
THU, FRI, SAT, SUN, MON, TUE = (date(2026, 10, d) for d in (8, 9, 10, 11, 12, 13))


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=TZ)


@pytest.fixture
def nyse(monkeypatch):
    def use(holidays=()):
        hol = set(holidays)

        def provider(start, end):
            return [start + timedelta(days=i) for i in range((end - start).days + 1)
                    if (start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) not in hol]
        monkeypatch.setattr(us_clock, "_calendar_provider", provider)
        monkeypatch.setattr(us_clock, "_cal_cache", {})
    use()
    return use


def _payload(url, headers=None):
    return {"data": {"rows": [{"symbol": "MU", "time": "time-after-hours"}]}, "status": {"rCode": 200}}


def _refreshed(tmp_path, when):
    svc = ec.EarningsCalendar(tmp_path)
    assert svc.refresh(when, nasdaq_get=_payload, use_finnhub=False)["fetched_at"]
    return svc


def test_friday_postclose_refresh_stays_fresh_over_weekend_until_monday_premarket(tmp_path, nyse):
    svc = _refreshed(tmp_path, at(FRI, 17, 0, 5))
    for now in (at(SAT, 12), at(SUN, 6), at(SUN, 23, 59), at(MON, 8, 29), at(MON, 9, 29)):
        assert svc.status(now)["status"] == "connected", now
        assert svc.usable(now)


def test_monday_premarket_refresh_missed_beyond_grace_is_stale(tmp_path, nyse):
    svc = _refreshed(tmp_path, at(FRI, 17, 0, 5))
    st = svc.status(at(MON, 9, 31))
    assert st["status"] == "stale" and not svc.usable(at(MON, 9, 31))
    assert st["due_at"] == at(MON, 8, 30).isoformat()


def test_missed_postclose_refresh_is_stale_after_grace(tmp_path, nyse):
    svc = _refreshed(tmp_path, at(FRI, 8, 30, 5))
    assert svc.status(at(FRI, 17, 59))["status"] == "connected"
    assert svc.status(at(FRI, 18, 1))["status"] == "stale"
    assert svc.status(at(SAT, 12))["status"] == "stale"      # 周五盘后那次没成功, 周末也不会自愈


def test_holiday_monday_does_not_expire(tmp_path, nyse):
    nyse(holidays={MON})
    svc = _refreshed(tmp_path, at(FRI, 17, 0, 5))
    assert svc.status(at(MON, 12))["status"] == "connected"
    assert svc.status(at(MON, 18, 30))["status"] == "connected"
    assert svc.status(at(TUE, 9, 31))["status"] == "stale"


def test_missing_corrupt_or_timeless_cache_is_unavailable(tmp_path, nyse):
    now = at(SAT, 12)
    assert ec.EarningsCalendar(tmp_path / "none").status(now)["status"] == "unavailable"
    svc = _refreshed(tmp_path, at(FRI, 17, 0, 5))
    cache = tmp_path / "reference" / "earnings" / "calendar.json"
    good = json.loads(cache.read_text())
    cache.write_text("{not json")
    assert svc.status(now)["status"] == "unavailable" and not svc.usable(now)
    cache.write_text(json.dumps({**good, "fetched_at": None}))
    assert svc.status(now)["status"] == "unavailable"
    cache.write_text(json.dumps({**good, "fetched_at": "garbage"}))
    assert svc.status(now)["status"] == "unavailable"


def test_last_refresh_failure_still_reports_unavailable(tmp_path, nyse):
    svc = _refreshed(tmp_path, at(FRI, 17, 0, 5))

    def down(url, headers=None):
        raise RuntimeError("down")
    svc.refresh(at(FRI, 17, 30), nasdaq_get=down, use_finnhub=False)
    assert svc.status(at(SAT, 12))["status"] == "unavailable"


def test_due_point_reuses_corporate_action_helper_and_corp_behaviour_unchanged(nyse):
    from app.services import corporate_actions as ca
    assert ca.refresh_due_point(at(MON, 8, 59)) == at(FRI, 8, 0)
    assert ca.refresh_due_point(at(MON, 9, 0)) == at(MON, 8, 0)
    assert ec.refresh_due_point(at(SUN, 12)) == at(FRI, 17, 0)
    assert ec.refresh_due_point(at(THU, 18, 0)) == at(THU, 18, 0) - timedelta(hours=1)
    assert ec.refresh_due_point(at(THU, 17, 59)) == at(THU, 8, 30)
    assert ec.refresh_due_point(at(THU, 9, 29)) == at(date(2026, 10, 7), 17, 0)
