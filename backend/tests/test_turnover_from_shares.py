"""第 3 批 (用户 2026-10-12 批准改变策略排名): 用 EDGAR 总股本恢复换手率; 多股类公司排除。"""
from __future__ import annotations

from datetime import date

import polars as pl
from test_edgar_fundamentals import _facts

from app.services import edgar_fundamentals as ef
from app.share_capital import apply_historical_float_shares, load_share_history


def test_today_row_falls_back_to_latest_announced_shares():
    rows = pl.DataFrame({"symbol": ["AAPL", "AAPL"], "date": [date(2026, 5, 4), date(2026, 10, 12)],
                         "float_shares": [None, None]}, schema_overrides={"float_shares": pl.Float64})
    shares = pl.DataFrame({"symbol": ["AAPL"], "period_end": ["2026-04-20"], "announce_date": ["2026-05-01"],
                           "float_shares": [990.0]})
    out = apply_historical_float_shares(rows, shares, today=date(2026, 10, 12))
    assert out["float_shares"].to_list() == [990.0, 990.0]


def test_today_row_keeps_instrument_value_when_present():
    rows = pl.DataFrame({"symbol": ["AAPL"], "date": [date(2026, 10, 12)], "float_shares": [1234.0]})
    shares = pl.DataFrame({"symbol": ["AAPL"], "period_end": ["2026-04-20"], "announce_date": ["2026-05-01"],
                           "float_shares": [990.0]})
    assert apply_historical_float_shares(rows, shares, today=date(2026, 10, 12))["float_shares"][0] == 1234.0


def test_multi_class_detected_and_shares_nulled():
    f = _facts()
    f["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"].append(
        {"val": 50, "end": "2026-04-20", "filed": "2026-05-01", "form": "10-Q", "fp": "Q1", "fy": 2026, "accn": "x"})
    t = ef.parse_companyfacts("GOOGL", f)
    sh = t["shares"]
    assert sh["multi_class"].all() and sh["float_shares"].is_null().all()
    assert t["metrics"]["bps"].is_null().all()


def test_dotted_ticker_flagged_multi_class():
    t = ef.parse_companyfacts("BRK.B", _facts())
    assert t["shares"]["multi_class"].all() and t["shares"]["float_shares"].is_null().all()


def test_single_class_not_flagged():
    t = ef.parse_companyfacts("AAPL", _facts())
    assert not t["shares"]["multi_class"].any()


def test_load_share_history_skips_multi_class(tmp_path):
    ef.write_tables(tmp_path, [ef.parse_companyfacts("AAPL", _facts()), ef.parse_companyfacts("BRK.B", _facts())])
    h = load_share_history(tmp_path)
    assert set(h.filter(pl.col("float_shares").is_not_null())["symbol"]) == {"AAPL"}
