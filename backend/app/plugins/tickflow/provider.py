"""TickFlow 美股实时插件 (2026-10-02 产品决策: 执行报价部署即切 TickFlow 主源)。

正式 key 实测权限 (2026-10-01 13:34 ET):
- 可用: /v1/quotes (按标的 ≤50 只/次 60 次/分, 标的池 US_Equity 12381 只 20 次/分),
  /v1/klines(+batch) 1d, /v1/klines/ex-factors, /v1/instruments
- 不可用 (403): 分钟K / 日内分时 / 盘口 / 财务 / WebSocket

口径 (实测):
- 报价为全市场合并口径, 约 5 秒刷新; 成交量是全市场股数 (约 IEX 的 35 倍), 与 SIP 日K 量比 0.999
- 报价里的 open **不是**官方开盘价 (与 SIP 日K open 可差 31 bps) → 执行开盘价只取 SIP/IEX 09:30 分钟根
- 除权因子同样有代码换主污染 (CALY 27 条前任因子) → 必须过 ticker_handoff
- 退市股无数据

内部契约 (美股): symbol 纯 ticker (BRK.B), 成交量=股, 金额=美元, change_pct/amplitude/turnover 小数制,
日K OHLC 不复权, 分钟K datetime=美东墙钟 naive。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import date, datetime, timezone
from typing import Callable

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.market_clock import US_SESSION_OPEN, us_clock

logger = logging.getLogger(__name__)

BASE = "https://api.tickflow.org"
QUOTE_BATCH = 50          # 按标的报价: 50 只/次
DAILY_BATCH = 100         # 日K批量: 100 只/次
EXF_BATCH = 200           # 除权因子: 200 只/次
UNIVERSE = "US_Equity"
FEED = "tickflow"
HOT_TICK_MINUTES = 20     # 热报价保留 20 分钟 (覆盖 SIP 16 分钟延迟窗口)


class TickFlowHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"TickFlow HTTP {status}: {body[:160]}")
        self.status = status


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class RateLimiter:
    """进程内每分钟限频 (留 20% 余量); 线程安全, 匀速间隔。"""

    SAFETY = 1.25  # 间隔放大 25% ≈ 只用 80% 额度

    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute * self.SAFETY
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        # 锁内只预约时隙, 锁外睡: 间隔不变, 但等待中的线程不再挡住别人预约 (10-09: 撮合与热报价互相拖慢)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        wait = slot - now
        if wait > 0:
            _sleep(wait)


_LIMITS = {
    "quotes": RateLimiter(60),
    "universe": RateLimiter(20),
    "klines_batch": RateLimiter(30),
    "klines": RateLimiter(60),
    "ex_factors": RateLimiter(60),
    "meta": RateLimiter(60),
}


def _api_key() -> str:
    from app import secrets_store
    return secrets_store.get_env_backed_secret("tickflow_api_key", "TICKFLOW_API_KEY")


def _raw_get(path: str, key: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    from urllib.parse import urlencode
    url = BASE + path + (("?" + urlencode(params, safe=",")) if params else "")
    req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "zentrade/0.2",
                                               "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace")
        raise TickFlowHTTPError(e.code, body) from None


def probe_api_key(key: str) -> tuple[bool, str]:
    """设置页「先探后存」: 用候选 Key 实取一次 SPY 报价。"""
    if not key or not key.strip():
        return False, "Key 为空"
    try:
        body = _raw_get("/v1/quotes", key.strip(), {"symbols": "SPY.US"}, timeout=15)
    except TickFlowHTTPError as e:
        return False, f"探测失败 (HTTP {e.status})"
    except Exception as e:  # noqa: BLE001
        return False, f"探测失败: {type(e).__name__}"
    rows = (body or {}).get("data") or []
    if not rows:
        return False, "探测失败: 无 SPY 报价 (Key 可能无美股实时权限)"
    return True, "ok"


def availability() -> tuple[bool, str]:
    if not _api_key():
        return False, "未配置 TickFlow Key (可在设置页数据源卡片中直接填写)"
    return True, "ok"


def to_tf(symbol: str) -> str:
    return f"{symbol}.US"


def from_tf(symbol: str | None) -> str | None:
    if not symbol or not symbol.endswith(".US"):
        return None
    return symbol[:-3]


def _finite(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _handoff_filter(rows: list[dict]) -> list[dict]:
    """代码换主: 剔除列在现任代码名下、属于前任发行人的除权事件 (查询失败抛出, fail-closed)。"""
    if not rows:
        return rows
    from app.services import ticker_handoff
    span_start = min(r["trade_date"] for r in rows)
    span_end = max(r["trade_date"] for r in rows)
    foreign = ticker_handoff.foreign_dates_for(sorted({r["symbol"] for r in rows}), span_start, span_end)
    if not foreign:
        return rows
    kept = [r for r in rows if r["trade_date"] not in foreign.get(r["symbol"], ())]
    logger.warning("tickflow adj_factor: 代码换主剔除外来事件 %d 条 (%s)", len(rows) - len(kept),
                   ", ".join(sorted(foreign)[:10]))
    return kept


_KNOWN: dict = {"sig": None, "set": None}


def _known_symbols() -> set[str] | None:
    """全市场快照只保留本地维表里的普通股 + 核心指数 (TickFlow 池含权证/优先股/ETF 等 1.2 万只)。

    维表不存在时返回 None (不过滤, 由下游按维表分流)。按文件 mtime 缓存。
    """
    from app.config import settings
    from app.services.index_const import CORE_INDEX_SYMBOLS
    path = settings.data_dir / "instruments" / "instruments.parquet"
    try:
        sig = path.stat().st_mtime_ns
    except OSError:
        return None
    if _KNOWN["sig"] != sig:
        try:
            syms = set(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())
        except Exception:  # noqa: BLE001
            return None
        _KNOWN.update(sig=sig, set=syms | set(CORE_INDEX_SYMBOLS))
    return _KNOWN["set"]


class TickFlowProvider:
    name = "tickflow"
    capabilities = ProviderCapabilities(realtime=True)

    def __init__(self) -> None:
        self._ticks: dict[str, deque] = {}
        self._tick_lock = threading.Lock()
        self._official: dict[str, tuple[float, int, str]] = {}
        self._official_day: date | None = None
        self._alpaca = None
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        # .config 兼容垫片: 与 YAML 自定义源同形, 供 current_capability_set 读取 rpm/batch。
        # realtime = 标的池接口 (全市场一次调用, 20 次/分)。
        from types import SimpleNamespace
        self.config = SimpleNamespace(display_name="TickFlow (美股实时)", path=None,
                                      datasets={"realtime": SimpleNamespace(rpm=20, batch=None)})

    def close(self) -> None:  # 插件框架约定
        return None

    # ── HTTP ────────────────────────────────────────────────
    def _call(self, path: str, params: dict | None = None, *, limit: str = "quotes", timeout: float = 30.0) -> dict:
        key = _api_key()
        if not key:
            raise RuntimeError("TickFlow Key 未配置")
        _LIMITS[limit].acquire()
        try:
            body = _raw_get(path, key, params, timeout=timeout)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            raise
        self.last_error = None
        self.last_ok_at = time.time()
        return body

    # ── 热报价 (5 秒) → 分钟K 拼接 ───────────────────────────
    def _record_tick(self, symbol: str, ts_ms: int, price: float, cum_volume: float | None) -> None:
        with self._tick_lock:
            dq = self._ticks.setdefault(symbol, deque())
            if dq and dq[-1][0] >= ts_ms:
                return  # 同一源帧或乱序帧不重复记
            dq.append((ts_ms, price, cum_volume))
            horizon = ts_ms - HOT_TICK_MINUTES * 60_000
            while dq and dq[0][0] < horizon:
                dq.popleft()

    def stitched_minutes(self, symbol: str, since: datetime | None = None) -> list[dict]:
        """把热报价按美东分钟归并成 OHLC; 成交量 = 累计量相减 (首分钟无前值 → None, 不伪造)。

        实测 (59 根): 收盘差中位 0.39 bps, 量比中位 1.00, 分钟振幅捕获中位 77%。只用于显示与盘中提醒,
        不进回测与撮合; Alpaca 正式分钟K到达后由调用方替换。
        """
        with self._tick_lock:
            ticks = list(self._ticks.get(symbol, ()))
        bars: dict[datetime, dict] = {}
        prev_last_vol = None
        for ts, price, vol in ticks:
            dt = datetime.fromtimestamp(ts / 1000, us_clock.tz)
            if not (US_SESSION_OPEN <= dt.time() < us_clock.session_close(dt.date())):
                continue
            m = dt.replace(second=0, microsecond=0, tzinfo=None)
            b = bars.get(m)
            if b is None:
                b = bars[m] = {"symbol": symbol, "datetime": m, "open": price, "high": price, "low": price,
                               "close": price, "_first_vol": prev_last_vol, "_last_vol": vol, "stitched": True}
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            if vol is not None:
                b["_last_vol"] = vol
            prev_last_vol = b["_last_vol"]
        out = []
        for m in sorted(bars):
            b = bars[m]
            fv, lv = b.pop("_first_vol"), b.pop("_last_vol")
            b["volume"] = (lv - fv) if (fv is not None and lv is not None and lv >= fv) else None
            b["amount"] = None
            if since is None or m >= since:
                out.append(b)
        return out

    def hot_symbols(self) -> list[str]:
        with self._tick_lock:
            return list(self._ticks)

    def poll_hot_quotes(self, symbols: list[str]) -> int:
        """5 秒热报价: ≤50 只一批 (持仓/活跃卡/自选/正在看的个股), 只记录 tick 不落盘。"""
        n = 0
        symbols = list(dict.fromkeys(s for s in symbols if s))[:QUOTE_BATCH]
        if not symbols:
            return 0
        body = self._call("/v1/quotes", {"symbols": ",".join(to_tf(s) for s in symbols)}, limit="quotes", timeout=10)
        for q in body.get("data") or []:
            sym = from_tf(q.get("symbol"))
            price, ts = _finite(q.get("last_price")), q.get("timestamp")
            if not sym or price is None or price <= 0 or not isinstance(ts, (int, float)):
                continue
            self._record_tick(sym, int(ts), price, _finite(q.get("volume")))
            n += 1
        return n

    # ── realtime (全市场研究快照) ─────────────────────────────
    def _quote_row(self, q: dict, now: datetime) -> dict | None:
        sym = from_tf(q.get("symbol"))
        price, ts = _finite(q.get("last_price")), q.get("timestamp")
        if not sym or price is None or price <= 0 or not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return None
        ts = int(ts)
        ext = q.get("ext") or {}
        prev = _finite(q.get("prev_close"))
        vol, amt = _finite(q.get("volume")), _finite(q.get("amount"))
        quote_day = datetime.fromtimestamp(ts / 1000, us_clock.tz).date()
        same_day = quote_day == now.date()
        valid_volume = same_day and vol is not None and vol >= 0 and amt is not None and amt >= 0
        change_amount = _finite(ext.get("change_amount"))
        change_pct = _finite(ext.get("change_pct"))
        if change_pct is None and prev:
            change_amount = price - prev
            change_pct = change_amount / prev
        return {
            "symbol": sym, "name": ext.get("name") or sym,
            "last_price": price, "prev_close": prev,
            "open": _finite(q.get("open")) if same_day else None,
            "high": _finite(q.get("high")) if same_day else None,
            "low": _finite(q.get("low")) if same_day else None,
            "volume": vol if valid_volume else None,
            "amount": amt if valid_volume else None,
            "change_pct": change_pct, "change_amount": change_amount,
            "amplitude": _finite(ext.get("amplitude")),
            "turnover_rate": _finite(ext.get("turnover_rate")),
            "timestamp": ts, "quote_ts": ts, "volume_ts": ts if valid_volume else None,
            "feed": FEED, "quote_feed": FEED, "volume_feed": FEED,
            "historical_volume_feed": "sip",
            "data_delay": 0,
            "quote_status": "current" if same_day else "previous_session",
            "volume_status": "valid" if valid_volume else ("daily_date_mismatch" if not same_day else "missing_volume"),
            "session": "regular",
        }

    def get_realtime(self) -> list[dict]:
        """全市场一次调用 (≈1.3 s, gzip 1.5 MB)。软失败: 返回 [] + warning。"""
        try:
            body = self._call("/v1/quotes", {"universes": UNIVERSE}, limit="universe", timeout=60)
        except Exception as e:  # noqa: BLE001
            logger.warning("TickFlow 全市场报价失败: %s", type(e).__name__)
            return []
        now = us_clock.now()
        known = _known_symbols()
        out = []
        for q in body.get("data") or []:
            row = self._quote_row(q, now)
            if row is not None and (known is None or row["symbol"] in known):
                out.append(row)
        if not out:
            logger.warning("TickFlow 全市场报价为空或结构变化 (%d 行原始数据)", len(body.get("data") or []))
        return out

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """核心四只 (SPY/QQQ/DIA/IWM) 在 US_Equity 池中, 全市场快照已覆盖; 显式补拉保证指数卡不空。"""
        if not symbols:
            return []
        try:
            out = []
            now = us_clock.now()
            for i in range(0, len(symbols), QUOTE_BATCH):
                chunk = symbols[i:i + QUOTE_BATCH]
                body = self._call("/v1/quotes", {"symbols": ",".join(to_tf(s) for s in chunk)}, limit="quotes", timeout=15)
                out.extend(r for q in body.get("data") or [] if (r := self._quote_row(q, now)) is not None)
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("TickFlow 指数报价失败: %s", type(e).__name__)
            return None

    # ── 纸面撮合执行报价 ─────────────────────────────────────
    def _alpaca_provider(self):
        if self._alpaca is None:
            from app.plugins.alpaca.provider import AlpacaProvider
            self._alpaca = AlpacaProvider()
        return self._alpaca

    def _official_opens(self, symbols: list[str]) -> dict[str, tuple[float, int, str]]:
        """官方开盘价 = 09:30 分钟根 (IEX 当分钟有成交即用; 否则 09:46 后取 SIP)。复用 Alpaca 实现与缓存。"""
        today = us_clock.today()
        if self._official_day != today:
            self._official, self._official_day = {}, today
        missing = [s for s in symbols if s not in self._official]
        if missing:
            try:
                self._official.update(self._alpaca_provider().official_opens(missing))
            except Exception as e:  # noqa: BLE001
                logger.warning("官方开盘价获取失败 (Alpaca): %s", type(e).__name__)
        return {s: self._official[s] for s in symbols if s in self._official}

    def get_execution_quotes(self, symbols: list[str], *, max_age_seconds: float = 90.0) -> list[dict]:
        """纸面撮合报价: TickFlow 全市场合并口径最新价 + 源时间戳; open 只给官方 09:30 开盘价。

        只接受同日常规时段、非未来、年龄 ≤ max_age_seconds 的报价; 不取全市场、不下单。
        同时把报价写入热报价缓冲 (分钟K拼接复用, 不额外请求)。
        """
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive and finite")
        symbols = list(dict.fromkeys(s for s in symbols if s))
        out = []
        for i in range(0, len(symbols), QUOTE_BATCH):
            chunk = symbols[i:i + QUOTE_BATCH]
            body = self._call("/v1/quotes", {"symbols": ",".join(to_tf(s) for s in chunk)}, limit="quotes", timeout=10)
            # 审计 M-4: 拿到响应后再取时间 (Alpaca 同式); 请求前取的话, 请求期间的新成交 age<0 被当成未来报价丢掉
            now = us_clock.now()
            for q in body.get("data") or []:
                sym = from_tf(q.get("symbol"))
                price, ts = _finite(q.get("last_price")), q.get("timestamp")
                if not sym or sym not in chunk or price is None or price <= 0:
                    continue
                if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                    continue
                ts = int(ts)
                src = datetime.fromtimestamp(ts / 1000, us_clock.tz)
                age = (now - src).total_seconds()
                if age < 0 or age > max_age_seconds or src.date() != now.date():
                    continue
                if not (US_SESSION_OPEN <= src.time() < us_clock.session_close(src.date())) or not us_clock.is_trading_day(src.date()):
                    continue
                self._record_tick(sym, ts, price, _finite(q.get("volume")))
                out.append({"symbol": sym, "price": price, "timestamp": ts, "open": None,
                            "open_timestamp": None, "feed": FEED})
        if not out:
            return []
        opens = self._official_opens([r["symbol"] for r in out])
        for r in out:
            got = opens.get(r["symbol"])
            if got:
                r["open"], r["open_timestamp"], r["open_source"] = got
        return out

    # ── 日K / 除权因子 (研究交叉核对用; 生效源仍为 Alpaca SIP) ─────────
    def get_daily(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
                  asset_type: AssetType = "stock", on_chunk_done: Callable[[int, int], None] | None = None) -> pl.DataFrame:  # noqa: ARG002
        rows: list[dict] = []
        chunks = [symbols[i:i + DAILY_BATCH] for i in range(0, len(symbols), DAILY_BATCH)]
        for ci, chunk in enumerate(chunks):
            params = {"symbols": ",".join(to_tf(s) for s in chunk), "period": "1d", "adjust": "none", "count": 10000}
            if start_time:
                params["start_time"] = int(datetime.combine(start_time.date(), datetime.min.time(), us_clock.tz).timestamp() * 1000)
            if end_time:
                params["end_time"] = int(datetime.combine(end_time.date(), datetime.max.time(), us_clock.tz).timestamp() * 1000)
            body = self._call("/v1/klines/batch", params, limit="klines_batch", timeout=60)
            for tf_sym, k in (body.get("data") or {}).items():
                sym = from_tf(tf_sym)
                if not sym or not isinstance(k, dict):
                    continue
                for i, ts in enumerate(k.get("timestamp") or []):
                    rows.append({"symbol": sym, "date": datetime.fromtimestamp(ts / 1000, timezone.utc).date(),
                                 "open": k["open"][i], "high": k["high"][i], "low": k["low"][i], "close": k["close"][i],
                                 "volume": float(k["volume"][i] or 0),
                                 "amount": float(k["amount"][i]) if k.get("amount") and k["amount"][i] is not None else None})
            if on_chunk_done:
                on_chunk_done(ci + 1, len(chunks))
        if not rows:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
                                        "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64})
        return pl.DataFrame(rows).select(["symbol", "date", "open", "high", "low", "close", "volume", "amount"]).sort(["symbol", "date"])

    def get_adj_factors(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
                        asset_type: AssetType = "stock", on_chunk_done: Callable[[int, int], None] | None = None,  # noqa: ARG002
                        anchor_dates: dict | None = None) -> pl.DataFrame:  # noqa: ARG002
        rows: list[dict] = []
        chunks = [symbols[i:i + EXF_BATCH] for i in range(0, len(symbols), EXF_BATCH)]
        for ci, chunk in enumerate(chunks):
            params = {"symbols": ",".join(to_tf(s) for s in chunk)}
            if start_time:
                params["start_time"] = int(datetime.combine(start_time.date(), datetime.min.time(), us_clock.tz).timestamp() * 1000)
            if end_time:
                params["end_time"] = int(datetime.combine(end_time.date(), datetime.max.time(), us_clock.tz).timestamp() * 1000)
            body = self._call("/v1/klines/ex-factors", params, limit="ex_factors", timeout=60)
            for tf_sym, events in (body.get("data") or {}).items():
                sym = from_tf(tf_sym)
                if not sym or not isinstance(events, list):
                    continue
                for e in events:
                    f, ts = _finite(e.get("ex_factor")), e.get("timestamp")
                    if f is None or f <= 0 or not isinstance(ts, (int, float)):
                        continue
                    rows.append({"symbol": sym, "trade_date": datetime.fromtimestamp(ts / 1000, us_clock.tz).date(), "ex_factor": f})
            if on_chunk_done:
                on_chunk_done(ci + 1, len(chunks))
        rows = _handoff_filter(rows)
        if not rows:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64})
        return pl.DataFrame(rows).select(["symbol", "trade_date", "ex_factor"]).sort(["symbol", "trade_date"])

    def status(self) -> dict:
        with self._tick_lock:
            hot = {s: len(d) for s, d in self._ticks.items()}
        return {"last_error": self.last_error, "last_ok_at": self.last_ok_at, "hot_symbols": len(hot),
                "hot_ticks": sum(hot.values()), "official_opens": len(self._official)}
