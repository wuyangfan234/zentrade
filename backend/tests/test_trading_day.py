"""交易日探针 (trading_day oracle) 与两个消费方接入的测试。

不依赖真实网络: 探测函数 (_probe_us_calendar) 全部 monkeypatch。
覆盖: 周末零成本直判、Alpaca 日历权威、未知 → None、TTL 缓存、
实时行情门控与分钟增量 gate_reason 的 holiday 分支。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.services import trading_day
from app.services.trading_day import is_trading_day, reset_cache

CN = timezone(timedelta(hours=-4))  # 美东夏令时 (UTC-4)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    reset_cache()


def _no_probes(monkeypatch):
    """探测函数替换为爆炸 — 用于验证未被打到。"""
    monkeypatch.setattr(trading_day, "_probe_us_calendar", lambda now: (_ for _ in ()).throw(AssertionError("不应探测")))


# ---- 周末零成本直判 ----

def test_weekend_returns_false_without_probing(monkeypatch):
    sat = datetime(2026, 8, 29, 10, 0, tzinfo=CN)  # 周六
    sun = datetime(2026, 8, 30, 10, 0, tzinfo=CN)  # 周日
    _no_probes(monkeypatch)
    assert is_trading_day(sat) is False
    assert is_trading_day(sun) is False


# ---- 探测语义 (Alpaca 日历单探针) ----

def test_us_calendar_is_authoritative_even_before_open(monkeypatch):
    """Alpaca 日历结论无时段依赖: 开盘缓冲窗内也直接生效。"""
    holiday_mon = datetime(2026, 9, 7, 9, 31, tzinfo=CN)  # 周一 (缓冲窗内)
    monkeypatch.setattr(trading_day, "_probe_us_calendar", lambda now: False)
    assert is_trading_day(holiday_mon) is False


def test_us_calendar_unknown_returns_none(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    monkeypatch.setattr(trading_day, "_probe_us_calendar", lambda now: None)
    assert is_trading_day(monday) is None


def test_us_calendar_true_is_trading(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    monkeypatch.setattr(trading_day, "_probe_us_calendar", lambda now: True)
    assert is_trading_day(monday) is True


# ---- TTL 缓存 ----

def test_verdict_cached_within_ttl(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"n": 0}

    def _counting_probe(now):
        calls["n"] += 1
        return True

    monkeypatch.setattr(trading_day, "_probe_us_calendar", _counting_probe)
    assert is_trading_day(monday) is True
    assert is_trading_day(monday) is True
    assert is_trading_day(monday) is True
    assert calls["n"] == 1  # 命中缓存, 只探一次


def test_unknown_verdict_retries_after_short_ttl(monkeypatch):
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"n": 0}

    def _counting_probe(now):
        calls["n"] += 1
        return None

    monkeypatch.setattr(trading_day, "_probe_us_calendar", _counting_probe)
    assert is_trading_day(monday) is None
    # 手动把缓存时间拨回 10 分钟前 (超过 unknown TTL 300s) → 重探
    with trading_day._CACHE_LOCK:
        trading_day._CACHE.probed_at -= 600
    assert is_trading_day(monday) is None
    assert calls["n"] == 2


def test_unknown_verdict_is_cached_within_short_ttl(monkeypatch):
    """未知结论也要按 _TTL_UNKNOWN_S 缓存: 轮询每拍重探会重复打请求。"""
    monday = datetime(2026, 9, 7, 10, 0, tzinfo=CN)
    calls = {"cal": 0}

    def _cal(now):
        calls["cal"] += 1
        return None

    monkeypatch.setattr(trading_day, "_probe_us_calendar", _cal)

    assert is_trading_day(monday) is None
    assert is_trading_day(monday) is None
    assert is_trading_day(monday) is None
    assert calls == {"cal": 1}


# ---- 消费方 1: 实时行情门控 ----

def test_quote_service_holiday_gate_blocks_polling(monkeypatch):
    from app.services.quote_service import QuoteService

    qs = QuoteService()
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: False)
    assert qs._should_poll_for_phase("regular") is False
    assert qs._should_poll_for_phase("closed") is False
    # final 定版同样被剔除: 休市日没有需要定版的当日行情
    assert qs._should_poll_for_phase("close_final") is False


def test_quote_service_polls_when_trading_or_unknown(monkeypatch):
    from app.services.quote_service import QuoteService

    qs = QuoteService()
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    assert qs._should_poll_for_phase("regular") is True
    # 未知 → 维持现状 (周几近似)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: None)
    assert qs._should_poll_for_phase("regular") is True


# ---- 消费方 2: 分钟增量 gate_reason ----

def _minute_service(monkeypatch):
    from app.services.minute_refresh import MinuteRefreshService

    svc = MinuteRefreshService.__new__(MinuteRefreshService)
    svc._app_state = None
    monkeypatch.setattr(
        "app.services.minute_refresh.preferences.get_minute_refresh_enabled",
        lambda: True,
    )
    monkeypatch.setattr(svc, "capability_ok", lambda: True)
    return svc


def test_minute_refresh_gate_returns_holiday(monkeypatch):
    """周几+时段门控放行 (周一盘中) 但探针判休市 → holiday。"""
    svc = _minute_service(monkeypatch)
    monkeypatch.setattr(
        "app.services.minute_refresh._in_continuous_session", lambda now=None: True
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: False)
    assert svc._gate_reason() == "holiday"


def test_minute_refresh_gate_passes_when_trading(monkeypatch):
    svc = _minute_service(monkeypatch)
    monkeypatch.setattr(
        "app.services.minute_refresh._in_continuous_session", lambda now=None: True
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    assert svc._gate_reason() is None


# ---- BH-06: 工作日休市日必须判成 False (真实 _probe_us_calendar, 日历打桩) ----

def _synthetic_calendar(monkeypatch, holidays: set):
    """2026-11 ~ 2027-01 的合成交易日历: 工作日减去 holidays。按 [start, end] 过滤, 与生产入口同语义。"""
    from datetime import date as _date

    from app.market_clock import calendar as cal

    all_days = []
    d = _date(2026, 10, 1)
    while d <= _date(2027, 1, 31):
        if d.weekday() < 5 and d not in holidays:
            all_days.append(d)
        d += timedelta(days=1)

    def _fake(start, end):
        return [x for x in all_days if start <= x <= end]

    monkeypatch.setattr(cal, "alpaca_trading_days", _fake)


def test_bh06_weekday_holiday_returns_false(monkeypatch):
    from datetime import date as _date

    _synthetic_calendar(monkeypatch, {_date(2026, 11, 26), _date(2026, 12, 25)})
    thanksgiving = datetime(2026, 11, 26, 10, 0, tzinfo=CN)  # 周四
    christmas = datetime(2026, 12, 25, 10, 0, tzinfo=CN)     # 周五
    assert is_trading_day(thanksgiving) is False
    reset_cache()
    assert is_trading_day(christmas) is False
    reset_cache()
    assert is_trading_day(datetime(2026, 11, 25, 10, 0, tzinfo=CN)) is True


def test_bh06_empty_calendar_is_unknown_not_holiday(monkeypatch):
    """日历拉取「成功」但附近一个交易日都没有 = 数据缺失, 不能当休市 (否则交易日停轮询)。"""
    from app.market_clock import calendar as cal

    monkeypatch.setattr(cal, "alpaca_trading_days", lambda start, end: [])
    assert is_trading_day(datetime(2026, 11, 25, 10, 0, tzinfo=CN)) is None


def test_bh06_calendar_error_is_unknown(monkeypatch):
    from app.market_clock import calendar as cal

    def _boom(start, end):
        raise RuntimeError("alpaca credentials not configured")

    monkeypatch.setattr(cal, "alpaca_trading_days", _boom)
    assert is_trading_day(datetime(2026, 11, 26, 10, 0, tzinfo=CN)) is None
