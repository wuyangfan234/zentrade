"""Repository anchors for long-suspended symbols (real DuckDB views, no mocks)."""
from datetime import date, timedelta

import polars as pl

from app.storage.repository import DataStore, KlineRepository


def _write(root, rows):
    frame = pl.DataFrame(rows, schema={"symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
                                       "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64})
    for part in frame.partition_by("date"):
        out = root / "kline_daily" / f"date={part['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(out)


def _row(symbol, d):
    return {"symbol": symbol, "date": d, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 100.0, "amount": 1000.0}


def test_only_symbols_without_a_bar_near_the_window_get_an_anchor(tmp_path):
    window = date(2026, 9, 23)
    _write(tmp_path, [
        _row("HALT", date(2026, 8, 31)), _row("HALT", date(2026, 9, 30)),     # 停牌跨越窗口前段
        _row("LIVE", date(2026, 9, 22)), _row("LIVE", date(2026, 9, 23)),     # 窗口内正常交易
        _row("NEW", date(2026, 9, 29)),                                        # 窗口前无历史
    ])
    repo = KlineRepository(DataStore(tmp_path))
    repo.rebuild_views()
    anchors = repo.daily_anchor_dates(window, timedelta(days=7))
    assert anchors == {"HALT": date(2026, 8, 31)}
