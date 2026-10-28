"""market_time — 兼容门面 (P1-2 过渡件)。

历史: 本模块曾是旧市场时钟 (北京时间两段 240 分钟)。
P1-2 起全系统切换到 market_clock (美股语义: ET 单段 9:30-16:00, 390 分钟)。

**本文件只是旧函数名的转发器**: 33 个消费方经此一次性获得美股时钟语义,
后续随各模块收编逐步改 import app.market_clock 并删除本文件。
新代码禁止从这里 import — 一律 `from app.market_clock import us_clock`。

函数名映射 (旧 → 新):
- CN_TZ → us_clock.tz (America/New_York, 夏令时自处理)
- cn_now() → us_clock.now()
- cn_today() → us_clock.today()
- in_continuous_session() → us_clock.in_session()
- trading_minutes_elapsed*() → us_clock.session_progress*()
"""
from __future__ import annotations

from datetime import date, datetime

from app.market_clock import us_clock

CN_TZ = us_clock.tz


def cn_now() -> datetime:
    """当前美东时间 (带时区)。"""
    return us_clock.now()


def cn_today() -> date:
    """当前美东日期。"""
    return us_clock.today()


def in_continuous_session(now: datetime | None = None) -> bool:
    """美股连续交易时段 (ET 9:30-16:00), 仅交易日。"""
    return us_clock.in_session(now)


def trading_minutes_elapsed_from_dt(dt: datetime) -> float:
    """当日已交易分钟数 (0~390)。盘前=0; 收盘后/周末=390。"""
    return us_clock.session_progress(dt)


def trading_minutes_elapsed() -> float:
    """当前已交易分钟数 (基于服务端美东时间)。"""
    return us_clock.session_progress()


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。

    ts 为 None/无效时返回 390 (视作全天, 避免量比折算成 0)。
    """
    if not ts_ms:
        return float(us_clock.total_minutes)
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=us_clock.tz)
    except (ValueError, TypeError, OSError):
        return float(us_clock.total_minutes)
    return us_clock.session_progress(dt)
