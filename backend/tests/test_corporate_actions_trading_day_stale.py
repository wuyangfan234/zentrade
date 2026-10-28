"""公司行动缓存新鲜度按 NYSE 交易日算 (用户 2026-10-11 批准, 取代 26 小时裸时长)。

规则: 缓存的 fetched_at 不早于「最近一个已到期的应刷新时点」就算新鲜。应刷新时点 = 交易日 08:00 ET
(daily_pipeline 的 zen_corporate_actions 定时任务); 到期 = 该时点 + 60 分钟宽限 (= 该任务
misfire_grace_time 3600s)。周末/休市日没有应刷新时点, 不会过期。缓存缺失/损坏/最近一次拉取失败仍是
unavailable (fail-closed 不变)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.services import corporate_actions as ca

pytestmark = pytest.mark.real_corporate_actions

TZ = us_clock.tz
FRI = date(2026, 10, 9)


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=TZ)


def _calendar(holidays=()):
    def provider(start, end):
        return [start + timedelta(days=i) for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) not in holidays]
    return provider


@pytest.fixture
def nyse(monkeypatch):
    def use(holidays=()):
        monkeypatch.setattr(us_clock, "_calendar_provider", _calendar(set(holidays)))
        monkeypatch.setattr(us_clock, "_cal_cache", {})
    use()
    return use


def _svc(tmp_path, fetched: datetime):
    svc = ca.CorporateActionService(tmp_path)
    svc.refresh(["AAPL"], fetched, http_get=lambda url: {"corporate_actions": {}})
    return svc


@pytest.mark.parametrize("now", [
    at(date(2026, 10, 10), 10, 1),     # 周六 (旧 26h 规则此刻已过期)
    at(date(2026, 10, 11), 20, 0),     # 周日
    at(date(2026, 10, 12), 7, 59),     # 周一 07:59
    at(date(2026, 10, 12), 8, 59),     # 周一 08:00 后宽限内 (定时任务 misfire 窗口)
])
def test_friday_refresh_stays_fresh_over_weekend(tmp_path, nyse, now):
    svc = _svc(tmp_path, at(FRI, 8, 0, 5))
    assert svc.status(now)["status"] == "connected"
    assert svc.usable(now) is True


def test_monday_without_refresh_after_grace_is_stale(tmp_path, nyse):
    svc = _svc(tmp_path, at(FRI, 8, 0, 5))
    assert svc.status(at(date(2026, 10, 12), 9, 0, 1))["status"] == "stale"
    assert svc.usable(at(date(2026, 10, 12), 9, 30)) is False


def test_monday_refresh_is_fresh_all_monday(tmp_path, nyse):
    svc = _svc(tmp_path, at(date(2026, 10, 12), 8, 0, 3))
    assert svc.status(at(date(2026, 10, 12), 15, 59))["status"] == "connected"
    assert svc.status(at(date(2026, 10, 13), 9, 1))["status"] == "stale"


def test_holiday_has_no_refresh_point(tmp_path, nyse):
    nyse(holidays={date(2026, 10, 12)})                     # 周一休市
    svc = _svc(tmp_path, at(FRI, 8, 0, 5))
    assert svc.status(at(date(2026, 10, 12), 15, 0))["status"] == "connected"
    assert svc.status(at(date(2026, 10, 13), 7, 59))["status"] == "connected"
    assert svc.status(at(date(2026, 10, 13), 9, 1))["status"] == "stale"


def test_refresh_before_0800_does_not_cover_that_days_point(tmp_path, nyse):
    svc = _svc(tmp_path, at(date(2026, 10, 12), 7, 30))     # 周一 07:30 开机补拉
    assert svc.status(at(date(2026, 10, 12), 8, 30))["status"] == "connected"
    assert svc.status(at(date(2026, 10, 12), 9, 1))["status"] == "stale"   # 08:00 定时任务没跑成


def test_missing_corrupt_or_failed_cache_is_still_unavailable(tmp_path, nyse):
    sat = at(date(2026, 10, 10), 12, 0)
    assert ca.CorporateActionService(tmp_path / "none").status(sat)["status"] == "unavailable"
    bad = ca.CorporateActionService(tmp_path / "bad")
    bad.cache_path.parent.mkdir(parents=True)
    bad.cache_path.write_text("{not json", encoding="utf-8")
    assert bad.status(sat)["status"] == "unavailable"
    svc = _svc(tmp_path / "failed", at(FRI, 8, 0, 5))

    def fail(url):
        raise OSError("down")
    svc.refresh(["AAPL"], at(FRI, 9, 0), http_get=fail)
    assert svc.status(sat)["status"] == "unavailable"


def test_status_reports_refresh_point_for_display(tmp_path, nyse):
    st = _svc(tmp_path, at(FRI, 8, 0, 5)).status(at(date(2026, 10, 12), 9, 30))
    assert st["status"] == "stale"
    assert st["due_at"] == at(date(2026, 10, 12), 8, 0).isoformat()
