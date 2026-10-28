"""实时行情轮询间隔下限契约测试 (厂商档位已退役 P1-1b2)。

下限语义: 实时源均为插件/自定义源, 通用下限 1s;
默认间隔 6s 不变 (只放宽下限, 不动存量偏好)。
"""
from __future__ import annotations

from app.services.quote_service import QuoteService


def _bare() -> QuoteService:
    """绕过 __init__ (单例/线程副作用), 只用无状态方法。"""
    return QuoteService.__new__(QuoteService)


def test_min_interval_is_1s(monkeypatch):
    """厂商档位查表已退役: 下限恒为通用 1s。"""
    assert _bare().get_min_interval() == 1.0


def test_clamp_allows_1s_and_caps_max(monkeypatch):
    qs = _bare()
    assert qs._clamp_interval(1.0) == 1.0
    assert qs._clamp_interval(0.5) == 1.0
    assert qs._clamp_interval(120.0) == QuoteService.MAX_INTERVAL


def test_default_interval_unchanged():
    """默认间隔仍是 6s — 放宽的只是下限, 不是默认值。"""
    assert QuoteService.DEFAULT_INTERVAL == 6.0
