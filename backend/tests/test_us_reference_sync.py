"""Public Nasdaq reference cache and durable sector/risk metadata."""
import importlib
import json
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_clock import us_clock
from app.plugins.alpaca import provider as alp
from app.services import instrument_sync as sync
from app.storage.repository import DataStore, KlineRepository


NOW = datetime(2026, 9, 29, 8, 0, tzinfo=us_clock.tz)


def reference_module():
    return importlib.import_module("app.services.us_reference")


def payload():
    return {"data": {"totalrecords": 3, "rows": [
        {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "sector": "Technology", "industry": "Computer Manufacturing"},
        {"symbol": "NTRS", "name": "Northern Trust Corporation Common Stock", "sector": "Finance", "industry": "Major Banks"},
        {"symbol": "UNK", "name": "Unknown Corp", "sector": "N/A", "industry": ""},
    ]}}


def test_reference_caches_valid_download_and_preserves_source_date_on_failure(tmp_path, monkeypatch):
    ref = reference_module()
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    fetch = Mock(return_value=payload())
    monkeypatch.setattr(ref, "_fetch_nasdaq", fetch)
    got = ref.load_us_reference(tmp_path)
    assert got.status == "fresh" and got.as_of == NOW.date().isoformat()
    assert got.rows["AAPL"]["sector"] == "Technology"
    assert got.rows["UNK"]["sector"] is None
    path = tmp_path / "reference" / "nasdaq_stocks.json"
    disk_before = path.read_bytes()
    assert json.loads(disk_before)["as_of"] == got.as_of
    assert ref.load_us_reference(tmp_path).status == "cache"
    assert fetch.call_count == 1
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(days=1))
    monkeypatch.setattr(ref, "_fetch_nasdaq", Mock(side_effect=OSError("offline")))
    stale = ref.load_us_reference(tmp_path)
    assert stale.status == "stale_cache" and stale.as_of == NOW.date().isoformat()
    assert stale.rows == got.rows
    assert path.read_bytes() == disk_before


@pytest.mark.parametrize("bad", [{"data": None}, {"data": {"rows": []}},
    {"data": {"totalrecords": 4, "rows": payload()["data"]["rows"]}}])
def test_invalid_or_partial_reference_does_not_replace_cache(tmp_path, monkeypatch, bad):
    ref = reference_module()
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(ref, "_fetch_nasdaq", lambda: payload())
    ref.load_us_reference(tmp_path)
    path = tmp_path / "reference" / "nasdaq_stocks.json"
    before = path.read_bytes()
    monkeypatch.setattr(ref, "_fetch_nasdaq", lambda: bad)
    got = ref.load_us_reference(tmp_path, force=True)
    assert got.status == "stale_cache"
    assert path.read_bytes() == before


def test_atomic_reference_publication_failure_preserves_old_file(tmp_path, monkeypatch):
    ref = reference_module()
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(ref, "_fetch_nasdaq", lambda: payload())
    ref.load_us_reference(tmp_path)
    path = tmp_path / "reference" / "nasdaq_stocks.json"
    before = path.read_bytes()
    monkeypatch.setattr(ref.os, "replace", Mock(side_effect=OSError("disk failure")))
    got = ref.load_us_reference(tmp_path, force=True)
    assert got.status == "stale_cache"
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["nasdaq_stocks.json"]


def test_no_reference_available_remains_unknown(tmp_path, monkeypatch):
    ref = reference_module()
    monkeypatch.setattr(ref, "_fetch_nasdaq", Mock(side_effect=OSError("offline")))
    got = ref.load_us_reference(tmp_path)
    assert got.rows == {} and got.as_of is None and got.status == "unavailable"
    assert not (tmp_path / "reference" / "nasdaq_stocks.json").exists()


@pytest.mark.parametrize("name, expected", [
    ("Northern Trust Corporation Common Stock", "common_stock"),
    ("Stock Yards Bancorp, Inc. Common Stock", "common_stock"),
    ("Digital Realty Trust, Inc. Common Stock", "common_stock"),
    ("Berkshire Hathaway Inc. Class B Common Stock", "common_stock"),
    ("Invesco QQQ Trust, Series 1", "etf"),
    ("Example Technology ETF", "etf"),
    ("Example Corporation Series A Preferred Stock", "preferred"),
    ("Example Corp Warrants", "warrant"),
    ("Example Corp Units", "unit"),
])
def test_risk_classification_uses_security_terms_not_stock_or_trust_substrings(name, expected):
    ref = reference_module()
    symbol = "QQQ" if name.startswith("Invesco QQQ") else "TEST"
    assert ref.classify_instrument({"symbol": symbol, "name": name})["instrument_type"] == expected


def test_alpaca_retains_class_b_stock_and_tags_etf_instead_of_misclassifying(monkeypatch):
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("test", "test"))
    items = [{"symbol": s, "name": n, "exchange": "NYSE", "tradable": True, "status": "active"}
             for s, n in [("BRK.B", "Berkshire Hathaway Class B Common Stock"),
                          ("NTRS", "Northern Trust Corporation Common Stock"),
                          ("FUND", "Acme Equity ETF"), ("PREF", "Acme Preferred Stock")]]
    monkeypatch.setattr(alp, "_get", lambda *a, **k: items)
    rows = {r["symbol"]: r for r in alp.AlpacaProvider().get_instruments()}
    assert set(rows) == {"BRK.B", "NTRS", "FUND", "PREF"}
    assert rows["BRK.B"]["instrument_type"] == "common_stock"
    assert rows["FUND"]["is_etf"] is True and rows["FUND"]["asset_type"] == "etf"
    assert rows["PREF"]["is_preferred"] is True


def test_flatten_preserves_explicit_sector_and_instrument_metadata():
    row = sync._flatten_instruments([{"symbol": "AAPL", "sector": "Technology",
        "industry": "Computer Manufacturing", "asset_type": "stock", "source": "alpaca",
        "instrument_type": "common_stock", "is_etf": False, "sector_as_of": "2026-09-29"}])[0]
    assert row["sector"] == "Technology" and row["industry"] == "Computer Manufacturing"
    assert row["source"] == "alpaca" and row["is_etf"] is False


def test_sync_persists_sectors_for_repository_and_keeps_unknown_unknown(tmp_path, monkeypatch):
    ref = reference_module()
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(ref, "_fetch_nasdaq", lambda: payload())
    items = [{"symbol": s, "name": n, "source": "alpaca", "asset_type": "stock"}
             for s, n in [("AAPL", "Apple Common Stock"), ("NTRS", "Northern Trust Common Stock"),
                          ("UNK", "Unknown Corp"), ("MISSING", "Missing Common Stock")]]
    monkeypatch.setattr(sync, "_fetch_instruments_via_provider", lambda: sync._flatten_instruments(items))
    assert sync.sync_instruments(tmp_path) == 4
    rows = {r["symbol"]: r for r in KlineRepository(DataStore(tmp_path)).get_instruments().to_dicts()}
    assert rows["AAPL"]["sector"] == "Technology"
    assert rows["NTRS"]["sector"] == "Finance"
    assert rows["UNK"]["sector"] is None and rows["MISSING"]["sector"] is None
    assert rows["AAPL"]["sector_as_of"] == NOW.date().isoformat()
    assert rows["AAPL"]["sector_source"] == "nasdaq_screener"
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(days=1))
    monkeypatch.setattr(ref, "_fetch_nasdaq", Mock(side_effect=OSError("offline")))
    sync.sync_instruments(tmp_path)
    row = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet").filter(pl.col("symbol") == "AAPL").to_dicts()[0]
    assert row["sector"] == "Technology"
    assert row["sector_as_of"] == NOW.date().isoformat()
    assert row["reference_status"] == "stale_cache"
    assert row["as_of"] == (NOW + timedelta(days=1)).date()
