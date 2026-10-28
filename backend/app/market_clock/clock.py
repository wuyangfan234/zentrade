"""MarketClock 核心实现（美股单例 us_clock）。

日历提供者注入式设计: 生产环境由 calendar.py 的 Alpaca 提供者注入;
测试注入内存提供者, 全链路不碰网络。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 美股常规交易时段（美东）: 单段 09:30-16:00 = 390 分钟, 无午休
US_SESSION_OPEN = dt_time(9, 30)
US_SESSION_CLOSE = dt_time(16, 0)
US_ENTRY_OPEN = dt_time(9, 45)
US_ENTRY_CLOSE = dt_time(15, 30)
US_HOLDING_EXIT = dt_time(15, 30)
US_PRE_CLOSE_OPEN = US_HOLDING_EXIT  # 事件减仓与时间止损共用收盘前窗口
_TRADING_DAY_SEARCH_SPAN = 14  # 自然日；覆盖最长休市连段
US_TOTAL_MINUTES = 390
US_TZ = ZoneInfo("America/New_York")

# 日历提供者协议: 返回 [start, end] 闭区间内的交易日集合
CalendarProvider = Callable[[date, date], Iterable[date]]


@dataclass(frozen=True)
class Session:
    open: dt_time
    close: dt_time

    @property
    def minutes(self) -> int:
        return (self.close.hour * 60 + self.close.minute) - (self.open.hour * 60 + self.open.minute)


class MarketClock:
    """市场时钟。线程安全; 日历结果带 TTL 缓存。"""

    def __init__(
        self,
        *,
        tz: ZoneInfo,
        sessions: list[Session],
        calendar_provider: CalendarProvider | None = None,
        calendar_ttl_s: float = 3600.0,
        close_provider: Callable[[date], dt_time | None] | None = None,
    ) -> None:
        if not sessions:
            raise ValueError("sessions 不能为空")
        self.tz = tz
        self.sessions = sessions
        self.total_minutes = sum(s.minutes for s in sessions)
        self._calendar_provider = calendar_provider
        self._close_provider = close_provider
        self._ttl = calendar_ttl_s
        self._lock = threading.Lock()
        self._cal_cache: dict[tuple[date, date], tuple[float, frozenset[date]]] = {}

    # ── 时间基 ──────────────────────────────────────────────
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def market_datetime(self, value: str | datetime) -> datetime:
        """ISO/aware 转市场时区；naive 按市场墙钟解释，不猜 UTC。"""
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value.replace(tzinfo=self.tz) if value.tzinfo is None else value.astimezone(self.tz)

    # ── 交易日 ──────────────────────────────────────────────
    def is_trading_day(self, d: date | None = None) -> bool:
        """d 是否交易日。无日历提供者时按工作日近似（周末必非交易日）。"""
        d = d or self.today()
        if d.weekday() >= 5:
            return False
        if self._calendar_provider is None:
            return True
        days = self._calendar_days(d, d)
        if days is None:
            return True  # 日历拉取失败: 保守视为交易日 (fail-open, 不阻塞管线)
        return d in days

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """[start, end] 闭区间内的交易日列表 (升序)。无提供者时按工作日近似。"""
        if start > end:
            return []
        if self._calendar_provider is None:
            days = None
        else:
            days = self._calendar_days(start, end)
        out: list[date] = []
        cur = start
        while cur <= end:
            if cur.weekday() < 5 and (days is None or cur in days):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def _calendar_days(self, start: date, end: date) -> frozenset[date] | None:
        """拉日历带 TTL 缓存; 失败返回 None (fail-open 由调用方决定)。"""
        assert self._calendar_provider is not None
        key = (start, end)
        import time
        with self._lock:
            hit = self._cal_cache.get(key)
            if hit and (time.monotonic() - hit[0]) < self._ttl:
                return hit[1]
        try:
            days = frozenset(self._calendar_provider(start, end))
        except Exception as e:  # noqa: BLE001
            logger.warning("market calendar fetch failed (%s~%s): %s (fail-open)", start, end, e)
            return None
        with self._lock:
            self._cal_cache[key] = (time.monotonic(), days)
        return days

    # ── 时段 ──────────────────────────────────────────────
    def in_session(self, now: datetime | None = None) -> bool:
        """当前是否连续交易时段 (含边界)。周末/非日历交易日恒 False。"""
        now = now or self.now()
        if not self.is_trading_day(now.date()):
            return False
        t = now.time()
        close = self.session_close(now.date())
        return any(s.open <= t <= min(s.close, close) for s in self.sessions)

    def session_close(self, d: date) -> dt_time:
        """Close of day d: calendar early close (half day) if earlier than the regular close."""
        regular = self.sessions[-1].close
        if self._close_provider is None or self._calendar_provider is None:
            return regular
        try:
            early = self._close_provider(d)
        except Exception as e:
            logger.warning("session close lookup failed (%s): %s", d, e)
            return regular
        return early if early is not None and early < regular else regular

    def _before_close(self, d: date, regular: dt_time) -> dt_time:
        """regular (15:30) on a full day; close - 30 min on a half day."""
        close = self.session_close(d)
        if close >= self.sessions[-1].close:
            return regular
        shifted = (datetime.combine(d, close) - timedelta(minutes=30)).time()
        return min(regular, shifted)

    def entry_close(self, d: date) -> dt_time:
        """入场截止 / 时间平仓时刻: 正常日 15:30, 半日市收盘前 30 分钟 (13:00 收盘 = 12:30)。
        0.39.3 (审计 M-2): 机会板 / 雷达 / 交易环统一读这里, 不再各自写死 15:30。"""
        return self._before_close(d, US_ENTRY_CLOSE)

    def session_progress(self, now: datetime | None = None) -> float:
        """当日已交易分钟数 (0~total)。开盘前=0, 收盘后=total, 非交易日=total。"""
        now = now or self.now()
        if now.weekday() >= 5:
            return float(self.total_minutes)
        t = now.time()
        elapsed = 0.0
        for s in self.sessions:
            open_min = s.open.hour * 60 + s.open.minute
            close_min = s.close.hour * 60 + s.close.minute
            cur = t.hour * 60 + t.minute + t.second / 60.0
            if cur < open_min:
                break
            if cur >= close_min:
                elapsed += close_min - open_min
                continue
            elapsed += cur - open_min
            break
        return min(elapsed, float(self.total_minutes))

    # ── 波段语义 ──────────────────────────────────────────
    def in_entry_window(self, now: datetime | None = None) -> bool:
        """纸面入场窗 [09:45, 15:30] ET；含边界且必须是交易日。"""
        now = self.market_datetime(now or self.now())
        return self.in_session(now) and US_ENTRY_OPEN <= now.time() <= self._before_close(now.date(), US_ENTRY_CLOSE)

    def holding_exit_due(self, entry: date, max_days: int, now: datetime | None = None) -> bool:
        """到期日 15:30 强退；已逾期持仓在下一有效常规时段立即退出。"""
        now = self.market_datetime(now or self.now())
        if not self.in_session(now):
            return False
        days = self.days_held(entry, now.date())
        return days > max_days or (days == max_days and now.time() >= self._before_close(now.date(), US_HOLDING_EXIT))

    def in_pre_close_window(self, now: datetime | None = None) -> bool:
        """收盘前风险减仓窗 [15:30, 16:00) ET，与时间止损同一窗口；须为交易日。"""
        now = self.market_datetime(now or self.now())
        d = now.date()
        return self.in_session(now) and self._before_close(d, US_PRE_CLOSE_OPEN) <= now.time() < self.session_close(d)

    def next_trading_day(self, d: date) -> date:
        """严格晚于 d 的首个交易日（日历缺失时按工作日近似）。"""
        days = self.trading_days_between(d + timedelta(days=1), d + timedelta(days=_TRADING_DAY_SEARCH_SPAN))
        if not days:
            raise ValueError(f"no trading day within {_TRADING_DAY_SEARCH_SPAN} days after {d}")
        return days[0]

    def previous_trading_day(self, d: date) -> date:
        """严格早于 d 的最近交易日。"""
        days = self.trading_days_between(d - timedelta(days=_TRADING_DAY_SEARCH_SPAN), d - timedelta(days=1))
        if not days:
            raise ValueError(f"no trading day within {_TRADING_DAY_SEARCH_SPAN} days before {d}")
        return days[-1]

    def days_held(self, entry: date, today: date | None = None) -> int:
        """持仓交易日计数: entry=1。today < entry 返回 0 (防御)。"""
        today = today or self.today()
        if today < entry:
            return 0
        return len(self.trading_days_between(entry, today))


# ── 美股单例 ─────────────────────────────────────────────
_calendar_provider: CalendarProvider | None = None


def _default_us_calendar(start: date, end: date) -> Iterable[date]:
    """Alpaca 日历 (生产默认)。网络失败抛异常由 MarketClock fail-open。"""
    from app.market_clock.calendar import alpaca_trading_days
    return alpaca_trading_days(start, end)


def _default_us_close(d: date) -> dt_time | None:
    """Half-day close from the Alpaca calendar cache (no network)."""
    from app.market_clock.calendar import alpaca_session_close
    return alpaca_session_close(d)


def _build_us_clock() -> MarketClock:
    return MarketClock(
        tz=US_TZ,
        sessions=[Session(US_SESSION_OPEN, US_SESSION_CLOSE)],
        calendar_provider=_default_us_calendar,
        close_provider=_default_us_close,
    )


us_clock = _build_us_clock()
