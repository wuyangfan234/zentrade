"""自选数据新鲜度 (0.22.5): 逐只检查日K / 30m / 4h 最新 bar, 落后 >1 个交易日告警。只读。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

MAX_LAG = 1          # 落后超过 1 个交易日 → 告警
NEW_LISTING_DAYS = 130   # 与扫描池 min_history_days 一致: 不足则不出卡 (只标注)


def _lag(days: list[date], last: date | None) -> int | None:
    if last is None:
        return None
    return sum(1 for d in days if d > last)


def check(data_dir: Path, repo, symbols: list[str], *, today: date) -> dict:
    from app.services import kline_4h
    syms = sorted({s.upper() for s in symbols if s})
    days = kline_4h.trading_days(repo, today - timedelta(days=20), today)
    if not syms or not days:
        return {"symbols": len(syms), "expected": None, "stale": [], "new_listing": []}
    expected = days[-1]
    d = repo.get_daily_batch(syms, today - timedelta(days=400), today, columns=["symbol", "date"])
    dmax: dict[str, date] = {}
    dlen: dict[str, int] = {}
    if d is not None and not d.is_empty():
        for s, mx, n in d.group_by("symbol").agg(pl.col("date").max().alias("mx"), pl.len().alias("n")).iter_rows():
            dmax[s], dlen[s] = mx, n
    m = kline_4h.load_m30(data_dir, syms, today - timedelta(days=40), today)
    mmax: dict[str, date] = {}
    if not m.is_empty():
        mm = m.with_columns(pl.col("t").str.to_datetime(time_zone="UTC").dt.convert_time_zone("America/New_York")
                            .dt.date().alias("d")).group_by("symbol").agg(pl.col("d").max().alias("mx"))
        mmax = dict(mm.iter_rows())
    stale, new_listing = [], []
    for s in syms:
        ld, lm = _lag(days, dmax.get(s)), _lag(days, mmax.get(s))
        # 4h 由 30m 合成: 最新 4h 根与 30m 同日
        row = {"symbol": s, "daily": dmax.get(s).isoformat() if dmax.get(s) else None,
               "m30": mmax.get(s).isoformat() if mmax.get(s) else None,
               "h4": mmax.get(s).isoformat() if mmax.get(s) else None,
               "lag_daily": ld, "lag_m30": lm}
        if ld is None or lm is None or ld > MAX_LAG or lm > MAX_LAG:
            stale.append(row)
        if dlen.get(s, 0) < NEW_LISTING_DAYS:
            new_listing.append({"symbol": s, "daily_bars": dlen.get(s, 0)})
    return {"symbols": len(syms), "expected": expected.isoformat(), "stale": stale, "new_listing": new_listing}


def summary(r: dict) -> tuple[str, str]:
    n, st, nl = r.get("symbols", 0), r.get("stale") or [], r.get("new_listing") or []
    if not n:
        return "idle", "自选为空"
    note = ("；上市不足 130 日不出卡：" + "、".join(f"{x['symbol']}({x['daily_bars']}日)" for x in nl)) if nl else ""
    if not st:
        return "ok", f"自选 {n} 只日K/30m/4h 均到 {r.get('expected')}" + note
    lst = "、".join(f"{x['symbol']}(日K {x['daily'] or '无'} · 30m/4h {x['m30'] or '无'})" for x in st[:12])
    more = f" 等 {len(st)} 只" if len(st) > 12 else ""
    return "warn", f"{len(st)}/{n} 只落后 >1 个交易日（应到 {r.get('expected')}）：{lst}{more}" + note
