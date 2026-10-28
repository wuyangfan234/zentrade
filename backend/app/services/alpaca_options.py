"""免费期权链 (0.23.2): Alpaca indicative 快照 (报价/成交量/IV/希腊值) + 纸面交易 API 合约列表 (持仓量, T-1)。

不扣费; 打开个股期权页时按需拉, 按美东日缓存 data/options_free/<SYM>/<日>.json。
输出行与 QVeris REALTIME_OPTIONS 同字段, 直接喂 options_analytics.analyze。只展示, 不进打分/交易。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

HORIZON_DAYS = 70          # 只取 70 天内到期 (热力图最多 6 个到期日, 期限结构够用)
MAX_PAGES = 20


def _dir(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / "options_free" / symbol.upper()


def _creds():
    from app.data_providers.custom import get_provider
    p = get_provider("alpaca")
    return p.key, p.secret


def _contracts(symbol: str, today: date, key: str, secret: str) -> dict[str, dict]:
    from app.plugins.alpaca.provider import PAPER_BASE, _get
    out: dict[str, dict] = {}
    token = None
    for _ in range(MAX_PAGES):
        q = {"underlying_symbols": symbol, "expiration_date_gte": today.isoformat(),
             "expiration_date_lte": (today + timedelta(days=HORIZON_DAYS)).isoformat(), "limit": 10000}
        if token:
            q["page_token"] = token
        body = _get(f"{PAPER_BASE}/options/contracts?{urlencode(q)}", key, secret)
        for c in body.get("option_contracts") or []:
            out[c["symbol"]] = c
        token = body.get("next_page_token")
        if not token:
            break
    return out


def _snapshots(symbol: str, today: date, key: str, secret: str) -> dict[str, dict]:
    from app.plugins.alpaca.provider import DATA_BASE, _get
    base = DATA_BASE.rsplit("/v2", 1)[0]
    out: dict[str, dict] = {}
    token = None
    for _ in range(MAX_PAGES):
        q = {"feed": "indicative", "limit": 1000,
             "expiration_date_lte": (today + timedelta(days=HORIZON_DAYS)).isoformat()}
        if token:
            q["page_token"] = token
        body = _get(f"{base}/v1beta1/options/snapshots/{symbol}?{urlencode(q)}", key, secret)
        out.update(body.get("snapshots") or {})
        token = body.get("next_page_token")
        if not token:
            break
    return out


def build_rows(contracts: dict[str, dict], snaps: dict[str, dict], today: date) -> list[dict]:
    rows = []
    for sym, c in contracts.items():
        s = snaps.get(sym) or {}
        q, g, bar = s.get("latestQuote") or {}, s.get("greeks") or {}, s.get("dailyBar") or {}
        bid, ask = q.get("bp"), q.get("ap")
        mark = (bid + ask) / 2 if bid and ask and ask >= bid > 0 else None
        bar_day = str(bar.get("t") or "")[:10]
        rows.append({
            "contractID": sym, "symbol": c.get("underlying_symbol"), "expiration": c.get("expiration_date"),
            "strike": c.get("strike_price"), "type": c.get("type"), "bid": bid, "ask": ask, "mark": mark,
            "last": (s.get("latestTrade") or {}).get("p"),
            # 当日成交量: 只认最近交易日的日 bar
            "volume": bar.get("v") if bar_day >= (today - timedelta(days=4)).isoformat() else 0,
            "open_interest": c.get("open_interest") or 0, "implied_volatility": s.get("impliedVolatility"),
            "delta": g.get("delta"), "gamma": g.get("gamma"), "theta": g.get("theta"), "vega": g.get("vega"),
            "date": c.get("open_interest_date") or today.isoformat(),
        })
    return rows


def chain(data_dir: Path, symbol: str, *, today: date, fetch=None) -> dict:
    """{available, rows, fetched_at, source, cached}。当天已拉过 → 读缓存。"""
    from datetime import datetime, timezone
    symbol = symbol.upper()
    p = _dir(data_dir, symbol) / f"{today.isoformat()}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return {**d, "cached": True}
        except (OSError, ValueError):
            pass
    try:
        if fetch is not None:
            contracts, snaps = fetch(symbol, today)
        else:
            key, secret = _creds()
            contracts, snaps = _contracts(symbol, today, key, secret), _snapshots(symbol, today, key, secret)
    except Exception as e:  # noqa: BLE001
        logger.warning("alpaca options %s failed: %s", symbol, type(e).__name__)
        return {"available": False, "reason": "免费期权源暂不可用", "source": "alpaca"}
    rows = build_rows(contracts, snaps, today)
    if not rows:
        return {"available": False, "reason": "该股无期权或数据源无链", "source": "alpaca"}
    d = {"available": True, "rows": rows, "source": "alpaca",
         "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    from app.services.fs_utils import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d))
    return {**d, "cached": False}
