"""财报保护的纯规则 (用户拍板口径, 2026-10-01):

- 临近财报禁开: 仅财报前 1 个交易日不开新仓 (用户原文"仅财报前 1 个交易日禁开（最宽松）");
  财报当天不在禁开窗口内。
- 财报前减半: 持仓期 (建仓日起第 max_days 个交易日内) 含财报 → 在减仓日的收盘前窗口
  [15:30, 16:00) ET 减半。减仓日 = 盘前/未知: 财报日前一交易日; 盘后: 财报当日。
- 窗口内拿不到新鲜报价 → 记录"未能减仓", 窗口过后不补做。
"""
from __future__ import annotations

from datetime import date, datetime

from app.market_clock import us_clock


def _event_day(event: dict) -> date:
    return date.fromisoformat(str(event["date"])[:10])


def entry_blocked(event: dict | None, today: date) -> bool:
    if not event:
        return False
    day = _event_day(event)
    return day == us_clock.next_trading_day(today)


def reduce_day(event: dict) -> date:
    day = _event_day(event)
    return day if event.get("timing") == "amc" else us_clock.previous_trading_day(day)


def reduction_needed(event: dict | None, entry_day: date, max_days: int) -> bool:
    """财报减仓日落在持仓期内 (含建仓日, 不晚于时间止损日) 才需要减半。"""
    if not event:
        return False
    target = reduce_day(event)
    if target < entry_day:
        return False
    return us_clock.days_held(entry_day, target) <= max_days


def in_reduce_window(event: dict, now: datetime) -> bool:
    now = us_clock.market_datetime(now)
    return now.date() == reduce_day(event) and us_clock.in_pre_close_window(now)


def reduce_window_missed(event: dict, now: datetime) -> bool:
    """减仓日收盘已过 (或已是之后的日子)。"""
    now = us_clock.market_datetime(now)
    target = reduce_day(event)
    return now.date() > target or (now.date() == target and now.time() >= us_clock.sessions[-1].close)
