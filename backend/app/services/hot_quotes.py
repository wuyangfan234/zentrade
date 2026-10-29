"""5 秒热报价池 (2026-10-02 产品决策: 持仓现价 5 秒刷新; 分时用实时报价拼分钟K)。

池 = 持仓 + 未终态机会卡 + 自选 + 最近被查看的个股 (最多 50 只, 按此优先级截断)。
每 5 秒一次 TickFlow 按标的报价 (12 次/分, 额度 60), 只写进程内缓冲, 不落盘、不撮合。
拼接出的分钟K只用于显示与盘中提醒; 撮合只用 trading_loop 的逐笔报价。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

POOL_LIMIT = 50
VIEW_TTL_SECONDS = 600

_viewed: dict[str, float] = {}
_lock = threading.Lock()
_last: dict = {"at": None, "count": 0, "pool": 0, "error": None, "error_at": None}


def watch(symbol: str) -> None:
    """个股分时被打开时调用: 该票进入热报价池 10 分钟。"""
    if symbol:
        with _lock:
            _viewed[symbol] = time.time()


def _viewed_symbols() -> list[str]:
    now = time.time()
    with _lock:
        for s in [s for s, t in _viewed.items() if now - t > VIEW_TTL_SECONDS]:
            _viewed.pop(s, None)
        return sorted(_viewed, key=lambda s: -_viewed[s])


def pool(loop=None) -> list[str]:
    ordered: list[str] = []
    if loop is not None:
        try:
            ordered += [p.symbol for p in loop.ledger.open_positions]
            # 0.24.1: 读交易环上一轮缓存的执行池; 旧实现每 5 秒重放全台账并等台账锁 (纸面撮合持锁时整轮阻塞)
            ordered += list(getattr(loop, "last_execution_symbols", None) or [])
        except Exception as e:  # noqa: BLE001
            logger.debug("hot pool: trading symbols unavailable: %s", type(e).__name__)
    try:
        from app.services import watchlist
        ordered += [r["symbol"] for r in watchlist.list_symbols() if r.get("symbol")]
    except Exception as e:  # noqa: BLE001
        logger.debug("hot pool: watchlist unavailable: %s", type(e).__name__)
    ordered += _viewed_symbols()
    return list(dict.fromkeys(ordered))[:POOL_LIMIT]


def provider():
    """当前生效的实时源若支持热报价 (TickFlow) 则返回之, 否则 None。"""
    try:
        from app.data_providers import custom as custom_sources
        from app.services.preferences import get_realtime_data_provider
        name = get_realtime_data_provider()
        if not name:
            return None
        p = custom_sources.get_provider(name)
        return p if callable(getattr(p, "poll_hot_quotes", None)) else None
    except Exception:  # noqa: BLE001
        return None


def tick(loop=None) -> dict:
    from app.market_clock import us_clock
    if not us_clock.in_session(us_clock.now()):
        return _last
    p = provider()
    if p is None:
        return _last
    symbols = pool(loop)
    try:
        n = p.poll_hot_quotes(symbols)
        _last.update(at=time.time(), count=n, pool=len(symbols), error=None, error_at=None)
    except Exception as e:  # noqa: BLE001
        _last.update(error=f"{type(e).__name__}: {e}"[:160], error_at=time.time(), pool=len(symbols))
        logger.warning("hot quote poll failed: %s", type(e).__name__)
    return _last


def status() -> dict:
    return dict(_last)
