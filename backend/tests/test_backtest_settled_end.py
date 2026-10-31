"""0.43.2 回归: 回测区间不能把盘中实时快照当成日K。

盘中回测页结束日默认 = 今天, 而今天的日K分区是行情服务每 ~10 秒整份重写的盘中快照 (quote_ts 非空,
收盘价是那一刻的价格)。每次重写都换 enriched 代次 (generation), 回测读矩阵/面板时代次一变就报
「指标数据正在发布更新, 请稍后重试」, 年化等统计都出不来; 就算碰巧读成功, 最后一天也是半天的价格。
结束日改到「最近一个已定版的日K」(quote_official_close.json 记的官方收盘日, 或今天之前的分区),
回测只读定版数据, 不再追着快照跑。"""
from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from app.storage.repository import DataStore, KlineRepository


def _part(data_dir, day: str, *, quote_ts: int | None, table: str = "kline_daily_enriched") -> None:
    out = data_dir / table / f"date={day}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["AAA"], "date": [date.fromisoformat(day)], "open": [1.0], "high": [1.0], "low": [1.0],
                  "close": [1.0], "volume": [1.0], "quote_ts": [quote_ts]},
                 schema_overrides={"quote_ts": pl.Int64}).write_parquet(out)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 10, 30))
    for d in ("2026-10-27", "2026-10-28", "2026-10-29"):
        _part(tmp_path, d, quote_ts=None)
    _part(tmp_path, "2026-10-30", quote_ts=1_793_385_000_000)        # 盘中快照, 未定版
    return KlineRepository(DataStore(tmp_path))


def test_latest_settled_daily_date_skips_today_live_snapshot(repo):
    assert repo.latest_enriched_date("stock") == date(2026, 10, 30)        # 原口径: 连快照也算
    assert repo.latest_settled_daily_date("stock") == date(2026, 10, 29)


def test_today_counts_once_official_close_is_marked(repo, tmp_path):
    (tmp_path / "quote_official_close.json").write_text(json.dumps(["2026-10-29", "2026-10-30"]), encoding="utf-8")
    assert repo.latest_settled_daily_date("stock") == date(2026, 10, 30)


def test_backtest_end_clamped_to_settled_day(repo):
    from app.api.backtest import clamp_backtest_end
    assert clamp_backtest_end(repo, date(2026, 10, 30), "stock") == date(2026, 10, 29)
    assert clamp_backtest_end(repo, date(2026, 11, 2), "stock") == date(2026, 10, 29)
    assert clamp_backtest_end(repo, date(2026, 10, 28), "stock") == date(2026, 10, 28)   # 更早的结束日不动


def test_data_status_reports_settled_date_for_backtest_default(tmp_path, monkeypatch):
    """回测页默认结束日读 data/status 的 enriched.settled_date (不含盘中快照), latest_date 口径不变。"""
    from app.api import data as data_api
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 10, 30))
    for d in ("2026-10-28", "2026-10-29"):
        _part(tmp_path, d, quote_ts=None)
    _part(tmp_path, "2026-10-30", quote_ts=1_793_385_000_000)
    repo = KlineRepository(DataStore(tmp_path))
    stats = data_api._safe_aggregate_enriched(repo)
    assert stats["latest_date"] == "2026-10-30"
    assert stats["settled_date"] == "2026-10-29"
