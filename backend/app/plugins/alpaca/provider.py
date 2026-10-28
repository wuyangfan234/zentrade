"""Alpaca provider — ZenTrade 唯一内置数据源 (P1-4)。

能力 (P0 实证, 见 p0-probe/REPORT.md):
- daily / minute: feed=sip 合并磁带全历史 (量口径真实)
- realtime: feed=delayed_sip 合并磁带快照 (15 分钟延迟); IEX 降级仅价格
- execution quotes: 定向 IEX 成交价, 独立验证源时间, 仅供纸面撮合
- adj_factor: adjustment=raw 与 all 双拉按日比值重建 (NVDA 拆股对账零误差)
- instruments: NYSE+NASDAQ tradable 普通股 (资产清单走 paper-api 域名)

内部契约: symbol=纯 ticker (AAPL); 日K date=美东日期; 分钟K datetime=美东墙钟 naive
(与 kline_sync._enforce_minute_market_wallclock 同口径); raw OHLC 不复权。
"""
from __future__ import annotations

import json
import logging
import math
import time
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.market_clock import US_SESSION_OPEN, us_clock

logger = logging.getLogger(__name__)

DATA_BASE = "https://data.alpaca.markets/v2"
PAPER_BASE = "https://paper-api.alpaca.markets/v2"
_BATCH = 100          # bars 端点单请求标的上限
_SNAPSHOT_BATCH = 1000  # snapshots 独立批量; 不改变历史 bars 的分页/时间契约
OPEN_RETRY_S = 300.0  # 09:46 后仍无 09:30 开盘根的标的: 重试间隔 (秒)
_PAGE_LIMIT = 10000   # 单页 bar 数上限
_SNAPSHOT_DELAY_SECONDS = 900
_SNAPSHOT_STALE_SLACK_SECONDS = 120


def _as_utc(value: datetime | None) -> datetime | None:
    """把调用方的窗口统一成带时区的 UTC。

    ZenTrade 的 naive datetime 契约是美东墙钟；Alpaca 查询参数却必须明确按
    UTC 解释。集中在 HTTP 边界转换，禁止把美东 naive 值直接拼成 ``...Z``。
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=us_clock.tz)
    return aware.astimezone(timezone.utc)


def _load_credentials() -> tuple[str, str]:
    """取 Alpaca 凭据 (统一规则见 app/credentials.py)。永不打印。"""
    from app import credentials
    k, s, _src = credentials.alpaca()
    return k, s


def probe_api_key(key: str) -> tuple[bool, str]:
    """设置页「先探后存」契约: 用候选 Key 实探一次。"""
    if not key or ":" not in key:
        return False, "格式应为 KEY_ID:SECRET"
    k, s = key.split(":", 1)
    try:
        _get(f"{DATA_BASE}/stocks/SPY/snapshot", k, s)
    except Exception as e:  # noqa: BLE001
        return False, f"探测失败: {e}"
    return True, "ok"


def availability() -> tuple[bool, str]:
    k, s = _load_credentials()
    if not k or not s:
        return False, "未配置 Alpaca Key (KEY_ID:SECRET)"
    return True, "ok"


def _get(url: str, key: str, secret: str, *, retries: int = 3, timeout: float = 30.0) -> dict:
    """单次 GET, 带 429/5xx 指数退避。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
            "User-Agent": "zentrade/0.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt
                logger.warning("alpaca %s → HTTP %s, retry in %ds", url.split("?")[0], e.code, wait)
                time.sleep(wait)
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_err or RuntimeError("alpaca request failed")


def _iter_bars(key: str, secret: str, symbols: list[str], timeframe: str,
               start: datetime | None, end: datetime | None, adjustment: str,
               feed: str, on_chunk_done=None, on_request=None):
    """按 batch 分块 + 分页拉 bars, 产出 (symbol, bar_dict) 流。"""
    # HTTP 窗口统一用带时区的 UTC；调用方传入的 naive 值按美东墙钟解释。
    start = _as_utc(start)
    end = _as_utc(end)
    # SIP 免费订阅扣留最近 15 分钟 (403): 请求窗口右端钳制到 now-16min。
    # 9-29 审计实证: 盘后管道以 now 为右端重建当日官方日K, 整批 403 静默失败,
    # 当日 K 线残留 IEX 快照小量 (AAPL 108万 vs 真实 3000万), 成交额口径塌缩 30 倍。
    if feed == "sip":
        from datetime import timedelta as _td
        clamp_aware = us_clock.now().astimezone(timezone.utc) - _td(minutes=16)
        if end is None or end > clamp_aware:
            end = clamp_aware
    if start is not None and end is not None and start > end:
        return  # SIP 延迟钳制后尚无可取数据；不要向上游发反向窗口
    start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None
    chunks = [symbols[i:i + _BATCH] for i in range(0, len(symbols), _BATCH)]
    for ci, chunk in enumerate(chunks):
        page_token: str | None = None
        while True:
            qs = (f"symbols={','.join(chunk)}&timeframe={timeframe}"
                  f"&limit={_PAGE_LIMIT}&adjustment={adjustment}&feed={feed}&currency=USD")
            if start_s:
                qs += f"&start={start_s}"
            if end_s:
                qs += f"&end={end_s}"
            if page_token:
                qs += f"&page_token={page_token}"
            if on_request is not None:
                on_request()
            body = _get(f"{DATA_BASE}/stocks/bars?{qs}", key, secret)
            for sym, bars in (body.get("bars") or {}).items():
                for bar in bars:
                    yield sym, bar
            page_token = body.get("next_page_token")
            if not page_token:
                break
        if on_chunk_done:
            on_chunk_done(ci + 1, len(chunks))


class AlpacaProvider:
    """Alpaca 数据源 (插件契约实现)。"""

    name = "alpaca"
    capabilities = ProviderCapabilities(
        instruments=True, daily=True, adj_factor=True, minute=True,
        realtime=True, financial=False,
    )
    minute_history_days = None  # SIP 全历史
    minute_delay_minutes = 16  # 免费 SIP 历史端点保守右端延迟
    minute_refresh_interval_seconds = 60
    minute_feed = "sip"

    def __init__(self) -> None:
        self.key, self.secret = _load_credentials()
        self._minute_metrics = threading.local()
        self._execution_opens: dict[str, tuple[float, int, str]] = {}
        self._execution_open_day: date | None = None
        self._open_retry_at: dict[str, float] = {}  # 开盘根缺失的标的: monotonic 下次重试时刻
        # .config 兼容垫片: 与 YAML 自定义源 (GenericHTTPProvider) 同形,
        # 供 current_capability_set / 各服务的 cfg.rpm/cfg.batch 消费 (Alpaca 200rpm/100批 实测)。
        from types import SimpleNamespace
        self.config = SimpleNamespace(datasets={
            ds: SimpleNamespace(rpm=200, batch=_SNAPSHOT_BATCH if ds == "realtime" else _BATCH)
            for ds in ("daily", "adj_factor", "realtime", "minute", "full_minute")
        })

    def close(self) -> None:
        pass

    # ── 标的维表 ────────────────────────────────────────────
    def get_instruments(self, asset_type: AssetType = "stock") -> list[dict]:
        """active us_equity ∩ NYSE/NASDAQ tradable, 附证券类型/风险标记。

        契约 (instrument_sync._fetch_instruments_via_provider): 返回 list[dict],
        与 tickflow SDK 同款 (provider.get_instruments("stock") or [] → flatten)。
        """
        body = _get(f"{PAPER_BASE}/assets?status=active&asset_class=us_equity", self.key, self.secret)
        rows = []
        for a in body:
            if a.get("exchange") not in ("NYSE", "NASDAQ"):
                continue
            if not a.get("tradable") or a.get("status") != "active":
                continue
            sym = str(a.get("symbol") or "")
            if not sym:  # 点号也用于普通股类别 (BRK.B), 不按标点误删
                continue
            from app.services.us_reference import classify_instrument
            rows.append({
                "symbol": sym,
                "name": str(a.get("name") or ""),
                "code": sym,
                "exchange": a.get("exchange"),
                "asset_type": "stock",
                "source": "alpaca",
                "region": "US",
                **classify_instrument(a),
            })
        return rows

    def instruments_df(self) -> pl.DataFrame:
        """DataFrame 版标的表 (扫描/回放便利; instrument_sync 用 get_instruments)。"""
        rows = self.get_instruments()
        if not rows:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "name": pl.Utf8, "code": pl.Utf8,
                                        "exchange": pl.Utf8, "asset_type": pl.Utf8, "source": pl.Utf8})
        return pl.DataFrame(rows)

    # ── 日K ────────────────────────────────────────────────
    @staticmethod
    def _daily_rows_to_frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows).with_columns(
            pl.col("date").str.to_date(strict=False),
        ).sort(["symbol", "date"])

    @staticmethod
    def _daily_row(sym: str, bar: dict) -> dict:
        vol = float(bar.get("v") or 0)
        vw = bar.get("vw")
        return {
            "symbol": sym,
            "date": str(bar["t"])[:10],  # 日K t=当日 04:00 UTC = 美东午夜, 日期即美东日
            "open": bar.get("o"), "high": bar.get("h"),
            "low": bar.get("l"), "close": bar.get("c"),
            "volume": vol, "amount": vol * float(vw) if vw is not None else None,
        }

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        rows = [self._daily_row(sym, bar) for sym, bar in _iter_bars(
            self.key, self.secret, symbols, "1Day", start_time, end_time, "raw", "sip", on_chunk_done)]
        return self._daily_rows_to_frame(rows)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        on_chunk_done: Callable[[int, int], None] | None = None,
    ):
        """按 100 只一批流式产出日K (多年回填不能整表常驻内存, 由同步层先落 staging 再提交)。"""
        chunks = [symbols[i:i + _BATCH] for i in range(0, len(symbols), _BATCH)]
        for ci, chunk in enumerate(chunks):
            rows = [self._daily_row(sym, bar) for sym, bar in _iter_bars(
                self.key, self.secret, chunk, "1Day", start_time, end_time, "raw", "sip", None)]
            frame = self._daily_rows_to_frame(rows)
            if not frame.is_empty():
                yield frame
            if on_chunk_done:
                on_chunk_done(ci + 1, len(chunks))

    # ── 除权事件 (raw/all 双拉 → 事件前后价格比, adj basis event_v2) ─────
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        on_chunk_done: Callable[[int, int], None] | None = None,
        anchor_dates: dict[str, date] | None = None,
    ) -> pl.DataFrame:
        """返回除权**事件**行: symbol, trade_date, ex_factor(事件前价格÷事件后), vol_factor。

        raw/all 收盘比是「相对现在」的水平值, 只在除权日跳变; 逐日落盘会被下游
        累乘成错误的复权链 (9-30 审计: 拆股未复权)。这里取相邻交易日水平之比,
        只保留超出分位舍入误差的真实事件。窗口首日没有前一日可比, 调用方应让
        窗口向前多留至少一个交易日。

        anchor_dates: symbol → 窗口前最近一个有效日K日期。长停牌票在增量窗口里
        没有前一根可比较的 bar, 按锚点单独把窗口拉长, 复牌首日的拆股才能被识别。
        """
        from app.services.adjustment_events import events_for_symbols

        anchor_dates = anchor_dates or {}
        window_start = start_time.date() if start_time else None
        groups: dict[datetime | None, list[str]] = {}
        for sym in symbols:
            anchor = anchor_dates.get(sym)
            begin = start_time
            if anchor is not None and window_start is not None and anchor < window_start:
                begin = datetime.combine(anchor, datetime.min.time())
            groups.setdefault(begin, []).append(sym)
        chunks = [(begin, syms[i:i + _BATCH]) for begin, syms in groups.items()
                  for i in range(0, len(syms), _BATCH)]
        rows: list[dict] = []
        for ci, (begin, chunk) in enumerate(chunks):
            # 因子用于整表迁移和历史重算；部分成功不能伪装成完整空集/事件表。
            # 传播失败让调用方保留旧产物，不允许随后发布错误的 enriched。
            raw = list(_iter_bars(self.key, self.secret, chunk, "1Day",
                                  begin, end_time, "raw", "sip", None))
            adjusted = list(_iter_bars(self.key, self.secret, chunk, "1Day",
                                       begin, end_time, "all", "sip", None))
            rows.extend(events_for_symbols(raw, adjusted))
            if on_chunk_done:
                on_chunk_done(ci + 1, len(chunks))
        if rows:
            # 代码换主: 源端会把前任发行人的分红/拆股套到现任价格上, 必须剔除。
            # 查询失败直接抛出 (fail-closed), 不带着可能被污染的事件落盘。
            from app.services import ticker_handoff
            span_start = min(r["trade_date"] for r in rows)
            span_end = max(r["trade_date"] for r in rows)
            foreign = ticker_handoff.foreign_dates_for(sorted({r["symbol"] for r in rows}), span_start, span_end)
            if foreign:
                before = len(rows)
                rows = [r for r in rows if r["trade_date"] not in foreign.get(r["symbol"], ())]
                logger.warning("adj_factor: 代码换主剔除外来事件 %d 条 (%s)", before - len(rows),
                               ", ".join(sorted(foreign)[:10]))
        if not rows:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date,
                                        "ex_factor": pl.Float64, "vol_factor": pl.Float64})
        return pl.DataFrame(rows, schema={"symbol": pl.Utf8, "trade_date": pl.Date,
                                          "ex_factor": pl.Float64, "vol_factor": pl.Float64},
                            ).sort(["symbol", "trade_date"])

    # ── 研究快照 (delayed SIP) / 纸面撮合报价 (IEX) ──────────
    def get_realtime(self) -> list[dict]:
        """全市场延迟合并快照。IEX 仅作有标记的价格降级, 不提供混算量能。"""
        instruments = self.instruments_df()
        if instruments.is_empty():
            return []
        return self._snapshot_records(instruments["symbol"].to_list())

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数快照补充 (quote_service 指数缓存钩子)。

        核心四只 SPY/DIA/IWM 挂 NYSE Arca, 不在普通股 universe (仅 NYSE/NASDAQ
        主板), get_realtime 永远拉不到 —— 必须按显式清单独立补拉。
        """
        if not symbols:
            return []
        return self._snapshot_records(list(symbols))

    def _snapshot_records(self, symbols: list[str]) -> list[dict]:
        """研究用快照; 所有时间为源 epoch ms, data_delay 单位为秒。

        dailyBar.t 是日桶标签而不是累计量的更新时间。volume_ts 取同日同 feed
        的最新 trade/minute 源时间, 不借用请求时间或另一个 feed 的成交时间。
        """
        out: list[dict] = []
        symbols = list(dict.fromkeys(symbols))
        chunks = [symbols[i:i + _SNAPSHOT_BATCH] for i in range(0, len(symbols), _SNAPSHOT_BATCH)]
        for chunk in chunks:
            feed = "delayed_sip"
            try:
                snapshots = self._get_snapshots(chunk, feed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("delayed_sip snapshot batch failed; IEX price-only fallback: %s", exc)
                feed = "iex"
                snapshots = self._get_snapshots(chunk, feed)
            now = us_clock.now()
            for sym, snap in snapshots.items():
                if sym not in chunk or not isinstance(snap, dict):
                    continue
                daily = snap.get("dailyBar") or {}
                prev = snap.get("prevDailyBar") or {}
                trade = snap.get("latestTrade") or {}
                minute = snap.get("minuteBar") or {}
                last, quote_ts = None, None
                for value, stamp in ((trade.get("p"), trade.get("t")), (minute.get("c"), minute.get("t"))):
                    price, ts = _positive_number(value), _rfc3339_ms(stamp)
                    if price is not None and ts is not None:
                        last, quote_ts = price, ts
                        break
                delay = _SNAPSHOT_DELAY_SECONDS if feed == "delayed_sip" else 0
                quote_status = _quote_status(quote_ts, now, delay + _SNAPSHOT_STALE_SLACK_SECONDS)
                daily_ts = _rfc3339_ms(daily.get("t"))
                same_day = daily_ts is not None and _source_day(daily_ts) == now.date()
                volume_ts = max((ts for ts in (
                    _rfc3339_ms(trade.get("t")), _rfc3339_ms(minute.get("t")),
                ) if ts is not None and _source_day(ts) == now.date()), default=None) if same_day else None
                if feed == "iex":
                    volume_status = "iex_volume_disabled"
                elif not same_day:
                    volume_status = "daily_date_mismatch"
                elif _quote_status(volume_ts, now, delay + _SNAPSHOT_STALE_SLACK_SECONDS) != "current":
                    volume_status = "invalid_volume_time"
                else:
                    volume_status = "valid"
                volume = _nonnegative_number(daily.get("v")) if volume_status == "valid" else None
                if volume_status == "valid" and volume is None:
                    volume_status = "missing_volume"
                vw = _positive_number(daily.get("vw"))
                prev_close = prev.get("c")
                change_amount = (float(last) - float(prev_close)) if last and prev_close else None
                change_pct = (change_amount / float(prev_close)) if change_amount is not None and prev_close else None
                out.append({
                    "symbol": sym,
                    "name": sym,
                    "last_price": last,
                    "prev_close": prev_close,
                    "open": daily.get("o") if same_day else None,
                    "high": daily.get("h") if same_day else None,
                    "low": daily.get("l") if same_day else None,
                    "volume": volume,
                    "amount": volume * vw if volume is not None and vw is not None else None,
                    "change_pct": change_pct,
                    "change_amount": change_amount,
                    "amplitude": None,   # IEX 快照无; 由 pipeline 按价格口径回算
                    "turnover_rate": None,  # 由 pipeline 按股本口径回算
                    "timestamp": quote_ts,
                    "quote_ts": quote_ts,
                    "volume_ts": volume_ts,
                    "daily_ts": daily_ts,
                    "feed": feed, "quote_feed": feed, "volume_feed": feed,
                    "historical_volume_feed": "sip",
                    "data_delay": delay,
                    "quote_status": quote_status,
                    "volume_status": volume_status,
                    "session": "regular",
                })
        return out

    def _get_snapshots(self, symbols: list[str], feed: str) -> dict:
        from urllib.parse import urlencode
        query = urlencode({"symbols": ",".join(symbols), "feed": feed, "currency": "USD"}, safe=",")
        body = _get(f"{DATA_BASE}/stocks/snapshots?{query}", self.key, self.secret)
        if not isinstance(body, dict):
            raise ValueError("invalid snapshot response")
        snapshots = body.get("snapshots", body)
        if not isinstance(snapshots, dict):
            raise ValueError("invalid snapshots object")
        return snapshots

    def get_execution_quotes(self, symbols: list[str], *, max_age_seconds: float = 90.0) -> list[dict]:
        """只读的小池 IEX 纸面报价, 不取全市场、不下单、不回用研究缓存。

        返回 {symbol, price, timestamp, open, open_timestamp, feed}; 时间均为 epoch ms。
        仅接受同日常规时段、非未来、年龄≤max_age_seconds 的 latestTrade。
        open 只取实际 09:30 IEX 分钟根的 o/t; 日桶 midnight 不伪装为开盘时间。
        无该根时 open/open_timestamp 均 None; 调用方不得用最新价代替次日开盘价。
        """
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive and finite")
        symbols = list(dict.fromkeys(symbols))
        out = []
        for i in range(0, len(symbols), _SNAPSHOT_BATCH):
            chunk = symbols[i:i + _SNAPSHOT_BATCH]
            snapshots = self._get_snapshots(chunk, "iex")
            now = us_clock.now()
            for sym in chunk:
                snap = snapshots.get(sym)
                if not isinstance(snap, dict):
                    continue
                trade = snap.get("latestTrade") or {}
                price, ts = _positive_number(trade.get("p")), _rfc3339_ms(trade.get("t"))
                if price is None or _quote_status(ts, now, max_age_seconds) != "current":
                    continue
                source = datetime.fromtimestamp(ts / 1000, us_clock.tz)
                if not (US_SESSION_OPEN <= source.time() < us_clock.session_close(source.date())) or not us_clock.is_trading_day(source.date()):
                    continue
                out.append({"symbol": sym, "price": price, "timestamp": ts,
                            "open": None, "open_timestamp": None, "feed": "iex"})
        if not out:
            return []
        self.official_opens([r["symbol"] for r in out])
        now = us_clock.now()
        for row in out:
            price, ts, source = self._execution_opens.get(row["symbol"], (None, None, None))
            row["open"], row["open_timestamp"] = price, ts
            if source:
                row["open_source"] = source
        # 开盘根的网络请求也消耗时间: 返回前按完成时点再验, 不让过期成交价混入。
        return [r for r in out if _quote_status(r["timestamp"], now, max_age_seconds) == "current"]

    def official_opens(self, symbols: list[str]) -> dict[str, tuple[float, int, str]]:
        """当日官方开盘价 = 09:30 那一分钟根的开盘价 (先 IEX; 无则 09:46 后取 SIP 合并磁带)。

        不依赖最新成交是否新鲜, 冷门股也能拿到; 结果按交易日缓存。返回 {symbol: (price, ts_ms, source)}。
        """
        symbols = list(dict.fromkeys(s for s in symbols if s))
        today = us_clock.today()
        if self._execution_open_day != today:
            self._execution_opens = {}
            self._open_retry_at = {}
            self._execution_open_day = today
        start = datetime.combine(today, US_SESSION_OPEN, tzinfo=us_clock.tz)
        open_ms = int(start.timestamp() * 1000)
        if us_clock.now() < start:
            return {}
        mono = time.monotonic()
        missing = [s for s in symbols if s not in self._execution_opens and self._open_retry_at.get(s, 0) <= mono]
        if missing:
            try:
                for sym, bar in _iter_bars(self.key, self.secret, missing, "1Min", start,
                                           start + timedelta(seconds=59), "raw", "iex"):
                    ts, price = _rfc3339_ms(bar.get("t")), _positive_number(bar.get("o"))
                    if sym in missing and price is not None and ts == open_ms:
                        self._execution_opens[sym] = (price, ts, "iex")
            except Exception as exc:  # noqa: BLE001
                logger.warning("IEX opening minute unavailable; open-based paper fills disabled: %s", exc)
        # 冷门股在 IEX 的首笔常晚 1–4 分钟 → 09:30 IEX 分钟根不存在。SIP 合并磁带同一分钟根
        # 是全市场官方开盘口径, 但免费档只能查 16 分钟前的数据: 09:46 后补取一次。
        still = [s for s in missing if s not in self._execution_opens]
        if still and us_clock.now() >= start + timedelta(minutes=16):
            try:
                for sym, bar in _iter_bars(self.key, self.secret, still, "1Min", start,
                                           start + timedelta(seconds=59), "raw", "sip"):
                    ts, price = _rfc3339_ms(bar.get("t")), _positive_number(bar.get("o"))
                    if sym in still and price is not None and ts == open_ms:
                        self._execution_opens[sym] = (price, ts, "sip")
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIP opening minute unavailable: %s", exc)
        # SIP 补取窗口 (09:46) 之后仍缺 = 当日 09:30 无成交, 5 分钟内不再重拉
        if us_clock.now() >= start + timedelta(minutes=16):
            for s in missing:
                if s not in self._execution_opens:
                    self._open_retry_at[s] = mono + OPEN_RETRY_S
        return {s: self._execution_opens[s] for s in symbols if s in self._execution_opens}

    # ── 分钟K (SIP, 美东墙钟 naive) ───────────────────────────
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        freq: str = "1m",  # noqa: ARG002
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        rows = []
        self._minute_metrics.requests = 0

        def count_request():
            self._minute_metrics.requests += 1

        for sym, bar in _iter_bars(self.key, self.secret, symbols, "1Min",
                                   start_time, end_time, "raw", "sip", on_chunk_done,
                                   on_request=count_request):
            dt = _utc_ms_to_et_naive(bar["t"])
            # Alpaca minute bars also include pre/post-market. The internal
            # minute contract is regular US session only: 09:30 <= t < 16:00 ET.
            if not (US_SESSION_OPEN <= dt.time() < us_clock.session_close(dt.date())):   # 半日市 13:00 (审计 L-1)
                continue
            rows.append({
                "symbol": sym,
                "datetime": dt,
                "open": bar.get("o"), "high": bar.get("h"),
                "low": bar.get("l"), "close": bar.get("c"),
                "volume": float(bar.get("v") or 0),
                "amount": (float(bar["v"]) * float(bar["vw"]))
                          if bar.get("v") and bar.get("vw") else None,
            })
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows).sort(["symbol", "datetime"])

    @property
    def minute_request_count(self) -> int:
        return getattr(self._minute_metrics, "requests", 0)

    def get_intraday_since(self, symbols: list[str], start_time: datetime) -> pl.DataFrame:
        """调用方拥有水位：每轮只取重叠尾窗，不全市场重拉全天。"""
        return self.get_minute(symbols, start_time, us_clock.now())

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,  # noqa: ARG002
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """全量分钟冷启动修复：当日开盘至当前，保留带时区窗口。"""
        end = us_clock.now()
        start = datetime.combine(end.date(), US_SESSION_OPEN, tzinfo=us_clock.tz)
        return self.get_minute(symbols, start, end, asset_type)


def _rfc3339_ms(t: str | None) -> int | None:
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _nonnegative_number(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _positive_number(value) -> float | None:
    number = _nonnegative_number(value)
    return number if number is not None and number > 0 else None


def _source_day(ts: int) -> date:
    return datetime.fromtimestamp(ts / 1000, us_clock.tz).date()


def _quote_status(ts: int | None, now: datetime, max_age_seconds: float) -> str:
    if ts is None:
        return "missing_source_time"
    age = now.timestamp() - ts / 1000
    if age < 0:
        return "future_source_time"
    if _source_day(ts) != now.date() or age > max_age_seconds:
        return "stale"
    return "current"


def _utc_ms_to_et_naive(t: str) -> datetime:
    """RFC3339 bar 时间 (UTC) → 美东墙钟 naive。"""
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return dt.astimezone(us_clock.tz).replace(tzinfo=None)
