"""助手查询与 system prompt 的今天取美东日期，而非服务器本地日历日。

文件名保留兼容历史测试入口；工具的 cn_today 门面目前也转发至美东时钟。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.custom.assistant import prompt as assistant_prompt
from app.custom.assistant import tools as assistant_tools

ET = date(2026, 9, 21)
LOCAL = date(2026, 9, 20)


class _FakeDate(date):
    """让模块内 date.today() 返回 LOCAL, 与美东时钟的 ET 错开。"""

    @classmethod
    def today(cls):
        return LOCAL


def _frame(end: date, n: int = 5) -> pl.DataFrame:
    days = [end - timedelta(days=i) for i in range(n)][::-1]
    return pl.DataFrame({
        "date": days,
        "open": [10.0] * n,
        "high": [11.0] * n,
        "low": [9.0] * n,
        "close": [10.5] * n,
        "volume": [100.0] * n,
    })


class _DailyRepo:
    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"AAPL": "Apple"}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        self.windows.append((start, end))
        return _frame(end)


def _patch_clocks(monkeypatch: pytest.MonkeyPatch, module) -> None:
    monkeypatch.setattr(module, "cn_today", lambda: ET, raising=False)
    monkeypatch.setattr(module, "date", _FakeDate)


async def test_get_stock_daily_window_ends_on_eastern_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_clocks(monkeypatch, assistant_tools)
    repo = _DailyRepo()
    ctx = assistant_tools.ToolContext.build(repo=repo)
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_daily", {"symbol": "AAPL", "days": 10}, ctx,
    )
    assert payload["ok"] is True
    assert repo.windows, "应调用 get_daily_asset"
    _start, end = repo.windows[0]
    assert end == ET, f"日线窗口右端必须是美东日期 {ET}, 实际 {end} (服务器本地 {LOCAL})"
    assert end != LOCAL


async def test_get_stock_analysis_window_ends_on_eastern_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_clocks(monkeypatch, assistant_tools)
    repo = _DailyRepo()
    ctx = assistant_tools.ToolContext.build(repo=repo)
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_analysis", {"symbol": "AAPL"}, ctx,
    )
    assert payload["ok"] is True
    assert repo.windows
    _start, end = repo.windows[0]
    assert end == ET, f"分析窗口右端必须是美东日期 {ET}, 实际 {end}"


def test_system_prompt_today_is_eastern_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_prompt.us_clock, "today", lambda: ET)
    monkeypatch.setattr(assistant_prompt, "date", _FakeDate, raising=False)
    text = assistant_prompt.build_system_prompt(None)
    assert f"今天日期: {ET.isoformat()}" in text
    assert f"今天日期: {LOCAL.isoformat()}" not in text
