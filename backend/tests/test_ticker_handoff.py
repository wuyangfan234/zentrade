"""Ticker hand-offs must not leak another issuer's corporate actions into a symbol's adjustment chain.

Real cases (2026-10-01 production audit): GOLD (Barrick -> B 2025-05; A-Mark AMRK -> GOLD 2025-12),
CALY (Callaway -> CALI 2024-10; MODG -> CALY 2026-01), FFR, CBIO, DOO, RBC, CR. Alpaca's adjustment=all
series for the reused ticker applies the previous issuer's dividends to the new issuer's prices.
"""
from datetime import date, datetime

import pytest

from app.plugins.alpaca import provider as alp
from app.services import ticker_handoff as th


def name_change(old_symbol, new_symbol, old_cusip, new_cusip, day):
    return {"old_symbol": old_symbol, "new_symbol": new_symbol, "old_cusip": old_cusip,
            "new_cusip": new_cusip, "process_date": day}


GOLD_PAYLOAD = {
    "name_changes": [
        name_change("GOLD", "B", "067901108", "06849F108", "2025-05-09"),      # Barrick leaves GOLD
        name_change("AMRK", "GOLD", "00181T107", "00181T107", "2025-12-02"),   # A-Mark takes GOLD
    ],
    "cash_dividends": [
        {"symbol": "GOLD", "cusip": "067901108", "ex_date": "2021-02-26", "rate": 0.09},   # Barrick
        {"symbol": "GOLD", "cusip": "067901108", "ex_date": "2025-02-28", "rate": 0.10},   # Barrick
        {"symbol": "GOLD", "cusip": "00181T107", "ex_date": "2026-03-10", "rate": 0.20},   # A-Mark, after arrival
    ],
}


def test_previous_issuer_dates_are_foreign_and_new_issuer_dates_are_kept():
    foreign = th.foreign_action_dates(GOLD_PAYLOAD, ["GOLD", "NVDA"])
    assert foreign == {"GOLD": {date(2021, 2, 26), date(2025, 2, 28)}}


def test_same_issuer_cusip_or_symbol_changes_are_not_handoffs():
    payload = {
        "name_changes": [
            name_change("META", "METV", "53656F417", "53656F417", "2022-01-31"),   # Roundhill ETF left META
            name_change("FB", "META", "30303M102", "30303M102", "2022-06-09"),     # Meta arrived
            name_change("CLS", "CLS", "15101Q108", "15101Q207", "2024-04-30"),     # same issuer, new CUSIP
            name_change("GSK", "GSK.WI", "37733W105", "37733W204", "2022-07-20"),  # when-issued round trip
            name_change("GSK.WI", "GSK", "37733W204", "37733W204", "2022-07-22"),
        ],
        "cash_dividends": [
            {"symbol": "CLS", "cusip": "15101Q108", "ex_date": "2023-01-05", "rate": 0.1},
            {"symbol": "GSK", "cusip": "37733W105", "ex_date": "2022-05-19", "rate": 0.5},
        ],
    }
    # META: the departing METV ETF and arriving Meta are different issuers, but no actions are listed
    # under META for the ETF, so nothing is foreign. CLS/GSK never changed issuer.
    assert th.foreign_action_dates(payload, ["META", "CLS", "GSK"]) == {}


def test_a_date_shared_with_an_own_issuer_action_is_not_dropped():
    payload = {
        "name_changes": GOLD_PAYLOAD["name_changes"],
        "cash_dividends": GOLD_PAYLOAD["cash_dividends"] + [
            {"symbol": "GOLD", "cusip": "00181T107", "ex_date": "2025-02-28", "rate": 0.2}],
    }
    assert th.foreign_action_dates(payload, ["GOLD"]) == {"GOLD": {date(2021, 2, 26)}}


def _bar(day, close, volume=1_000_000):
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": volume, "vw": close}


def test_provider_drops_foreign_events_and_keeps_own_events(monkeypatch):
    # A-Mark prices; adjustment=all wrongly carries Barrick's 2021-02-26 dividend and A-Mark's own 2021-06-01 one.
    raw = [("GOLD", _bar("2021-02-25", 30.0)), ("GOLD", _bar("2021-02-26", 30.0)),
           ("GOLD", _bar("2021-05-28", 31.0)), ("GOLD", _bar("2021-06-01", 31.0))]
    adjusted = [("GOLD", _bar("2021-02-25", 27.0)), ("GOLD", _bar("2021-02-26", 27.3)),
                ("GOLD", _bar("2021-05-28", 28.21)), ("GOLD", _bar("2021-06-01", 28.6))]

    def source(key, secret, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        yield from (raw if adjustment == "raw" else adjusted)

    payload = {**GOLD_PAYLOAD, "cash_dividends": GOLD_PAYLOAD["cash_dividends"] + [
        {"symbol": "GOLD", "cusip": "00181T107", "ex_date": "2021-06-01", "rate": 0.4}]}
    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(alp, "_iter_bars", source)
    monkeypatch.setattr(th, "fetch_corporate_actions", lambda symbols, start, end, *, types: payload)
    df = alp.AlpacaProvider().get_adj_factors(["GOLD"], datetime(2021, 2, 20), datetime(2021, 6, 2))
    assert df["trade_date"].to_list() == [date(2021, 6, 1)]


def test_provider_fails_closed_when_the_handoff_check_cannot_run(monkeypatch):
    def source(key, secret, symbols, timeframe, start, end, adjustment, feed, *a, **kw):
        yield "GOLD", _bar("2021-02-25", 30.0)
        yield "GOLD", _bar("2021-02-26", 30.0 if adjustment == "raw" else 29.0)

    def unavailable(symbols, start, end, *, types):
        raise ConnectionError("corporate actions down")

    monkeypatch.setattr(alp, "_load_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(alp, "_iter_bars", source)
    monkeypatch.setattr(th, "fetch_corporate_actions", unavailable)
    with pytest.raises(ConnectionError):
        alp.AlpacaProvider().get_adj_factors(["GOLD"], datetime(2021, 2, 20), datetime(2021, 3, 1))
