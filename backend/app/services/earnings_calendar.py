"""财报日历: Nasdaq 主源 (免 key), Finnhub 备源; 原子缓存 + 新鲜度判定。

缓存 data_dir/reference/earnings/calendar.json: {fetched_at, source, window, events{symbol: {date, timing}}}
状态 data_dir/reference/earnings/state.json: 最近一次尝试与错误 (不含任何密钥)。

timing: bmo (盘前) / amc (盘后) / unknown (源未给出; 保护逻辑按盘前处理)。
源失败不覆盖上次成功的缓存。新鲜度按 NYSE 交易日 (用户 2026-10-11 批准, 取代 36 小时裸时长):
缓存 fetched_at 不早于「最近一个已到期的应刷新时点」即新鲜 — 应刷新时点 = 交易日 08:30 / 17:00 ET
(zen_earnings_premarket / zen_earnings_postclose), 到期 = 时点 + REFRESH_GRACE (= 任务 misfire_grace_time);
周末/休市日没有应刷新时点, 不过期。缓存缺失/损坏/无时间/时间解析失败/最近一次刷新失败 → unavailable。
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Callable

from app.config import PUBLIC_HTTP_USER_AGENT
from app.market_clock import us_clock

logger = logging.getLogger(__name__)

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date="
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
LOOKAHEAD_DAYS = 16          # 自然日; 覆盖约 10 个交易日
REFRESH_TIMES = (dt_time(8, 30), dt_time(17, 0))   # = daily_pipeline 财报日历两个 cron (mon-fri, ET)
REFRESH_GRACE = timedelta(minutes=60)               # = 两个任务的 misfire_grace_time 3600s
_NASDAQ_HEADERS = {
    "User-Agent": PUBLIC_HTTP_USER_AGENT, "Accept": "application/json",
    "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
}
_NASDAQ_TIMING = {"time-pre-market": "bmo", "time-after-hours": "amc"}
_FINNHUB_TIMING = {"bmo": "bmo", "amc": "amc"}

HttpGet = Callable[..., dict]


def _http_json(url: str, headers: dict | None = None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "zentrade"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints
        return json.loads(r.read() or b"null") or {}


def fetch_nasdaq(start: date, end: date, *, http_get: HttpGet | None = None) -> list[dict]:
    """逐交易日拉取; 任一日请求失败即整体失败 (不发布残缺日历)。"""
    http_get = http_get or _http_json
    out = []
    for day in us_clock.trading_days_between(start, end):
        body = http_get(NASDAQ_URL + day.isoformat(), headers=_NASDAQ_HEADERS)
        rows = ((body or {}).get("data") or {}).get("rows") or []
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                out.append({"symbol": sym, "date": day.isoformat(),
                            "timing": _NASDAQ_TIMING.get(row.get("time"), "unknown")})
    return out


def fetch_finnhub(start: date, end: date, token: str, *, http_get: HttpGet | None = None) -> list[dict]:
    http_get = http_get or _http_json
    from urllib.parse import urlencode
    body = http_get(f"{FINNHUB_URL}?{urlencode({'from': start.isoformat(), 'to': end.isoformat(), 'token': token})}")
    out = []
    for row in (body or {}).get("earningsCalendar") or []:
        sym, day = str(row.get("symbol") or "").strip().upper(), row.get("date")
        if sym and day:
            out.append({"symbol": sym, "date": str(day)[:10], "timing": _FINNHUB_TIMING.get(row.get("hour"), "unknown")})
    return out


def _configured_finnhub_token() -> str | None:
    """统一规则 (app/credentials.py): 设置页 > FINNHUB_API_KEY > ZENTRADE_ENV_DIR/finnhub.env。永不打印/落盘。"""
    from app import credentials
    return credentials.finnhub()


def refresh_due_point(now: datetime) -> datetime:
    """财报日历最近一个已到期的应刷新时点 (复用公司行动的交易日时点函数)。"""
    from app.services.corporate_actions import latest_due_point

    return latest_due_point(now, REFRESH_TIMES, REFRESH_GRACE)


class EarningsCalendar:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir) / "reference" / "earnings"
        self.cache_path = self.dir / "calendar.json"
        self.state_path = self.dir / "state.json"

    def _write(self, path: Path, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    def refresh(self, now: datetime | None = None, *, nasdaq_get: HttpGet | None = None,
                finnhub_get: HttpGet | None = None, finnhub_token: str | None = None,
                use_finnhub: bool = True) -> dict:
        """拉取 [今天, 今天+LOOKAHEAD] ; Nasdaq 失败才用 Finnhub (use_finnhub=False 禁用备源)。"""
        now = us_clock.market_datetime(now or us_clock.now())
        start, end = now.date(), now.date() + timedelta(days=LOOKAHEAD_DAYS)
        state = {"last_attempt_at": now.isoformat()}
        rows, source, errors = None, None, []
        if nasdaq_get is None:
            nasdaq_get = _http_json
        try:
            rows, source = fetch_nasdaq(start, end, http_get=nasdaq_get), "nasdaq"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"nasdaq: {type(exc).__name__}")
        # Nasdaq returns data=null for a normal no-earnings day, so one empty day is legal; an empty
        # ~10 trading-day window is not (bot block / schema change). Cross-check Finnhub then.
        if rows is not None and not rows and use_finnhub:
            errors.append("nasdaq: empty window")
            empty_nasdaq, rows = rows, None
        else:
            empty_nasdaq = None
        if rows is None and use_finnhub:
            token = finnhub_token if finnhub_token is not None else _configured_finnhub_token()
            if token:
                try:
                    rows, source = fetch_finnhub(start, end, token, http_get=finnhub_get or _http_json), "finnhub"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"finnhub: {type(exc).__name__}")
            else:
                errors.append("finnhub: no token")
        if rows is None and empty_nasdaq is not None:
            rows, source = empty_nasdaq, "nasdaq"   # Finnhub unavailable: keep the Nasdaq answer as before
        if rows is None:
            logger.warning("财报日历两源均失败, 保留旧缓存: %s", errors)
            self._write(self.state_path, {**state, "ok": False, "error": "; ".join(errors)})
            return self.status(now)
        events: dict[str, dict] = {}
        for row in sorted(rows, key=lambda r: r["date"]):
            events.setdefault(row["symbol"], {"date": row["date"], "timing": row["timing"]})
        self._write(self.cache_path, {"fetched_at": now.isoformat(), "source": source,
                                      "window": [start.isoformat(), end.isoformat()], "events": events})
        self._write(self.state_path, {**state, "ok": True, "error": None})
        return self.status(now)

    def status(self, now: datetime | None = None) -> dict:
        now = us_clock.market_datetime(now or us_clock.now())
        cache, state = self._read(self.cache_path), self._read(self.state_path) or {}
        if not isinstance(cache, dict):
            cache = None
        fetched_at = cache.get("fetched_at") if cache else None
        due = refresh_due_point(now)
        try:
            fresh = bool(fetched_at) and us_clock.market_datetime(fetched_at) >= due
            parsed = bool(fetched_at)
        except (TypeError, ValueError):
            fresh, parsed = False, False
        if not parsed:
            status = "unavailable"
        elif not fresh:
            status = "stale"
        elif state.get("ok") is False:
            # 最近一次刷新失败: 如实报不可用 (新开仓暂停), 缓存仍保留供展示
            status = "unavailable"
        else:
            status = "connected"
        return {"status": status, "source": cache.get("source") if cache else None, "fetched_at": fetched_at,
                "last_attempt_at": state.get("last_attempt_at"), "error": state.get("error"),
                "window": cache.get("window") if cache else None, "due_at": due.isoformat()}

    def usable(self, now: datetime | None = None) -> bool:
        return self.status(now)["status"] == "connected"

    def event_for(self, symbol: str) -> dict | None:
        cache = self._read(self.cache_path) or {}
        return (cache.get("events") or {}).get(symbol.upper())
