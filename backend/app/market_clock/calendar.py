"""Alpaca 日历提供者 + 本地 parquet 缓存。

首拉覆盖 [今年-1, 明年+1] 宽窗口并落盘缓存; 后续窗口命中缓存直接读,
缓存不覆盖时才打网络。网络失败抛异常 (MarketClock 层 fail-open 处理)。

密钥: 只从环境/secrets 读, 不打印。Alpaca calendar 端点在交易 API 域名。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import date, timedelta
from datetime import time as dt_time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_FILE = "us_trading_calendar.json"
_WARM_YEARS = 1
_REFRESH_AHEAD_DAYS = 60   # 覆盖终点不足 今天+60 天即重拉
_REFRESH_AGE_DAYS = 30     # 距上次拉取超过 30 天也重拉 (学新增临时休市)
_RETRY_AFTER_S = 600       # 重拉失败后 10 分钟内沿用旧缓存不再重试
_last_failed_fetch: float | None = None


def _secrets() -> tuple[str, str, str]:
    """返回 (key, secret, paper_base)。凭据走统一规则 (app/credentials.py), 设置页填的 Key 同样生效。"""
    from app import credentials
    key, sec, _src = credentials.alpaca()
    base = os.environ.get("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets/v2").rstrip("/")
    return key, sec, base


def _cache_path() -> Path:
    from app.config import settings
    d = Path(settings.data_dir) / "market_clock"
    d.mkdir(parents=True, exist_ok=True)
    return d / _CACHE_FILE


def _load_disk_cache() -> dict[str, object]:
    try:
        p = _cache_path()
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        logger.warning("calendar disk cache unreadable: %s", e)
    return {}


def _save_disk_cache(data: dict[str, object]) -> None:
    try:
        p = _cache_path()
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # 并发写互不覆盖临时文件
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("calendar disk cache write failed: %s", e)


def _fetch_calendar_rows(start: date, end: date) -> list[dict]:
    key, sec, base = _secrets()
    if not key or not sec:
        raise RuntimeError("alpaca credentials not configured")
    url = f"{base}/calendar?start={start.isoformat()}&end={end.isoformat()}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec,
        "User-Agent": "zentrade/0.1",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# early closes {"YYYY-MM-DD": "HH:MM"} (half days, e.g. day after Thanksgiving 13:00 ET); None = not loaded
_early_closes: dict[str, str] | None = None
_FULL_CLOSE = "16:00"


def _early_from_rows(rows: list[dict]) -> dict[str, str]:
    out = {}
    for row in rows:
        close = str(row.get("close") or "")
        if len(close) == 5 and close < _FULL_CLOSE:
            out[str(row["date"])] = close
    return out


def alpaca_session_close(d: date) -> dt_time | None:
    """Early close of day d from the calendar cache (no network); None = regular 16:00 close."""
    global _early_closes
    if _early_closes is None:
        raw = _load_disk_cache().get("early_closes")
        _early_closes = raw if isinstance(raw, dict) else {}
    hhmm = _early_closes.get(d.isoformat())
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return dt_time(int(h), int(m))


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _coverage(cache: dict[str, object], cached_days: set[date]) -> tuple[date, date] | None:
    """缓存实际覆盖的 [起, 止]; 旧缓存无 covered_* 字段时按已缓存交易日的首尾推断"""
    try:
        return (date.fromisoformat(str(cache["covered_start"])), date.fromisoformat(str(cache["covered_end"])))
    except (KeyError, TypeError, ValueError):
        pass
    if not cached_days:
        return None
    return min(cached_days), max(cached_days)


def _warmed_at(cache: dict[str, object]) -> date | None:
    try:
        return date.fromisoformat(str(cache.get("warmed_at")))
    except (TypeError, ValueError):
        return None


_REFRESH_LOCK = threading.Lock()


def _parse_rows(rows) -> set[date]:
    """校验并解析日历行; 格式不对抛 ValueError (按拉取失败处理, 走退避)。"""
    if not isinstance(rows, list):
        raise ValueError(f"calendar payload is {type(rows).__name__}, expected list")
    out = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValueError("calendar row without date")
        out.add(date.fromisoformat(row["date"]))
    return out


def alpaca_trading_days(start: date, end: date) -> list[date]:
    """生产日历入口: 磁盘缓存优先, 缺口/将到期/超龄时重拉 (宽窗口预暖).

    - 覆盖终点早于 今天+_REFRESH_AHEAD_DAYS, 或 warmed_at 超过 _REFRESH_AGE_DAYS 天, 重拉;
      重拉窗口内以新结果为准 (能学到新增的临时休市), 窗口外保留旧数据.
    - 重拉失败: 有旧缓存就继续用旧缓存 (_RETRY_AFTER_S 内不再重试), 无缓存才抛.
    - 覆盖范围之外的日子不当休市: fail-open 按工作日返回.
    """
    global _early_closes, _last_failed_fetch
    cache = _load_disk_cache()
    raw_days = cache.get("trading_days", [])
    if not isinstance(raw_days, list):
        raw_days = []
    cached_days = {date.fromisoformat(d) for d in raw_days if isinstance(d, str)}
    today = date.today()
    warm_start = today - timedelta(days=365 * _WARM_YEARS)
    warm_end = today + timedelta(days=365 * (_WARM_YEARS + 1))
    early = cache.get("early_closes")
    cov = _coverage(cache, cached_days)
    warmed = _warmed_at(cache)
    stale = warmed != today and (       # 今天已拉过就不再因到期/超龄重拉 (上游没更远数据时不反复打网络)
        warmed is None
        or cov is None
        or cov[1] < today + timedelta(days=_REFRESH_AHEAD_DAYS)
        or (today - warmed).days > _REFRESH_AGE_DAYS
    )
    need = (
        cov is None
        or start < cov[0]
        or stale
        or not isinstance(early, dict)        # caches written before half-day support lack early_closes
    )
    if need and cov is not None and _last_failed_fetch is not None \
            and time.monotonic() - _last_failed_fetch < _RETRY_AFTER_S:
        need = False                          # 刚失败过: 先用旧缓存, 避免每次调用都打网络
    # 同一时刻只让一个线程打网络; 其余线程不等, 直接用旧缓存 (交易环 15 秒一拍不能被 30 秒超时卡住)
    if need:
        # 无缓存时必须等到结果; 有旧缓存时拿不到锁就直接用旧缓存
        need = _REFRESH_LOCK.acquire(blocking=cov is None)
    if need:
        try:
            fetch_start = min(start, warm_start) if cov is None or start < cov[0] else warm_start
            fetched: set[date] = set()
            rows = None
            try:
                rows = _fetch_calendar_rows(fetch_start, warm_end)
                fetched = _parse_rows(rows)
            except Exception:
                if cov is None:
                    raise
                _last_failed_fetch = time.monotonic()
                logger.warning("calendar refresh failed; keeping cached calendar (covered %s~%s)", cov[0], cov[1])
                rows = None
            if rows is not None and not fetched:
                _last_failed_fetch = time.monotonic()   # 拉取成功但空: 视同失败, 沿用旧缓存
            if fetched:
                _last_failed_fetch = None
                # 只替换上游实际返回的区间 [首日, 末日]: 上游从中间某天开始返回时, 不抹掉之前的缓存交易日
                f_lo, f_hi = min(fetched), max(fetched)
                cached_days = {d for d in cached_days if not (f_lo <= d <= f_hi)} | fetched
                cov = (min(cov[0], f_lo) if cov else f_lo, max(cov[1], f_hi) if cov else f_hi)
                early = {k: v for k, v in (early if isinstance(early, dict) else {}).items()
                         if not (f_lo.isoformat() <= k <= f_hi.isoformat())}
                early.update(_early_from_rows(rows))
                _save_disk_cache({
                    "trading_days": sorted(d.isoformat() for d in cached_days),
                    "early_closes": early,
                    "warmed_at": today.isoformat(),
                    "covered_start": cov[0].isoformat(),
                    "covered_end": cov[1].isoformat(),
                })
        finally:
            _REFRESH_LOCK.release()
    if isinstance(early, dict):
        _early_closes = early
    out = {d for d in cached_days if start <= d <= end}
    if cov is None:
        return sorted(out)
    if start < cov[0]:
        out.update(_weekdays(start, min(end, cov[0] - timedelta(days=1))))
    if end > cov[1]:
        out.update(_weekdays(max(start, cov[1] + timedelta(days=1)), end))
    return sorted(out)
