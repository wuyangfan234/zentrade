"""30 分钟K / 4h K 线数据层 — 卡结算、盘前检查、盘前放量排序、数据新鲜度共用; 可选的 4h 方向门插件也读它。

数据: Alpaca SIP 30 分钟线, 04:00–20:00 ET 全时段 (原系统 yfinance prepost=True 同口径), 不复权原始价;
      按月分区存 data/kline_4h_src/m30/<YYYY-MM>.parquet (列 symbol,t(UTC ISO),open,high,low,close,volume)。
合成 (仅 4h 插件用): 30 分钟线转美东墙钟 (naive) → 4h 分箱 (09:30/13:30/17:30/21:30/01:30/05:30)。
      喂墙钟是因为原函数吃带时区索引时 pandas 会在夏令时切换后整体偏移 1 小时 (实测 09:30→08:30)。
复权: 价格 × 当日复权系数 (系统日K close/raw_close, 与日K同一口径); 成交量 ÷ 系数。
每日增量: 盘后拉“最近一个月”不完整分区补齐 (幂等重写该月)。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def src_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "kline_4h_src" / "m30"


def _month_path(data_dir: Path, month: str) -> Path:
    return src_dir(data_dir) / f"{month}.parquet"


def fetch_days(provider, symbols: list[str], days: list[date], *, threads: int = 4) -> pl.DataFrame:
    """拉指定交易日的 30 分钟线 (04:00–19:59 ET, raw)。provider = AlpacaProvider。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.plugins.alpaca import provider as ap

    def one(d: date):
        rows = []
        lo = datetime.combine(d, datetime.min.time()).replace(hour=4)
        hi = datetime.combine(d, datetime.min.time()).replace(hour=19, minute=59)
        for s, b in ap._iter_bars(provider.key, provider.secret, symbols, "30Min", lo, hi, "raw", "sip"):
            rows.append((s, b["t"], float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"]), d.isoformat()))
        return rows

    with ThreadPoolExecutor(threads) as ex:
        parts = list(ex.map(one, days))
    rows = [r for p in parts for r in p]
    return pl.DataFrame(rows, schema=["symbol", "t", "open", "high", "low", "close", "volume", "day"], orient="row")


SCHEMA = {"symbol": pl.Utf8, "t": pl.Utf8, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
          "close": pl.Float64, "volume": pl.Float64}


def _canon(df: pl.DataFrame) -> pl.DataFrame:
    """统一列与类型 (Alpaca JSON 的整数价/量会被推断成 Int64, 与增量的 Float64 不能直接拼接)。"""
    return df.select([pl.col(c).cast(t) for c, t in SCHEMA.items()])


def write_month(data_dir: Path, month: str, df: pl.DataFrame) -> None:
    from app.services.fs_utils import atomic_write_parquet
    p = _month_path(data_dir, month)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(_canon(df).unique(subset=["symbol", "t"], keep="last").sort(["symbol", "t"]), p)


def load_m30(data_dir: Path, symbols: list[str] | None, start: date, end: date) -> pl.DataFrame:
    files = []
    m = date(start.year, start.month, 1)
    while m <= end:
        p = _month_path(data_dir, m.strftime("%Y-%m"))
        if p.exists():
            files.append(p)
        m = (m + timedelta(days=32)).replace(day=1)
    if not files:
        return pl.DataFrame(schema=SCHEMA)
    parts = []
    for f in files:  # 逐文件读并统一类型 (历史分区可能是 Int64 量)
        lf = pl.scan_parquet(str(f))
        if symbols is not None:
            lf = lf.filter(pl.col("symbol").is_in(symbols))
        parts.append(_canon(lf.collect()))
    return pl.concat(parts)


def _adj_factors(repo, symbols: list[str], start: date, end: date, frames: dict | None = None) -> pl.DataFrame:
    """当日复权系数 close/raw_close。frames = 调用方已加载的日K (决策台盘后扫描已有), 有就直接用, 避免再扫全市场。"""
    if frames is not None:
        parts = [f.select([pl.lit(s).alias("symbol"), "date", "close", "raw_close"]) for s, f in frames.items()
                 if s in set(symbols) and not f.is_empty() and {"date", "close", "raw_close"} <= set(f.columns)]
        d = pl.concat(parts) if parts else pl.DataFrame()
    else:
        d = repo.get_daily_batch(symbols, start - timedelta(days=7), end, columns=["symbol", "date", "close", "raw_close"])
    if d.is_empty() or "raw_close" not in d.columns:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "adj": pl.Float64})
    return d.select(["symbol", "date", (pl.col("close") / pl.col("raw_close")).alias("adj")]).filter(
        pl.col("adj").is_finite() & (pl.col("adj") > 0))


def bars_4h(data_dir: Path, repo, symbols: list[str], start: date, end: date,
            frames: dict | None = None) -> dict[str, pd.DataFrame]:
    """返回 {symbol: 4h OHLCV (index = 美东墙钟 naive, 分箱起点)}。价格已按系统日K复权口径调整。"""
    from app.vendor.oasis_phantom.resample_4h_tv import _resample_4h_tv  # 仅完整版的 4h 插件调用本函数

    raw = load_m30(data_dir, symbols, start, end)
    if raw.is_empty():
        return {}
    raw = raw.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York").alias("ts"))
    raw = raw.with_columns(pl.col("ts").dt.date().alias("date"), pl.col("ts").dt.replace_time_zone(None).alias("wall"))
    adj = _adj_factors(repo, symbols, start, end, frames)
    # 复权系数按“最近一个已知交易日”向前填 (盘前/盘后、日K未落盘的当日 → 用前一日系数)
    # 两侧已按 date 排序; by 分组下 polars 无法自检排序, 显式声明
    raw = raw.sort("date").join_asof(adj.sort("date"), on="date", by="symbol", strategy="backward",
                                     check_sortedness=False).sort(["symbol", "ts"])
    raw = raw.with_columns(pl.col("adj").fill_null(1.0))
    raw = raw.with_columns([(pl.col(c) * pl.col("adj")).alias(c) for c in ("open", "high", "low", "close")]
                           + [(pl.col("volume") / pl.col("adj")).alias("volume")])
    out: dict[str, pd.DataFrame] = {}
    for part in raw.partition_by("symbol"):
        sym = part["symbol"][0]
        pdf = part.select(["wall", "open", "high", "low", "close", "volume"]).to_pandas().set_index("wall")
        pdf.index.name = None
        out[sym] = _resample_4h_tv(pdf)
    return out


def daily_atr_pct(repo, symbols: list[str], start: date, end: date) -> dict[str, pd.Series]:
    """日线 ATR(14)% (原 _fetch_daily_atr_map 口径), 按日期索引; 供需要日线 ATR 保护位的批跑口径。"""
    d = repo.get_daily_batch(symbols, start - timedelta(days=60), end, columns=["symbol", "date", "high", "low", "close"])
    out = {}
    for part in d.partition_by("symbol") if not d.is_empty() else []:
        p = part.sort("date").to_pandas().set_index("date")
        pc = p["close"].shift(1)
        tr = pd.concat([p["high"] - p["low"], (p["high"] - pc).abs(), (p["low"] - pc).abs()], axis=1).max(axis=1)
        out[part["symbol"][0]] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / p["close"] * 100
    return out


def refresh_recent(data_dir: Path, repo, provider, symbols: list[str], today: date, *, days_back: int = 6) -> dict:
    """盘后增量: 重拉最近 days_back 个交易日的 30 分钟线并合并进对应月份分区 (幂等)。"""
    with _lock:
        days = trading_days(repo, today - timedelta(days=days_back * 3), today)[-days_back:]
        if not days:
            return {"skipped": "no trading days"}
        fresh = fetch_days(provider, symbols, days)
        written = _merge_into_months(data_dir, fresh)
        return {"days": [str(d) for d in days], "bars": written, "symbols": len(symbols)}


def ensure_history(data_dir: Path, repo, provider, symbols: list[str], today: date, *, min_days: int = 130) -> dict:
    """新进入扫描池、本地 4h 数据不足 min_days 个交易日的股票: 补拉最近 min_days 个交易日 (幂等)。"""
    days = trading_days(repo, today - timedelta(days=min_days * 2), today)[-min_days:]
    if not days:
        return {"skipped": "no trading days"}
    have = load_m30(data_dir, symbols, days[0], days[-1])
    counts = dict(have.group_by("symbol").agg(pl.col("t").str.slice(0, 10).n_unique().alias("n")).iter_rows()) if have.height else {}
    missing = [s for s in symbols if counts.get(s, 0) < int(min_days * 0.8)]
    if not missing:
        return {"missing": 0}
    with _lock:
        fresh = fetch_days(provider, missing, days)
        written = _merge_into_months(data_dir, fresh)
    return {"missing": len(missing), "bars": written}


def trading_days(repo, start: date, end: date) -> list[date]:
    """本地日K里 SPY 有成交的日子 (us_clock 日历只缓存约一年, 历史区间用日K)。"""
    spy = repo.get_daily("SPY", start, end, columns=["date"])
    return sorted(d for d in (spy["date"].to_list() if spy is not None and not spy.is_empty() else []) if start <= d <= end)


def _merge_into_months(data_dir: Path, fresh: pl.DataFrame) -> int:
    if fresh.is_empty():
        return 0
    written = 0
    for part in fresh.with_columns(pl.col("day").str.slice(0, 7).alias("_m")).partition_by("_m"):
        month = part["_m"][0]
        new = part.drop(["_m", "day"])
        p = _month_path(data_dir, month)
        merged = pl.concat([_canon(pl.read_parquet(p)), _canon(new)]) if p.exists() else new
        write_month(data_dir, month, merged)
        written += new.height
    return written
