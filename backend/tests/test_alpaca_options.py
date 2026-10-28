from datetime import date

from app.services import alpaca_options as ao
from app.services import options_analytics as oa

TODAY = date(2026, 10, 15)


def _fetch(symbol, today):
    contracts = {
        "LITE261023C00700000": {"underlying_symbol": "LITE", "expiration_date": "2026-10-23", "strike_price": "700",
                                "type": "call", "open_interest": "1200", "open_interest_date": "2026-10-14"},
        "LITE261023P00600000": {"underlying_symbol": "LITE", "expiration_date": "2026-10-23", "strike_price": "600",
                                "type": "put", "open_interest": "300", "open_interest_date": "2026-10-14"},
    }
    snaps = {"LITE261023C00700000": {"latestQuote": {"bp": 10.0, "ap": 11.0}, "impliedVolatility": 0.8,
                                     "greeks": {"delta": 0.3}, "dailyBar": {"v": 50, "t": "2026-10-14T04:00:00Z"}}}
    return contracts, snaps


def test_chain_builds_rows_caches_per_day_and_feeds_heatmap(tmp_path):
    calls = []

    def f(s, d):
        calls.append(s)
        return _fetch(s, d)
    r = ao.chain(tmp_path, "lite", today=TODAY, fetch=f)
    assert r["available"] and r["cached"] is False and len(r["rows"]) == 2
    call = next(x for x in r["rows"] if x["type"] == "call")
    assert call["mark"] == 10.5 and call["volume"] == 50 and call["open_interest"] == "1200"
    assert ao.chain(tmp_path, "LITE", today=TODAY, fetch=f)["cached"] is True and calls == ["LITE"]
    h = oa.analyze(r["rows"], 650.0, today=TODAY)["heatmap"]
    assert h["resistance"] == 700 and h["support"] == 600 and h["stance"] == "偏多"


def test_chain_failure_is_unavailable(tmp_path):
    def boom(s, d):
        raise OSError("x")
    assert ao.chain(tmp_path, "LITE", today=TODAY, fetch=boom)["available"] is False
