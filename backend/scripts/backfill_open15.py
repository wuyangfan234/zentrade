"""09:45 那根 15 分钟K 的历史回补 (2026-11-10; 机会板卡回放的入场起点, 见 kline_4h.fetch_open15)。

盘后任务只滚动维护最近 130 个交易日 (kline_4h.refresh_open15); 规则成绩 / 赔率表 / 排序因子候选池的回放窗口从
2023-01 起, 更早的历史用这个脚本一次补齐。只写 <data_dir>/kline_4h_src/m15_open/, 幂等 (重跑只覆盖同一根)。

逐个交易日取 09:45-09:59 这一根 (kline_4h.fetch_open15, 与盘后任务同一个函数; 一天一个请求, 约 1000 个请求,
限流由行情客户端自带的退避处理), 行情密钥按 app/credentials.py 的统一规则取。交易日 = 本地日K里 SPY 有成交的日子。

用法: python backfill_open15.py <data_dir> [--start 2023-01-03] [--end YYYY-MM-DD] [--symbols A,B,C]
  默认标的 = <data_dir>/user_data/watchlist.parquet 里的自选 + 最近 60 天下单簿里出现过的票。
"""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

DATA = Path(sys.argv[1]).resolve()
os.environ.setdefault("DATA_DIR", str(DATA))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl  # noqa: E402

from app.plugins.alpaca import provider as ap  # noqa: E402
from app.services import kline_4h  # noqa: E402
from app.storage.repository import DataStore, KlineRepository  # noqa: E402


def arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


START = date.fromisoformat(arg("--start", "2023-01-03"))
END = date.fromisoformat(arg("--end", date.today().isoformat()))
if arg("--symbols"):
    SYMS = sorted({s.strip().upper() for s in arg("--symbols").split(",") if s.strip()})
else:
    SYMS = set(pl.read_parquet(DATA / "user_data" / "watchlist.parquet")["symbol"].drop_nulls().unique().to_list())
    for f in sorted((DATA / "opportunities" / "book").glob("20*.json"))[-60:]:
        try:
            SYMS |= {c["symbol"] for c in json.loads(f.read_text(encoding="utf-8")).get("cards") or [] if c.get("symbol")}
        except (OSError, ValueError):
            continue
    SYMS = sorted(SYMS)
prov = ap.AlpacaProvider()
if not prov.key or not prov.secret:
    raise SystemExit("no market-data credentials (see app/credentials.py)")
DAYS = kline_4h.trading_days(KlineRepository(DataStore(DATA)), START, END)
print(f"symbols={len(SYMS)} window={START}..{END} trading_days={len(DAYS)}", flush=True)

t0 = time.time()
total = 0
CHUNK = 40
for i in range(0, len(DAYS), CHUNK):
    part = DAYS[i:i + CHUNK]
    for attempt in range(6):                       # 连接被重置之类的瞬时错误: 这一段重来 (整段幂等)
        try:
            fresh = kline_4h.fetch_open15(prov, SYMS, part, threads=2)
            break
        except OSError as e:
            if attempt == 5:
                raise
            print(f"{part[0]}..{part[-1]} retry {attempt + 1}: {type(e).__name__}", flush=True)
            time.sleep(5 * (attempt + 1))
    total += kline_4h._merge_open15(DATA, fresh)
    print(f"{part[0]}..{part[-1]} bars={total} {time.time() - t0:.0f}s", flush=True)
print(f"done bars={total} in {time.time() - t0:.0f}s -> {kline_4h.open15_dir(DATA)}", flush=True)
