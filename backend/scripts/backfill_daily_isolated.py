#!/usr/bin/env python3
"""隔离目录回填: SIP 原始日K + 完整除权事件表 (不碰生产数据目录)。

用途: 批1 真实规则重放 / 批3 生产回填前的内存与耗时预演。
写入 <out>/kline_daily/date=*/part.parquet 与 <out>/adj_factor/{all.parquet,basis.json}。

每 100 只一批: raw 一遍 (日K + 事件原料) + all 一遍 (事件), 两遍即可, 不重复拉 raw。
生产与本脚本共用同一 Alpaca key, 盘中须限速, 默认 ≤50 请求/分钟。

续跑按 scope 绑定: 标的清单哈希 + 起止日 + 口径版本 + 分块映射写入 manifest,
只有 scope 完全一致才复用已完成分块; 不一致则清空重来, 绝不把别的任务的缓存发布为本次结果。
end 默认取最近一个已收盘交易日, 盘中未定版的日K不进入历史。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import polars as pl

import app.plugins.alpaca.provider as alp
from app.market_clock import us_clock
from app.services.adjustment_events import events_for_symbols
from app.services.kline_sync import ADJ_EVENT_BASIS, store_adj_factor_events

logger = logging.getLogger("backfill")
SCRIPT_VERSION = "backfill-v2-scope-manifest"


class RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


def last_closed_session(now: datetime | None = None) -> date:
    """最近一个已完整收盘的美东交易日 (当日未收盘则取前一交易日)。"""
    now = now or us_clock.now()
    day = now.date()
    if us_clock.is_trading_day(day) and now.time() >= us_clock.sessions[-1].close:
        return day
    days = us_clock.trading_days_between(day - timedelta(days=14), day - timedelta(days=1))
    return days[-1] if days else day - timedelta(days=1)


def build_scope(symbols: list[str], start: date, end: date, batch: int) -> dict:
    chunks = [symbols[i:i + batch] for i in range(0, len(symbols), batch)]
    return {
        "version": SCRIPT_VERSION, "adj_basis": ADJ_EVENT_BASIS, "feed": "sip",
        "start": start.isoformat(), "end": end.isoformat(), "symbols": len(symbols),
        "symbols_sha256": hashlib.sha256("\n".join(symbols).encode()).hexdigest(),
        "chunks": [hashlib.sha256("\n".join(c).encode()).hexdigest()[:16] for c in chunks],
    }


def prepare_staging(out: Path, scope: dict) -> Path:
    """scope 一致则复用; 否则整个 staging 作废重建。"""
    staging = out / ".staging_daily"
    manifest = staging / "scope.json"
    try:
        same = json.loads(manifest.read_text()) == scope
    except (OSError, ValueError):
        same = False
    if not same:
        if staging.exists():
            logger.warning("staging scope 不一致, 作废旧缓存重新拉取")
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        manifest.write_text(json.dumps(scope, indent=2))
    return staging


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instruments", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=None, help="默认: 最近一个已收盘交易日")
    ap.add_argument("--types", default="common_stock,adr,unknown")
    ap.add_argument("--extra", default="SPY,QQQ,DIA,IWM")
    ap.add_argument("--rpm", type=int, default=50)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(args.out)
    inst = pl.read_parquet(args.instruments)
    symbols = sorted(set(inst.filter(pl.col("instrument_type").is_in(args.types.split(",")))["symbol"].to_list())
                     | set(filter(None, args.extra.split(","))))
    start_day = date.fromisoformat(args.start)
    end_day = date.fromisoformat(args.end) if args.end else last_closed_session()
    start = datetime.combine(start_day, datetime.min.time())
    end = datetime.combine(end_day, datetime.max.time().replace(microsecond=0))
    provider = alp.AlpacaProvider()
    limiter = RateLimiter(args.rpm)
    original_get = alp._get
    requests = [0]

    def limited_get(*a, **kw):
        limiter.wait()
        requests[0] += 1
        return original_get(*a, **kw)

    alp._get = limited_get
    scope = build_scope(symbols, start_day, end_day, alp._BATCH)
    staging = prepare_staging(out, scope)
    chunks = [symbols[i:i + alp._BATCH] for i in range(0, len(symbols), alp._BATCH)]
    t0 = time.time()
    lock = threading.Lock()
    done = [0]

    def work(ci: int, chunk: list[str]) -> None:
        target = staging / f"chunk-{ci:04d}.parquet"
        events_path = staging / f"events-{ci:04d}.json"
        if target.exists() and events_path.exists():  # 同 scope 断点续跑
            return
        raw = list(alp._iter_bars(provider.key, provider.secret, chunk, "1Day", start, end, "raw", "sip"))
        adjusted = list(alp._iter_bars(provider.key, provider.secret, chunk, "1Day", start, end, "all", "sip"))
        frame = provider._daily_rows_to_frame([provider._daily_row(s, b) for s, b in raw])
        events = events_for_symbols(raw, adjusted)
        # 先写日K再写事件: 事件文件存在才算分块完成, 中断的半成品会被重新拉取
        (frame if not frame.is_empty() else pl.DataFrame()).write_parquet(target)
        tmp = events_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([{**e, "trade_date": e["trade_date"].isoformat()} for e in events]))
        tmp.replace(events_path)
        with lock:
            done[0] += 1
            logger.info("chunk %d/%d rows=%d events=%d requests=%d elapsed=%.0fs", done[0], len(chunks),
                        frame.height, len(events), requests[0], time.time() - t0)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda x: work(*x), enumerate(chunks)))

    # 只发布本 scope 的分块 (按序号精确取, 不 glob 残留文件)
    frames, events_all = [], []
    for ci in range(len(chunks)):
        path = staging / f"chunk-{ci:04d}.parquet"
        if pl.read_parquet_schema(path):
            frames.append(pl.read_parquet(path))
        events_all.extend(json.loads((staging / f"events-{ci:04d}.json").read_text()))
    daily = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
    daily_root = out / "kline_daily"
    if daily_root.exists():
        shutil.rmtree(daily_root)
    dates = []
    for part in (daily.partition_by("date") if not daily.is_empty() else []):
        d = part["date"][0]
        dates.append(d)
        p = daily_root / f"date={d.isoformat()}" / "part.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        part.sort("symbol").write_parquet(p)
    dates.sort()
    events = pl.DataFrame(events_all, schema={"symbol": pl.Utf8, "trade_date": pl.Utf8,
                                              "ex_factor": pl.Float64, "vol_factor": pl.Float64}
                          ).with_columns(pl.col("trade_date").str.to_date())
    changed, affected = store_adj_factor_events(out / "adj_factor", events, replace=True)
    summary = {"scope": {k: scope[k] for k in ("version", "start", "end", "symbols", "symbols_sha256")},
               "daily_rows": daily.height, "dates": len(dates),
               "first": dates[0].isoformat() if dates else None, "last": dates[-1].isoformat() if dates else None,
               "adj_events": events.height, "adj_symbols": len(affected),
               "splits": events.filter((pl.col("vol_factor") - 1).abs() > 0.01).height,
               "requests": requests[0], "elapsed_s": round(time.time() - t0, 1)}
    (out / "backfill-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
