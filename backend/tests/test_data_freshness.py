from datetime import date
from types import SimpleNamespace

import polars as pl

from app.services import data_freshness as df
from app.services import kline_4h


def _m30(tmp_path, rows):
    kline_4h.write_month(tmp_path, "2026-10", pl.DataFrame({
        "symbol": [r[0] for r in rows], "t": [r[1] for r in rows],
        "open": [1.0] * len(rows), "high": [1.0] * len(rows), "low": [1.0] * len(rows),
        "close": [1.0] * len(rows), "volume": [1.0] * len(rows)}))


def test_freshness_flags_lagging_m30_and_new_listing(tmp_path, monkeypatch):
    days = [date(2026, 10, d) for d in (8, 9, 12, 13, 14)]
    monkeypatch.setattr(kline_4h, "trading_days", lambda repo, a, b: days)
    daily = pl.DataFrame({"symbol": ["AAA"] * 200 + ["NEW"] * 60, "date": [date(2026, 10, 14)] * 260})
    repo = SimpleNamespace(get_daily_batch=lambda syms, a, b, columns=None: daily)
    _m30(tmp_path, [("AAA", "2026-10-14T19:30:00Z"), ("NEW", "2026-10-02T19:30:00Z")])
    r = df.check(tmp_path, repo, ["AAA", "NEW"], today=date(2026, 10, 14))
    assert [x["symbol"] for x in r["stale"]] == ["NEW"]
    assert r["new_listing"] == [{"symbol": "NEW", "daily_bars": 60}]
    state, text = df.summary(r)
    assert state == "warn" and "NEW" in text and "上市不足 130 日" in text


def test_freshness_ok_when_all_current(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_4h, "trading_days", lambda repo, a, b: [date(2026, 10, 14)])
    daily = pl.DataFrame({"symbol": ["AAA"] * 200, "date": [date(2026, 10, 14)] * 200})
    repo = SimpleNamespace(get_daily_batch=lambda syms, a, b, columns=None: daily)
    _m30(tmp_path, [("AAA", "2026-10-14T19:30:00Z")])
    assert df.summary(df.check(tmp_path, repo, ["AAA"], today=date(2026, 10, 14)))[0] == "ok"
