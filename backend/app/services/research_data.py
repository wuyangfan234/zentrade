"""证据 v3 研究数据集 (生产内, 与交易数据隔离): data/research/v3/ 下的独立数据目录。

为什么要单独一份: v3 需要把已退市股票补进历史 (否则回放只看到“活下来的”股票, 结果偏乐观),
但退市代码 (代码~退市日) 绝不能进入交易用的 kline_daily / 股票池 / 行情链路。
所以评估读这份合并副本, 交易链路继续只读生产 data/。

- 退市名单: Massive(Polygon) reference (active=false, 普通股/ADR, 主板, 2021 年起退市), 剔除改名延续 (同 FIGI/CIK 仍在市)
- 退市日K: Alpaca SIP 日K, raw + adjustment=all 双拉 (与生产复权机制同口径)
- 合并: 每周评估前重建一次; 生产 kline_daily_enriched 硬链接/复制 + 退市行按日追加
- 事件数据: Nasdaq 财报日历历史 (每日 JSON) + SEC Form 345 季度数据集, 增量下载到 data/research/events/
全部失败只降级 (评估改用生产数据并在结果里标注“未含退市股”), 不影响交易。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.config import PUBLIC_HTTP_USER_AGENT
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

logger = logging.getLogger(__name__)

MASSIVE = "https://api.massive.com"

WEB_UA = PUBLIC_HTTP_USER_AGENT
DELIST_SINCE = "2021-01-01"
MAIN_EXCHANGES = ("XNYS", "XNAS", "XASE", "ARCX", "BATS")


def research_root(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "evidence_v3"


def v3_data_dir(data_dir: Path) -> Path:
    return research_root(data_dir) / "v3"


def events_dir(data_dir: Path) -> Path:
    return research_root(data_dir) / "events"


def _jget(url: str, headers: dict | None = None, tries: int = 6, binary: bool = False):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:  # noqa: S310
                data = r.read()
                return data if binary else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(15 * (i + 1) if e.code == 429 else 5 * (i + 1))
        except Exception:  # noqa: BLE001
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"fetch failed: {url.split('?')[0]}")


def _sec_ua() -> str:
    from app import credentials
    return credentials.sec_user_agent()


def _secret(name: str) -> str | None:
    """统一规则 (app/credentials.py)。永不打印/落盘。"""
    from app import credentials
    if name in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        k, s, _src = credentials.alpaca()
        return (k if name == "ALPACA_API_KEY_ID" else s) or None
    spec = {"POLYGON_API_KEY": credentials.POLYGON, "MASSIVE_API_KEY": credentials.MASSIVE}.get(name)
    return (credentials.get(spec) or None) if spec else None


# ── 退市股 ────────────────────────────────────────────────────

def refresh_delisted(data_dir: Path, *, max_age_days: int = 7) -> dict:
    """更新退市名单与日K (增量: 已拉过的 (代码, 退市日) 不再拉)。返回统计。"""
    root = research_root(data_dir) / "delisted"
    root.mkdir(parents=True, exist_ok=True)
    poly = _secret("POLYGON_API_KEY") or _secret("MASSIVE_API_KEY")
    alp_id, alp_secret = _secret("ALPACA_API_KEY_ID"), _secret("ALPACA_API_SECRET_KEY")
    if not poly or not alp_id or not alp_secret:
        return {"status": "skipped", "reason": "缺少 Massive/Alpaca 密钥"}
    lst = root / "list.json"
    if not lst.exists() or (time.time() - lst.stat().st_mtime) > max_age_days * 86400:
        rows = _reference(poly, active=False)
        keep = [r for r in rows if r.get("delisted_utc") and r["delisted_utc"][:10] >= DELIST_SINCE
                and r.get("primary_exchange") in MAIN_EXCHANGES]
        active = _reference(poly, active=True)
        afigi = {a.get("composite_figi") for a in active if a.get("composite_figi")}
        acik = {a.get("cik") for a in active if a.get("cik")}
        keep = [r for r in keep if r.get("composite_figi") not in afigi and r.get("cik") not in acik]
        atomic_write_text(lst, json.dumps(keep))  # 原子写: 中途被杀不留半截名单 (BH-04)
    keep = json.loads(lst.read_text())
    bars_path = root / "bars.parquet"
    have = set()
    old = None
    if bars_path.exists():
        old = pl.read_parquet(bars_path)
        have = set(old["rsym"].unique().to_list())
    jobs, prev = [], {}
    for r in sorted(keep, key=lambda r: (r["ticker"], r["delisted_utc"])):
        t, d = r["ticker"], r["delisted_utc"][:10]
        rsym = f"{t}~{d.replace('-', '')}"
        start = prev.get(t, "2020-06-01")
        prev[t] = d
        if rsym not in have:
            jobs.append((t, max(start, "2020-06-01"), d, rsym, r.get("type"), r.get("name")))
    headers = {"APCA-API-KEY-ID": alp_id, "APCA-API-SECRET-KEY": alp_secret}
    new_rows = []
    for k in range(0, len(jobs), 40):
        batch = jobs[k:k + 40]
        seen_t = set()
        uniq = [j for j in batch if not (j[0] in seen_t or seen_t.add(j[0]))]
        for adj in ("raw", "all"):
            got = _alpaca_daily(headers, [j[0] for j in uniq], min(j[1] for j in uniq), max(j[2] for j in uniq), adj)
            win = {j[0]: j for j in uniq}
            for g in got:
                j = win.get(g["symbol"])
                if j is None or not (j[1] < g["date"] <= j[2]):
                    continue
                g.update({"rsym": j[3], "adj": adj})
                new_rows.append(g)
        time.sleep(0.3)
    if new_rows:
        df = pl.DataFrame(new_rows).with_columns(pl.col("date").str.to_date())
        wide = (df.filter(pl.col("adj") == "raw").drop("adj")
                .join(df.filter(pl.col("adj") == "all").select(["rsym", "date", pl.col("close").alias("close_adj")]),
                      on=["rsym", "date"], how="left"))
        merged = pl.concat([old, wide], how="vertical_relaxed") if old is not None else wide
        atomic_write_parquet(merged.unique(subset=["rsym", "date"], keep="last").sort(["rsym", "date"]), bars_path)
    names = {f"{r['ticker']}~{r['delisted_utc'][:10].replace('-', '')}": (r.get("name"), r.get("type")) for r in keep}
    atomic_write_text(root / "names.json", json.dumps(names))
    return {"status": "ok", "listed": len(keep), "new_symbols": len(jobs)}


def _reference(key: str, *, active: bool) -> list[dict]:
    out = []
    for typ in ("CS", "ADRC"):
        url = f"{MASSIVE}/v3/reference/tickers?" + urllib.parse.urlencode(
            {"market": "stocks", "active": str(active).lower(), "type": typ, "limit": 1000, "apiKey": key})
        while url:
            d = _jget(url) or {}
            out += d.get("results", [])
            nxt = d.get("next_url")
            url = (nxt + "&apiKey=" + key) if nxt else None
            time.sleep(13)  # 免费档 5 次/分钟
    return out


def _alpaca_daily(headers: dict, symbols: list[str], start: str, end: str, adj: str) -> list[dict]:
    out, tok = [], None
    while True:
        q = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start + "T00:00:00Z", "end": end + "T23:59:59Z",
             "feed": "sip", "adjustment": adj, "limit": 10000}
        if tok:
            q["page_token"] = tok
        d = _jget("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(q), headers) or {}
        for s, bs in (d.get("bars") or {}).items():
            for b in bs:
                out.append({"symbol": s, "date": b["t"][:10], "open": float(b["o"]), "high": float(b["h"]),
                            "low": float(b["l"]), "close": float(b["c"]), "volume": float(b["v"])})
        tok = d.get("next_page_token")
        if not tok:
            return out
        time.sleep(0.3)


def build_v3_dataset(data_dir: Path) -> dict:
    """生产 enriched 日K + 退市股 → data/research/v3 (重建; 原子替换目录)。只含评估需要的列。"""
    data_dir = Path(data_dir)
    bars_path = research_root(data_dir) / "delisted" / "bars.parquet"
    if not bars_path.exists():
        return {"status": "skipped", "reason": "还没有退市股数据"}
    dl = pl.read_parquet(bars_path)
    names = json.loads((research_root(data_dir) / "delisted" / "names.json").read_text())
    src = data_dir / "kline_daily_enriched"
    existing = pl.scan_parquet(str(src / "date=*" / "part.parquet"), hive_partitioning=True,
                               extra_columns="ignore", missing_columns="insert").select(["symbol", "date"]).collect()
    dl = (dl.join(existing.with_columns(pl.lit(True).alias("_dup")), on=["symbol", "date"], how="left")
            .filter(pl.col("_dup").is_null()).drop("_dup"))
    f = (pl.col("close_adj") / pl.col("close")).fill_null(1.0)
    enr = dl.select([pl.col("rsym").alias("symbol"), "date", (pl.col("open") * f).alias("open"),
                     (pl.col("high") * f).alias("high"), (pl.col("low") * f).alias("low"),
                     (pl.col("close") * f).alias("close"), (pl.col("volume") / f).alias("volume"),
                     (pl.col("close") * pl.col("volume")).alias("amount"), pl.col("close").alias("raw_close"),
                     pl.col("high").alias("raw_high"), pl.col("low").alias("raw_low")])
    by_day = {d: g for (d,), g in enr.partition_by("date", as_dict=True).items()}
    tmp = research_root(data_dir) / "v3.building"
    if tmp.exists():
        shutil.rmtree(tmp)
    out_dir = tmp / "kline_daily_enriched"
    n_parts = 0
    for p in sorted(src.glob("date=*"), key=lambda x: x.name):
        base = pl.read_parquet(p / "part.parquet")
        add = by_day.pop(date.fromisoformat(p.name.removeprefix("date=")), None)
        (out_dir / p.name).mkdir(parents=True, exist_ok=True)
        if add is not None:
            add = add.with_columns([pl.lit(None, dtype=base.schema[c]).alias(c) for c in base.columns if c not in add.columns])
            base = pl.concat([base, add.select(base.columns).cast(base.schema)], how="vertical")
            base.write_parquet(out_dir / p.name / "part.parquet")
        else:
            try:
                os.link(p / "part.parquet", out_dir / p.name / "part.parquet")
            except OSError:
                shutil.copy2(p / "part.parquet", out_dir / p.name / "part.parquet")
        n_parts += 1
    inst = pl.read_parquet(data_dir / "instruments" / "instruments.parquet")
    rows = [{"symbol": s, "name": f"{(names.get(s) or [s])[0] or s} (已退市)",
             "instrument_type": "adr" if (names.get(s) or [None, None])[1] == "ADRC" else "common_stock",
             "is_etf": False, "asset_type": "stock", "reference_status": "delisted"} for s in enr["symbol"].unique().to_list()]
    if rows:
        add = pl.DataFrame(rows)
        add = add.with_columns([pl.lit(None, dtype=inst.schema[c]).alias(c) for c in inst.columns if c not in add.columns])
        inst = pl.concat([inst, add.select(inst.columns).cast(inst.schema, strict=False)], how="vertical")
    (tmp / "instruments").mkdir(parents=True, exist_ok=True)
    inst.write_parquet(tmp / "instruments" / "instruments.parquet")
    for d in ("adj_factor", "kline_index_daily", "kline_index_enriched", "user_data", "strategies"):
        if (data_dir / d).exists():
            (tmp / d).symlink_to((data_dir / d).resolve())
    final = v3_data_dir(data_dir)
    old = research_root(data_dir) / "v3.old"
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    tmp.rename(final)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    return {"status": "ok", "partitions": n_parts, "delisted_symbols": len(rows)}


# ── 事件数据 ──────────────────────────────────────────────────

def refresh_events(data_dir: Path, *, since: date = date(2021, 12, 1), today: date | None = None) -> dict:
    """增量下载 Nasdaq 财报日历 (按日) 与 SEC Form 345 季度数据。已下载的不重下 (最近 10 天的财报日历重拉, 补 EPS 实际值)。"""
    today = today or date.today()
    ed = events_dir(data_dir) / "earnings"
    ed.mkdir(parents=True, exist_ok=True)
    n_e = 0
    d = since
    while d <= today:
        p = ed / f"{d}.json"
        recent = (today - d).days <= 10
        if d.weekday() < 5 and (not p.exists() or recent):
            js = _jget(f"https://api.nasdaq.com/api/calendar/earnings?date={d}",
                       {"User-Agent": WEB_UA, "Accept": "application/json, text/plain, */*"})
            rows = ((js or {}).get("data") or {}).get("rows") if js else None
            p.write_text(json.dumps({"date": str(d), "rows": rows or [], "ok": js is not None}))
            n_e += 1
            time.sleep(1.0)
        d += timedelta(days=1)
    sd = events_dir(data_dir) / "sec"
    sd.mkdir(parents=True, exist_ok=True)
    n_s = 0
    for y in range(2021, today.year + 1):
        for q in range(1, 5):
            if date(y, 3 * q, 1) > today:
                continue
            p = sd / f"{y}q{q}_form345.zip"
            if p.exists() and p.stat().st_size > 1000:
                continue
            data = _jget(f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip",
                         {"User-Agent": _sec_ua()}, binary=True)
            if data:
                p.write_bytes(data)
                n_s += 1
            time.sleep(1.0)
    return {"status": "ok", "earnings_days_fetched": n_e, "sec_quarters_fetched": n_s}
