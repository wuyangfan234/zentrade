"""交易日探针 (oracle) — 回答「今天是否美股交易日」。

消费方 (实时行情轮询 / 盘中分钟增量) 在周几+时段门控之后调用, 用于把
「工作日但休市」的节假日从轮询窗口里剔除; 返回 None (未知) 时调用方
维持现状行为 (周几近似 + 快照新鲜度判据兜底), 不引入新依赖。

探测链:
  1. Alpaca 交易日历 (与 market_clock.us_clock 同源): 今天在交易日列表内
     ⇔ 交易日。权威日历, 无时段依赖。
  2. 拉取失败 → None: 调用方按周几近似继续 (fail-open)。

安全约束:
  - 周末直接返回 False (周几判断零成本, 不打任何请求)。
  - 探针是纯读: 只产出一个布尔判定, 不落盘、不进行情管道、不碰归属链路。
  - 只用于「降档」(休市不轮询); 休市结论 TTL 较短 (30 分钟) 定期复探,
    探针误判最坏损失一段快照且可自愈; 未知结论短 TTL (5 分钟) 防止
    轮询循环每拍重打失败的探测。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.market_clock import us_clock


_TTL_TRADING_S = 3600.0   # 交易日结论每小时复探 (跨日天然失效)
_TTL_HOLIDAY_S = 1800.0   # 休市结论 30 分钟复探, 误判自愈上限
_TTL_UNKNOWN_S = 300.0    # 未知结论 5 分钟后重试探测

_CACHE_LOCK = threading.Lock()


@dataclass
class _Cache:
    day: object | None = None
    verdict: bool | None = None
    probed_at: float = 0.0


_CACHE = _Cache()


def reset_cache() -> None:
    """清空探针缓存 (测试用)。"""
    with _CACHE_LOCK:
        _CACHE.day = None
        _CACHE.verdict = None
        _CACHE.probed_at = 0.0


_PROBE_WINDOW = timedelta(days=7)  # 前后各 7 天: 美股最长连续休市 (周末+节假日) 也不会让窗口为空


def _probe_us_calendar(now: datetime) -> bool | None:
    """Alpaca 交易日历: 今天在列表内 ⇔ 交易日。拉取失败 → None (未知, 维持周几近似)。

    查今天前后一周的窗口而不是单日: 单日查询在工作日休市日 (感恩节/圣诞) 也拿到空列表,
    与「日历没数据」分不开, 以前一律当未知, 休市门控永远不触发 (BH-06)。
    窗口内有交易日 ⇒ 日历覆盖了今天, 可以下结论; 窗口整段为空 ⇒ 数据缺失, 仍按未知。
    """
    try:
        from app.market_clock.calendar import alpaca_trading_days

        today = now.date()
        days = set(alpaca_trading_days(today - _PROBE_WINDOW, today + _PROBE_WINDOW))
        return today in days if days else None
    except Exception:  # noqa: BLE001 — 探针失败按未知处理, 不上抛
        return None



def is_trading_day(now: datetime | None = None) -> bool | None:
    """今天是否美股交易日。True=交易日, False=确定休市, None=未知 (维持周几近似)。

    周末零成本直判; 工作日走探测链 (Alpaca 日历, 拉取失败即未知),
    结论按 TTL 缓存。线程安全: 实时行情与分钟增量两个线程共用。
    """
    now = now or us_clock.now()
    if now.weekday() >= 5:
        return False

    with _CACHE_LOCK:
        # 「未知」(None) 也是一个结论, 同样按 TTL 缓存 —— 它正是 _TTL_UNKNOWN_S 要
        # 挡住的场景 (探针全部不可用时, 轮询每拍都会重打一次探测)。
        # _CACHE.day 只在探测写回时设置, 因此「当天已探过」用它判定即可。
        if (
            _CACHE.day == now.date()
            and (time.monotonic() - _CACHE.probed_at) < _ttl_of(_CACHE.verdict)
        ):
            return _CACHE.verdict

    verdict = _probe_us_calendar(now)
    # 日历拉取失败时 probe 返回 None (未知), 调用方按周几近似继续 (fail-open)

    with _CACHE_LOCK:
        _CACHE.day = now.date()
        _CACHE.verdict = verdict
        _CACHE.probed_at = time.monotonic()
    return verdict


def _ttl_of(verdict: bool | None) -> float:
    if verdict is True:
        return _TTL_TRADING_S
    if verdict is False:
        return _TTL_HOLIDAY_S
    return _TTL_UNKNOWN_S
