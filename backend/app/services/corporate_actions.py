"""公司行动（Alpaca corporate-actions）拉取、缓存与自动处理。

范围（已批准）：
- 现金分红 / 正拆 / 反拆：自动写 corp_action 台账事件（action_id 去重、幂等）。
- 分拆 / 并购 / 改代码 / 股票分红等：只标记——待入场卡禁开、持仓写提醒事件并入队通知，
  不自动改持仓。

缓存：data_dir/reference/corporate_actions/actions.json（原子写，含 fetched_at）；
源失败不覆盖已有缓存，状态单独落 state.json 并标为 unavailable。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from app.market_clock import us_clock

logger = logging.getLogger(__name__)

API_URL = "https://data.alpaca.markets/v1/corporate-actions"
LOOKBACK_DAYS = 10
LOOKAHEAD_DAYS = 30
# 新鲜度按 NYSE 交易日算 (用户 2026-10-11 批准, 取代原 26 小时裸时长 —— 后者让每个周末都判过期、暂停新开仓):
# 应刷新时点 = 交易日 08:00 ET (daily_pipeline 定时任务 zen_corporate_actions); 到期 = 该时点 + 宽限。
# 缓存 fetched_at 不早于「最近一个已到期的应刷新时点」即新鲜; 周末/休市日没有应刷新时点, 不过期。
REFRESH_AT = dt_time(8, 0)
REFRESH_GRACE = timedelta(minutes=60)   # = 定时任务 misfire_grace_time 3600s; 过了仍没刷新成功 → stale, 暂停新开仓
AUTO_TYPES = ("forward_split", "reverse_split", "cash_dividend")

_TYPE_MAP = {
    "forward_splits": "forward_split",
    "reverse_splits": "reverse_split",
    "unit_splits": "unit_split",
    "cash_dividends": "cash_dividend",
    "stock_dividends": "stock_dividend",
    "spin_offs": "spin_off",
    "cash_mergers": "cash_merger",
    "stock_mergers": "stock_merger",
    "stock_and_cash_mergers": "stock_and_cash_merger",
    "name_changes": "name_change",
    "worthless_removals": "worthless_removal",
    "rights_distributions": "rights_distribution",
    "redemptions": "redemption",
}
TYPE_LABEL = {
    "forward_split": "拆股", "reverse_split": "反向拆股", "cash_dividend": "现金分红",
    "stock_dividend": "股票分红", "spin_off": "分拆", "cash_merger": "现金并购",
    "stock_merger": "换股并购", "stock_and_cash_merger": "股票+现金并购",
    "name_change": "更名/改代码", "unit_split": "单位拆分", "worthless_removal": "摘牌注销",
    "rights_distribution": "配权", "redemption": "赎回",
}


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_actions(payload: dict) -> list[dict]:
    """把一页响应规整成统一记录；非法拆股比例的记录降级为只标记（不自动入账）。"""
    out: list[dict] = []
    for group, rows in (payload.get("corporate_actions") or {}).items():
        kind = _TYPE_MAP.get(group, group.rstrip("s"))
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            symbol = (row.get("symbol") or row.get("acquiree_symbol") or row.get("source_symbol")
                      or row.get("old_symbol"))
            ex_date = row.get("ex_date") or row.get("effective_date") or row.get("process_date")
            if not symbol or not ex_date:
                continue
            rec = {"id": row["id"], "type": kind, "symbol": symbol, "ex_date": ex_date,
                   "payable_date": row.get("payable_date"), "record_date": row.get("record_date"),
                   "auto": kind in AUTO_TYPES, "raw": row}
            if kind in ("forward_split", "reverse_split"):
                new, old = _num(row.get("new_rate")), _num(row.get("old_rate"))
                if new and old and new > 0 and old > 0:
                    rec["factor"] = new / old
                else:
                    rec["auto"] = False
            elif kind == "cash_dividend":
                rate = _num(row.get("rate"))
                if rate is None or rate < 0:
                    rec["auto"] = False
                rec["rate"] = rate
                rec["special"] = bool(row.get("special"))
            out.append(rec)
    return out


def _default_http_get(url: str) -> dict:
    from app.plugins.alpaca.provider import _get, _load_credentials
    key, secret = _load_credentials()
    if not key or not secret:
        raise RuntimeError("未配置 Alpaca Key")
    return _get(url, key, secret)


def fetch_actions(symbols: list[str], start: date, end: date, *,
                  http_get: Callable[[str], dict] | None = None, limit: int = 1000) -> list[dict]:
    """按标的+窗口分页拉全；无关心标的时不访问源。"""
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return []
    http_get = http_get or _default_http_get
    out: list[dict] = []
    token: str | None = None
    for _ in range(100):  # 分页硬上限，防源端 token 循环
        params = {"symbols": ",".join(symbols), "start": start.isoformat(),
                  "end": end.isoformat(), "limit": limit}
        if token:
            params["page_token"] = token
        body = http_get(f"{API_URL}?{urlencode(params)}")
        out.extend(parse_actions(body))
        token = body.get("next_page_token")
        if not token:
            return out
    raise RuntimeError("公司行动分页超过上限")


def latest_due_point(now: datetime, times: tuple[dt_time, ...], grace: timedelta) -> datetime:
    """按交易日定时刷新的缓存共用: 最近一个已到期的应刷新时点。走 NYSE 日历, 不用裸小时数。

    应刷新时点 = 交易日 D 上的 times 各时刻 (ET); 已到期 = now ≥ 时点 + grace。今天不是交易日或
    今天的时点都没到期 → 上一交易日的最晚时点。周末/休市日因此没有新的应刷新时点, 缓存不会过期。
    公司行动 (08:00) 与财报日历 (08:30 / 17:00) 共用。
    """
    now = us_clock.market_datetime(now)
    d = now.date()
    if us_clock.is_trading_day(d):
        for t in sorted(times, reverse=True):
            point = datetime.combine(d, t, tzinfo=us_clock.tz)
            if now >= point + grace:
                return point
    d = us_clock.previous_trading_day(d)
    return datetime.combine(d, max(times), tzinfo=us_clock.tz)


def refresh_due_point(now: datetime) -> datetime:
    """最近一个已到期的应刷新时点 (交易日 D 的 08:00 ET, 且 now ≥ 该时点 + REFRESH_GRACE)。走 NYSE 日历。"""
    return latest_due_point(now, (REFRESH_AT,), REFRESH_GRACE)


class CorporateActionService:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir) / "reference" / "corporate_actions"
        self.cache_path = self.dir / "actions.json"
        self.state_path = self.dir / "state.json"

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

    def load(self) -> dict | None:
        return self._read(self.cache_path)

    def refresh(self, symbols: list[str], now: datetime, *, http_get=None) -> dict:
        start, end = now.date() - timedelta(days=LOOKBACK_DAYS), now.date() + timedelta(days=LOOKAHEAD_DAYS)
        state: dict = {"last_attempt_at": now.isoformat()}
        try:
            actions = fetch_actions(symbols, start, end, http_get=http_get)
        except Exception as exc:  # noqa: BLE001 — 源失败只降级，不覆盖缓存
            logger.warning("公司行动拉取失败，保留旧缓存: %s", type(exc).__name__)
            state.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
            self._atomic_write(self.state_path, state)
            return self.status(now)
        self._atomic_write(self.cache_path, {
            "fetched_at": now.isoformat(), "symbols": sorted(set(symbols)),
            "window": {"start": start.isoformat(), "end": end.isoformat()}, "actions": actions,
        })
        state.update(ok=True, error=None)
        self._atomic_write(self.state_path, state)
        return self.status(now)

    def refresh_merge(self, symbols: list[str], now: datetime, *, http_get=None) -> dict:
        """盘中只为新增标的补拉, 合并进现有缓存。失败只记在 merge_error, 不改动 state.ok / fetched_at:
        早上那份成功缓存继续有效, 已覆盖的标的照常交易, 未覆盖的新票仍被 covers() 拦下 (2026-10-10 审查 P2)。"""
        cache = self.load()
        if cache is None or not cache.get("fetched_at"):
            return self.refresh(symbols, now, http_get=http_get)
        new = sorted(set(symbols) - set(cache.get("symbols") or []))
        if not new:
            return {"merged": 0}
        start, end = date.fromisoformat(cache["window"]["start"]), date.fromisoformat(cache["window"]["end"])
        try:
            actions = fetch_actions(new, start, end, http_get=http_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("公司行动补拉失败 (保留早盘缓存): %s", type(exc).__name__)
            st = self._read(self.state_path) or {}
            self._atomic_write(self.state_path, {**st, "merge_error": f"{type(exc).__name__}: {exc}"[:200],
                                                 "merge_attempt_at": now.isoformat()})
            return {"merged": 0, "error": type(exc).__name__}
        seen = {a.get("id") for a in cache.get("actions") or []}
        self._atomic_write(self.cache_path, {**cache, "symbols": sorted(set(cache.get("symbols") or []) | set(new)),
                                             "actions": list(cache.get("actions") or []) + [a for a in actions
                                                                                         if a.get("id") not in seen],
                                             "merged_at": now.isoformat()})
        return {"merged": len(new)}

    def status(self, now: datetime) -> dict:
        cache, state = self.load(), self._read(self.state_path) or {}
        fetched_at = cache.get("fetched_at") if cache else None
        due = refresh_due_point(now)
        if cache is None or not fetched_at or state.get("ok") is False:
            status = "unavailable"
        else:
            try:
                fresh = us_clock.market_datetime(fetched_at) >= due
            except (TypeError, ValueError):
                fresh = None
            status = "unavailable" if fresh is None else ("connected" if fresh else "stale")
        return {"status": status, "fetched_at": fetched_at, "last_attempt_at": state.get("last_attempt_at"),
                "error": state.get("error"), "symbols": cache.get("symbols", []) if cache else [],
                "due_at": due.isoformat()}

    def usable(self, now: datetime) -> bool:
        return self.status(now)["status"] == "connected"

    def covers(self, symbol: str) -> bool:
        """最近一次成功拉取是否覆盖该标的 (未覆盖 = 不知道有没有公司行动, 不能开仓)。"""
        return symbol in set((self.load() or {}).get("symbols") or [])

    def actions(self) -> list[dict]:
        return list((self.load() or {}).get("actions", []))
