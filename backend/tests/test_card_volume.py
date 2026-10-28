"""量能等级 / 美东时段 / 盘前量比 / 盘前实时价 (0.29.1 起旧机会卡的共振评分与盘中确认已退役)。不联网: 盘前取数用桩。"""
from datetime import date, datetime
from datetime import time as dtime

import pytest

from app.market_clock import us_clock
from app.services import card_volume as cv


def test_level_bins():
    assert cv.level(2.3)["label"] == "爆量" and cv.level(1.2)["label"] == "放量"
    assert cv.level(1.0)["label"] == "平量" and cv.level(0.5)["label"] == "缩量"
    assert cv.level(None) is None and cv.level(float("nan")) is None


def test_phase_boundaries(monkeypatch):
    us_clock._calendar_provider = None
    d = date(2026, 10, 9)                                  # 周五
    at = lambda h, m: datetime.combine(d, dtime(h, m), tzinfo=us_clock.tz)   # noqa: E731
    assert cv.phase(at(3, 59))["phase"] == "night"
    assert cv.phase(at(4, 0))["phase"] == "premarket"
    assert cv.phase(at(9, 30))["phase"] == "open_wait"
    assert cv.phase(at(9, 45))["phase"] == "session"
    assert cv.phase(at(16, 0))["phase"] == "after"
    assert cv.phase(datetime.combine(date(2026, 10, 10), dtime(10, 0), tzinfo=us_clock.tz))["phase"] == "closed"


def test_premarket_ratio_vs_same_minute_of_past_10_sessions():
    """今日 = SIP 1 分钟K 增量 (只取上次截至之后); 基线 = 过去 10 日 15 分钟K, 截至时刻落在 K 中间时按时间比例计入。"""
    us_clock._calendar_provider = None
    cv._cache.clear()
    now = datetime.combine(date(2026, 10, 9), dtime(8, 52, 30), tzinfo=us_clock.tz)        # SIP 截至 08:36
    calls = []

    def fetch(symbols, start, end, tf="15Min"):
        calls.append((start, end, tf))
        d = start.date()
        out = []
        if tf == "1Min":                       # 今日: 04:00-08:35 每分钟 30 股, 收盘价随分钟递增
            t = datetime.combine(d, dtime(4, 0), tzinfo=us_clock.tz)
            while t < end:
                if t >= start:
                    out.append({"t": t.isoformat(), "v": 30.0, "c": 100.0 + t.minute / 100})
                t += __import__("datetime").timedelta(minutes=1)
        else:                                  # 历史: 每根 15 分钟K 150 股 (= 每分钟 10 股)
            t = datetime.combine(d, dtime(4, 0), tzinfo=us_clock.tz)
            while t.time() < dtime(9, 30):
                out.append({"t": t.isoformat(), "v": 150.0, "c": 100.0})
                t += __import__("datetime").timedelta(minutes=15)
        return {"NVDA": out}
    out = cv.premarket(["NVDA"], now, fetch=fetch, block=True)
    # 今日 04:00→08:36 共 276 分钟 x 30 = 8280; 基线同窗 276 x 10 = 2760 (08:30 那根按 6/15 计入) → 3.0x
    assert out["NVDA"]["ratio"] == pytest.approx(3.0) and out["NVDA"]["label"] == "爆量"
    assert out["NVDA"]["through"] == "08:36" and out["NVDA"]["last"] == pytest.approx(100.35)
    today_calls = [c for c in calls if c[2] == "1Min"]
    assert len(today_calls) == 1 and today_calls[0][1].time() == dtime(8, 36)
    n = len(calls)
    cv.premarket(["NVDA"], now, fetch=fetch, block=True)    # 1 分钟内复用缓存
    assert len(calls) == n
    # 一分钟后: 只增量取 08:34→08:37 (留 2 分钟重叠防漏), 基线不再取
    cv._cache[("pre", now.date(), ("NVDA",))] = (0.0, {}, now)          # 让结果缓存过期
    cv.premarket(["NVDA"], now + __import__("datetime").timedelta(minutes=1), fetch=fetch, block=True)
    new_calls = calls[n:]
    assert [c[2] for c in new_calls] == ["1Min"] and new_calls[0][0].time() == dtime(8, 34)
    assert cv.premarket(["NVDA"], datetime.combine(date(2026, 10, 9), dtime(3, 30), tzinfo=us_clock.tz), fetch=fetch) == {}


def test_premarket_prices_from_hyperliquid_cached_15s():
    cv._cache.clear()
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {"NVDA": 235.1, "AAPL": 0.0}
    out = cv.premarket_prices(["NVDA", "AAPL", "XYZ"], fetch=fetch, block=True)
    assert set(out) == {"NVDA"} and out["NVDA"]["price"] == 235.1 and out["NVDA"]["source"] == "HL"
    cv.premarket_prices(["NVDA", "AAPL", "XYZ"], fetch=fetch, block=True)
    assert len(calls) == 1                                  # 15 秒缓存
    boom = lambda symbols: (_ for _ in ()).throw(OSError("net"))   # noqa: E731
    cv._cache.clear()
    assert cv.premarket_prices(["NVDA"], fetch=boom, block=True) == {}  # 取不到 → 空, 不猜
