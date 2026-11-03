"""0.47.2 性能: 日K按日期分区裁剪、新闻分片按文件签名缓存。结果必须与旧实现逐字节一致。"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.storage import repository as repo_mod
from app.storage.repository import DataStore, KlineRepository, _partition_source


def _write_partitions(base: Path, days: list[date], symbols=("AAA", "BBB", "CCC")) -> None:
    for i, d in enumerate(days):
        part = base / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": list(symbols),
            "date": [d] * len(symbols),
            "open": [10.0 + i] * len(symbols), "high": [11.0 + i] * len(symbols), "low": [9.0 + i] * len(symbols),
            "close": [10.5 + i + k for k in range(len(symbols))], "volume": [1e6] * len(symbols), "amount": [1e7] * len(symbols),
            "raw_close": [10.5 + i] * len(symbols), "raw_high": [11.0] * len(symbols), "raw_low": [9.0] * len(symbols),
            "turnover_rate": [None] * len(symbols), "quote_ts": [0] * len(symbols),
        }).write_parquet(part / "part.parquet")


@pytest.fixture
def repo(tmp_path):
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(60)]
    _write_partitions(tmp_path / "kline_daily_enriched", days)
    _write_partitions(tmp_path / "kline_index_enriched", days, symbols=("SPY", "QQQ"))
    (tmp_path / "kline_daily_enriched" / f"date={days[-1].isoformat()}" / "part.parquet.tmp").write_bytes(b"junk")
    r = KlineRepository(DataStore(tmp_path))
    r._enriched_warming = True
    return r


def _old(repo, method, *args):
    """Same query with the pre-0.47.2 whole-tree glob."""
    orig = repo_mod._partition_source
    repo_mod._partition_source = lambda pattern, start, end: pattern
    try:
        return getattr(repo, method)(*args)
    finally:
        repo_mod._partition_source = orig


@pytest.mark.parametrize("method,args", [
    ("_scan_daily_batch", (["AAA", "CCC"], date(2026, 9, 10), date(2026, 9, 20), ["symbol", "date", "close"])),
    ("_scan_daily_batch", (["AAA", "BBB", "CCC"], date(2025, 1, 1), date(2027, 1, 1), None)),
    ("_scan_daily_batch", (["ZZZ"], date(2026, 9, 10), date(2026, 9, 12), ["symbol", "date"])),
    ("_scan_daily_batch", (["AAA"], date(2026, 9, 5), date(2026, 9, 5), ["symbol", "date", "close", "no_such_col"])),
    ("_scan_daily_symbol", ("BBB", date(2026, 9, 25), date(2026, 10, 3), None)),
    ("_scan_daily_symbol", ("BBB", date(2027, 1, 1), date(2027, 2, 1), ["symbol", "date"])),
    ("_scan_index_daily_symbol", ("SPY", date(2026, 9, 1), date(2026, 9, 9), ["symbol", "date", "close"])),
])
def test_partition_pruning_returns_identical_frames(repo, method, args):
    new = getattr(repo, method)(*args)
    old = _old(repo, method, *args)
    assert new.schema == old.schema
    assert new.equals(old)


def test_partition_source_picks_only_the_requested_dates(repo):
    files = _partition_source(repo._enriched_glob, date(2026, 9, 10), date(2026, 9, 12))
    assert isinstance(files, list) and len(files) == 3
    assert all(f.endswith("part.parquet") for f in files)


def test_partition_source_falls_back_to_the_glob_when_layout_is_not_standard(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    _write_partitions(base, [date(2026, 9, 1)])
    pattern = str(base / "**" / "*.parquet")
    (base / "legacy").mkdir()
    assert _partition_source(pattern, date(2026, 9, 1), date(2026, 9, 1)) == pattern
    (base / "legacy").rmdir()
    pl.DataFrame({"symbol": ["A"]}).write_parquet(base / "loose.parquet")
    assert _partition_source(pattern, date(2026, 9, 1), date(2026, 9, 1)) == pattern


def test_partition_source_falls_back_without_dates_or_matches(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    _write_partitions(base, [date(2026, 9, 1)])
    pattern = str(base / "**" / "*.parquet")
    assert _partition_source(pattern, None, date(2026, 9, 1)) == pattern
    assert _partition_source(pattern, date(2030, 1, 1), date(2030, 2, 1)) == pattern
    assert _partition_source(str(tmp_path / "missing" / "**" / "*.parquet"), date(2026, 9, 1), date(2026, 9, 2)).endswith("*.parquet")
    assert _partition_source("/x/y.parquet", date(2026, 9, 1), date(2026, 9, 2)) == "/x/y.parquet"


# ---------------------------------------------------------------- news shard cache
from app.services import news_ingest, news_store  # noqa: E402


def _old_load_recent(data_dir, *, days, now):
    since = now - timedelta(days=days)
    rows = {}
    for when in news_store._months(now, days):
        for rid, row in news_ingest.load_shard(data_dir, when).items():
            dt = news_store._parse(row.get("created_at"))
            if dt and since <= dt <= now + timedelta(minutes=5):
                rows[rid] = row
    return sorted(rows.values(), key=lambda r: r.get("created_at") or "", reverse=True)


def _old_batch_summary(data_dir, symbols, *, days=3, now):
    rows = _old_load_recent(data_dir, days=days, now=now)
    return {s.upper(): news_store.summarize(news_store.for_symbol(data_dir, s, rows=rows, now=now, limit=200)) for s in symbols if s}


def _news_rows(now):
    rows = []
    for i in range(240):
        at = now - timedelta(hours=i * 0.75)
        sym = ["NVDA", "AAPL", "TSLA", "MU"][i % 4]
        other = ["AMD"] if i % 5 == 0 else []
        rows.append({
            "id": f"n{i:04d}", "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": f"h{i}",
            "zh_headline": f"标题{i}" if i % 3 else None, "symbols": [sym, *other],
            "mentions": {sym: "headline" if i % 2 else "summary", **{o: "summary" for o in other}},
            "tags": {sym: {"relevance": "primary", "importance": ["high", "medium", "low"][i % 3],
                           "direction": ["bullish", "bearish", "neutral"][i % 3], "event_type": "earnings"}} if i % 4 else {},
            "tagged_by": {"mode": "llm"} if i % 4 else {}, "sources": ["alpaca"], "refs": [{"url": f"https://x/{i}"}],
        })
    rows.append({"id": "n0001", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": "dup newer",
                 "symbols": ["NVDA"], "mentions": {"NVDA": "headline"}, "sources": ["finnhub"]})
    rows.append({"id": "nodate", "created_at": None, "headline": "x", "symbols": ["NVDA"], "mentions": {"NVDA": "headline"}})
    rows.append({"id": "future", "created_at": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": "f",
                 "symbols": ["NVDA"], "mentions": {"NVDA": "headline"}})
    return rows


def _write_shards(data_dir: Path, rows):
    by_month = {}
    for r in rows:
        at = news_store._parse(r.get("created_at")) or datetime(2026, 11, 2, tzinfo=UTC)
        by_month.setdefault(at.strftime("%Y-%m"), []).append(r)
    for month, rs in by_month.items():
        p = news_ingest.shard_path(data_dir, datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC))
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n", encoding="utf-8")


@pytest.fixture
def news_dir(tmp_path):
    news_store._SHARD_INDEX.clear()
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)   # 窗口跨 10 / 11 两个月分片
    _write_shards(tmp_path, _news_rows(now))
    yield tmp_path, now
    news_store._SHARD_INDEX.clear()


@pytest.mark.parametrize("days", [1, 2, 3, 5, 7, 30])
def test_load_recent_matches_the_uncached_reader(news_dir, days):
    d, now = news_dir
    assert news_store.load_recent(d, days=days, now=now) == _old_load_recent(d, days=days, now=now)
    assert news_store.load_recent(d, days=days, now=now) == _old_load_recent(d, days=days, now=now)   # cached path


def test_batch_summary_matches_the_uncached_reader(news_dir):
    d, now = news_dir
    syms = ["nvda", "AAPL", "TSLA", "MU", "AMD", "NONE", "", "NVDA"]
    assert news_store.batch_summary(d, syms, now=now) == _old_batch_summary(d, syms, now=now)


def test_returned_rows_are_independent_copies(news_dir):
    d, now = news_dir
    a = news_store.load_recent(d, days=3, now=now)
    a[0]["headline"] = "mutated"
    a[0].setdefault("tags", {})["X"] = 1
    b = news_store.load_recent(d, days=3, now=now)
    assert b[0]["headline"] != "mutated" and "X" not in (b[0].get("tags") or {})


def test_rewritten_shard_is_reread_even_with_same_size_and_mtime(news_dir):
    d, now = news_dir
    p = news_ingest.shard_path(d, now)
    before = news_store.load_recent(d, days=1, now=now)
    st = os.stat(p)
    text = p.read_text(encoding="utf-8").replace('"h2"', '"H2"', 1)   # same length
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, p)                                                  # atomic replace = new inode
    after = news_store.load_recent(d, days=1, now=now)
    assert after == _old_load_recent(d, days=1, now=now)
    assert [r.get("headline") for r in after] != [r.get("headline") for r in before]


def test_appended_shard_is_reread(news_dir):
    d, now = news_dir
    p = news_ingest.shard_path(d, now)
    news_store.load_recent(d, days=1, now=now)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "late", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": "late",
                             "symbols": ["NVDA"], "mentions": {"NVDA": "headline"}}) + "\n")
    assert any(r["id"] == "late" for r in news_store.load_recent(d, days=1, now=now))


def test_missing_shard_is_empty(tmp_path):
    news_store._SHARD_INDEX.clear()
    assert news_store.load_recent(tmp_path, days=3, now=datetime(2026, 11, 2, tzinfo=UTC)) == []
