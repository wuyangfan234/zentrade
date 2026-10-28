"""MarketClock — 市场时钟抽象（ZenTrade 美股语义，P1-2）。

全系统唯一允许做交易时段判断的地方。任何模块需要「现在是否盘中」
「今天是否交易日」「当日已交易分钟数」「持仓交易日计数」都必须走这里。

美股语义（MVP，常规时段）:
- 时区: America/New_York（自带夏令时，代码不手写偏移）
- 连续交易: 单段 09:30-16:00（无午休，390 分钟）
- 日历: NYSE（Alpaca calendar API + 本地缓存；拉取失败保守视为交易日）

与旧 market_time.py（旧市场语义）的本质差异:
- 无午休分段、无 240 分钟总量、无集合竞价缓冲窗
- 时区从 CN_TZ 换成市场本地时区（服务器墙钟不可靠，与上游一致纪律）

usage:
    from app.market_clock import us_clock
    us_clock.is_trading_day(date(2026, 9, 26))     # → bool (缓存/探针)
    us_clock.in_session()                          # → bool (当前是否连续交易时段)
    us_clock.session_progress()                    # → float (0~390 已交易分钟)
    us_clock.days_held(entry_date, today)          # → int (交易日计数, 时间止损用)
"""
from __future__ import annotations

from app.market_clock.clock import (
    US_SESSION_CLOSE,
    US_SESSION_OPEN,
    US_TOTAL_MINUTES,
    MarketClock,
    Session,
    us_clock,
)

__all__ = [
    "MarketClock", "Session", "US_SESSION_CLOSE", "US_SESSION_OPEN",
    "US_TOTAL_MINUTES", "us_clock",
]
