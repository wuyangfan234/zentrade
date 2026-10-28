"""审计 1026 回归: 分时信号注入调用 kline_sync.fetch_intraday_monitor_batch 时签名必须匹配。

原 bug: quote_service._inject_intraday_signals 以 now= 关键字调用, 而真实函数签名是
(symbols, capset, source), 抛 TypeError 被 _evaluate_monitors 外层吞掉, 整轮监控规则都不评估。
这里用真实函数 (不打桩), 只把能力门控打开。
"""
from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.services import kline_sync
from app.services.quote_service import QuoteService


def _service(monkeypatch) -> QuoteService:
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 26, 10, 30, tzinfo=us_clock.tz))
    monkeypatch.setattr(
        kline_sync, "intraday_monitor_support",
        lambda capset: {"available": True, "max_symbols": 100, "source": "custom_minute"},
    )
    svc = QuoteService.__new__(QuoteService)
    svc._intraday_signal_bucket = {}
    svc._repo = None
    svc._app_state = SimpleNamespace(capabilities=None, minute_refresh=None)
    return svc


def test_fetch_intraday_monitor_batch_accepts_call_site_kwargs():
    sig = inspect.signature(kline_sync.fetch_intraday_monitor_batch)
    sig.bind(["AAPL"], None, now=datetime(2026, 10, 26, 10, 30))  # 调用侧形参不再 TypeError
    assert kline_sync.fetch_intraday_monitor_batch(["AAPL"], None, now=None).is_empty()


def test_inject_intraday_signals_uses_real_fetch_without_typeerror(monkeypatch):
    svc = _service(monkeypatch)
    captured: list[pl.DataFrame] = []
    svc._intraday_signal_evaluator = SimpleNamespace(
        evaluate=lambda minute_df, **kw: (captured.append(minute_df), [])[1],
        inject=lambda df, signals: df,
    )
    svc._load_intraday_signal_defs = lambda: []
    engine = SimpleNamespace(intraday_signal_symbols=lambda asset: {"AAPL"})
    enriched = pl.DataFrame({"symbol": ["AAPL"], "close": [100.0], "prev_close": [99.0]})

    out = svc._inject_intraday_signals(enriched, engine, "stock")

    assert out.equals(enriched)
    assert captured and captured[0].is_empty()  # 退役源返回空表, 按「本轮无数据」降级


def test_inject_intraday_signals_skips_when_fetch_raises(monkeypatch):
    """拉取异常只跳过分时信号, 不向外抛 (否则整轮监控作废)。"""
    svc = _service(monkeypatch)
    injected: list[list] = []
    svc._intraday_signal_evaluator = SimpleNamespace(
        evaluate=lambda *a, **k: pytest.fail("拉取失败后不应评估"),
        inject=lambda df, signals: (injected.append(signals), df)[1],
    )

    def boom(*a, **k):
        raise TypeError("signature drift")

    monkeypatch.setattr(kline_sync, "fetch_intraday_monitor_batch", boom)
    engine = SimpleNamespace(intraday_signal_symbols=lambda asset: {"AAPL"})
    enriched = pl.DataFrame({"symbol": ["AAPL"], "close": [100.0]})

    assert svc._inject_intraday_signals(enriched, engine, "stock").equals(enriched)
    assert injected == [[]]
