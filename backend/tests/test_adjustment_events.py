"""复权口径 v2 回归 (9-30 审计): raw/all 水平比值 → 除权事件 → enriched 前复权。

真实数值取自 Alpaca SIP 日K (NVDA 2024-06-10 1:10 拆股, F 2024-06 分红, SIRI 2024-09-10 1:10 合股)。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.indicators.pipeline import _apply_adj_factor, _load_factors
from app.services.adjustment_events import events_from_bars, events_for_symbols
from app.services.kline_sync import ADJ_EVENT_BASIS, adj_factor_basis, store_adj_factor_events


def _series(rows):
    return {date.fromisoformat(d): (c, v) for d, c, v in rows}


NVDA_RAW = _series([("2024-06-06", 1209.98, 66_469_619), ("2024-06-07", 1208.88, 41_238_580),
                    ("2024-06-10", 121.79, 314_162_666), ("2024-06-11", 120.91, 222_551_158)])
NVDA_ALL = _series([("2024-06-06", 120.65, 664_696_190), ("2024-06-07", 120.54, 412_385_800),
                    ("2024-06-10", 121.44, 314_162_666), ("2024-06-11", 120.58, 222_551_158)])


def test_split_is_one_event_not_daily_levels():
    events = events_from_bars(NVDA_RAW, NVDA_ALL)
    assert len(events) == 1
    ev = events[0]
    assert ev["trade_date"] == date(2024, 6, 10)
    assert ev["ex_factor"] == pytest.approx(10.0, rel=1e-3)
    assert ev["vol_factor"] == pytest.approx(10.0, rel=1e-9)


def test_reverse_split_factor_below_one():
    raw = _series([("2024-09-06", 3.70, 30_000_000), ("2024-09-09", 3.65, 28_000_000), ("2024-09-10", 36.20, 2_900_000)])
    adj = _series([("2024-09-06", 37.00, 3_000_000), ("2024-09-09", 36.50, 2_800_000), ("2024-09-10", 36.20, 2_900_000)])
    (ev,) = events_from_bars(raw, adj)
    assert ev["trade_date"] == date(2024, 9, 10)
    assert ev["ex_factor"] == pytest.approx(0.1, rel=2e-3)
    assert ev["vol_factor"] == pytest.approx(0.1, rel=1e-9)


def test_rounding_noise_is_not_an_event():
    """低价股分位舍入会让相邻日水平比值抖动 ~1e-4, 不能被当成除权事件。"""
    raw = _series([("2024-06-03", 12.19, 1), ("2024-06-04", 12.03, 1), ("2024-06-05", 12.07, 1),
                   ("2024-06-06", 12.07, 1), ("2024-06-07", 12.15, 1)])
    adj = _series([("2024-06-03", 10.66, 1), ("2024-06-04", 10.52, 1), ("2024-06-05", 10.55, 1),
                   ("2024-06-06", 10.55, 1), ("2024-06-07", 10.62, 1)])
    assert events_from_bars(raw, adj) == []


def test_cash_dividend_detected_on_ex_date_with_price_only_factor():
    # 前一日 L=1.0200, 除权日 L=1.0000: 事件前价格需要 ÷1.02
    raw = _series([("2026-09-24", 51.00, 1000), ("2026-09-25", 50.00, 1000)])
    adj = _series([("2026-09-24", 50.00, 1000), ("2026-09-25", 50.00, 1000)])
    (ev,) = events_from_bars(raw, adj)
    assert ev["trade_date"] == date(2026, 9, 25)
    assert ev["ex_factor"] == pytest.approx(1.02, rel=1e-9)
    assert ev["vol_factor"] == 1.0


def test_events_for_symbols_accepts_alpaca_bar_stream():
    def stream(series, sym):
        return [(sym, {"t": f"{d.isoformat()}T04:00:00Z", "c": c, "v": v}) for d, (c, v) in series.items()]
    rows = events_for_symbols(stream(NVDA_RAW, "NVDA"), stream(NVDA_ALL, "NVDA"))
    assert [(r["symbol"], r["trade_date"]) for r in rows] == [("NVDA", date(2024, 6, 10))]


def _bars(symbol, closes, volumes, start=date(2024, 6, 5)):
    days = [start + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame({"symbol": [symbol] * len(closes), "date": days,
                         "open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": [float(v) for v in volumes]})


def test_enriched_adjustment_makes_split_series_continuous():
    """事件表经 _apply_adj_factor 后, 拆股前价格 ÷10、成交量 ×10, 序列连续。"""
    raw = _bars("NVDA", [1224.40, 1209.98, 121.79, 120.91], [52_840_178, 66_469_619, 314_162_666, 222_551_158])
    events = pl.DataFrame([{"symbol": "NVDA", **events_from_bars(NVDA_RAW, NVDA_ALL)[0]}])
    events = events.with_columns(pl.lit(date(2024, 6, 7)).alias("trade_date"))  # 对齐合成日期: 第3根为除权日
    out = _apply_adj_factor(raw, events).sort("date")
    closes = out["close"].to_list()
    assert closes[1] == pytest.approx(120.99, rel=2e-3)
    assert closes[2] == pytest.approx(121.79)
    assert abs(closes[2] / closes[1] - 1) < 0.02  # 无拆股断崖
    assert out["volume"][1] == pytest.approx(664_696_190, rel=1e-6)
    assert out["volume"][2] == pytest.approx(314_162_666)


def test_store_replaces_legacy_level_table_and_marks_basis(tmp_path):
    d = tmp_path / "adj_factor"
    d.mkdir()
    legacy = pl.DataFrame({"symbol": ["X"] * 3, "trade_date": [date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)],
                           "ex_factor": [1.04, 1.04, 1.0]})
    legacy.write_parquet(d / "all.parquet")
    assert adj_factor_basis(d) is None
    assert _load_factors(d / "all.parquet").is_empty()  # 旧口径不得进入复权链

    new = pl.DataFrame({"symbol": ["X"], "trade_date": [date(2026, 9, 29)], "ex_factor": [1.04], "vol_factor": [1.0]})
    changed, affected = store_adj_factor_events(d, new)
    assert adj_factor_basis(d) == ADJ_EVENT_BASIS
    stored = pl.read_parquet(d / "all.parquet")
    assert stored.height == 1 and affected == ["X"] and changed == 1
    assert not _load_factors(d / "all.parquet").is_empty()

    # 幂等: 同一事件再写, 无变化
    assert store_adj_factor_events(d, new) == (0, [])
    # 新事件增量合并
    more = pl.DataFrame({"symbol": ["Y"], "trade_date": [date(2026, 9, 30)], "ex_factor": [2.0], "vol_factor": [2.0]})
    changed, affected = store_adj_factor_events(d, more)
    assert (changed, affected) == (1, ["Y"])
    assert json.loads((d / "basis.json").read_text())["basis"] == ADJ_EVENT_BASIS
