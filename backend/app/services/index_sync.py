"""核心指数 ETF (SPY/QQQ/DIA/IWM) 官方日K → 指数表 (kline_index_daily / kline_index_enriched)。

背景 (2026-10-09 核实): 指数独立同步在 P1-5 删除后, 指数表唯一写入路径是盘中实时行情,
收盘前最后一帧 (15:59:5x) 永久留在分区里 → 完整性检查每天判坏, 总览/分时读到盘中值
(10/6 DIA 515.07 / 量 89 万)。股票表里这四只每晚随股票日K按 SIP 官方批次拉取 (quote_ts 为空),
这里把官方行复制进指数表, merge-upsert 覆盖盘中快照。

只复制 quote_ts 为空的官方行; 当日常规时段未收盘时不复制。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime

import polars as pl

from app.market_clock import us_clock
from app.services.index_const import CORE_INDEX_SYMBOLS

logger = logging.getLogger(__name__)

_DAILY_COLS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")


def _closed(day: date, now: datetime) -> bool:
    return day < now.date() or (day == now.date() and now.time() >= us_clock.sessions[-1].close)


def sync_core_index_from_stock(repo, days: Iterable[date], now: datetime | None = None) -> list[str]:
    """把指定日期股票表中核心指数 ETF 的官方行写入指数表; 返回实际写入的日期。"""
    now = us_clock.market_datetime(now or us_clock.now())
    base = repo.store.data_dir / "kline_daily"
    done: list[str] = []
    for d in sorted(set(days)):
        if not _closed(d, now):
            continue
        parts = sorted((base / f"date={d.isoformat()}").glob("*.parquet"))
        if not parts:
            continue
        df = pl.concat([pl.read_parquet(p) for p in parts], how="diagonal_relaxed")
        df = df.filter(pl.col("symbol").is_in(list(CORE_INDEX_SYMBOLS)))
        if "quote_ts" in df.columns:
            df = df.filter(pl.col("quote_ts").is_null())
        else:
            df = df.with_columns(pl.lit(None, pl.Int64).alias("quote_ts"))
        if "date" not in df.columns:
            df = df.with_columns(pl.lit(d).alias("date"))
        if df.is_empty() or any(c not in df.columns for c in _DAILY_COLS):
            continue
        df = df.unique(["symbol", "date"], keep="last")
        repo.append_index_daily(df)
        repo.append_index_enriched(df.select(
            *_DAILY_COLS,
            pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"), pl.col("quote_ts").cast(pl.Int64),
        ))
        done.append(d.isoformat())
    if done:
        logger.info("index_sync: 核心指数官方日K写入指数表 %s", ",".join(done))
    return done
