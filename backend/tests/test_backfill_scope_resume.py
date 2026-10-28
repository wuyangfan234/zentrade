"""Isolated backfill resume must be bound to its exact scope (independent review R7)."""
import importlib.util
import json
import sys
from pathlib import Path

import polars as pl
import pytest

import app.plugins.alpaca.provider as alp

SCRIPT = Path(__file__).parents[1] / "scripts" / "backfill_daily_isolated.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(monkeypatch):
    calls = []
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("offline", "offline"))

    def iter_bars(k, s, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        calls.append((tuple(symbols), str(start.date()), str(end.date()), adjustment))
        for sym in symbols:
            for d in (start.date(), end.date()):
                yield sym, {"t": f"{d}T04:00:00Z", "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1000.0, "vw": 10.0}
    monkeypatch.setattr(alp, "_iter_bars", iter_bars)
    return calls


def _run(monkeypatch, tmp_path, symbol, start, end):
    inst = tmp_path / f"inst-{symbol}.parquet"
    pl.DataFrame({"symbol": [symbol], "instrument_type": ["common_stock"]}).write_parquet(inst)
    monkeypatch.setattr(sys, "argv", ["backfill", "--instruments", str(inst), "--out", str(tmp_path / "out"),
                                      "--start", start, "--end", end, "--extra", "", "--workers", "1"])
    _load().main()


def test_second_scope_never_publishes_the_first_scopes_cache(monkeypatch, tmp_path, source):
    _run(monkeypatch, tmp_path, "A", "2024-06-07", "2024-06-10")
    first = len(source)
    _run(monkeypatch, tmp_path, "B", "2023-01-03", "2023-01-04")
    assert len(source) > first, "different scope must fetch, not reuse another scope's chunks"
    stored = pl.read_parquet(str(tmp_path / "out" / "kline_daily" / "**" / "*.parquet"))
    assert stored["symbol"].unique().to_list() == ["B"]
    summary = json.loads((tmp_path / "out" / "backfill-summary.json").read_text())
    assert summary["scope"]["symbols"] == 1 and summary["scope"]["start"] == "2023-01-03"


def test_same_scope_resumes_without_refetching(monkeypatch, tmp_path, source):
    _run(monkeypatch, tmp_path, "A", "2024-06-07", "2024-06-10")
    first = len(source)
    _run(monkeypatch, tmp_path, "A", "2024-06-07", "2024-06-10")
    assert len(source) == first
