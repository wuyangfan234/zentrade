"""Alpaca paper mirror (approved 2026-10-02: enabled, dedicated paper account, market orders).
Mirrors only ledger fills after it starts (starts flat); never sells what it did not buy; paper endpoint only;
idempotent via client_order_id; daily order cap; kill switch; records real fill vs ledger price."""
from datetime import datetime, timedelta

import pytest

from app.market_clock import us_clock
from app.services import alpaca_mirror as am

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=us_clock.tz)


class FakeAlpaca:
    def __init__(self):
        self.orders = {}
        self.posts = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/account":
            return {"status": "ACTIVE", "equity": "100000"}
        if method == "POST" and path == "/orders":
            self.posts.append(body)
            cid = body["client_order_id"]
            if cid in self.orders:
                raise am.AlpacaHTTPError(422, "client_order_id must be unique")
            self.orders[cid] = {"id": f"o-{len(self.orders)}", "client_order_id": cid, "status": "accepted",
                                "symbol": body["symbol"], "qty": body["qty"], "side": body["side"]}
            return self.orders[cid]
        if method == "GET" and path.startswith("/orders:by_client_order_id"):
            cid = path.split("=", 1)[1]
            return self.orders[cid]
        raise AssertionError((method, path))

    def fill(self, cid, price):
        self.orders[cid].update(status="filled", filled_avg_price=str(price), filled_qty=self.orders[cid]["qty"],
                                filled_at=NOW.isoformat())


def ev(seq, kind, sym, qty, price, side=None, **kw):
    return {"seq": seq, "kind": kind, "symbol": sym, "qty": qty, "price": price, "side": side, "at": NOW.isoformat(),
            "trade_date": "2026-10-02", **kw}


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "in_session", lambda *a: True)
    fake = FakeAlpaca()
    m = am.AlpacaMirror(tmp_path, http=fake)
    return m, fake


def test_paper_endpoint_guard():
    with pytest.raises(ValueError):
        am.AlpacaMirror.check_base("https://api.alpaca.markets/v2")
    am.AlpacaMirror.check_base("https://paper-api.alpaca.markets/v2")


def test_starts_flat_and_ignores_history(mirror):
    m, fake = mirror
    history = [ev(5, "fill", "SSL", 1590, 13.77, side="buy")]
    m.start(history)
    m.sync(history + [ev(6, "close", "SSL", 1590, 14.0)])
    m.submit_pending()
    assert fake.posts == []  # legacy position: never bought by the mirror → its exit is not mirrored
    assert m.orders()[0]["status"] == "skipped_not_mirrored"


def test_buy_then_partial_then_close_mirrored_with_market_orders(mirror):
    m, fake = mirror
    m.start([])
    m.sync([ev(1, "fill", "AAA", 100, 50.0, side="buy")])
    m.submit_pending()
    assert fake.posts[0] == {"symbol": "AAA", "qty": "100", "side": "buy", "type": "market", "time_in_force": "day",
                             "client_order_id": "zt-1"}
    fake.fill("zt-1", 50.05)
    m.poll()
    o = m.orders()[0]
    assert o["status"] == "filled" and o["deviation_bps"] == pytest.approx(10.0, abs=0.01)  # paid 10 bps more
    m.sync([ev(1, "fill", "AAA", 100, 50.0, side="buy"), ev(2, "partial_close", "AAA", 50, 55.0),
            ev(3, "close", "AAA", 80, 56.0)])  # close qty larger than mirror holds after partial → capped
    m.submit_pending()
    fake.fill("zt-2", 54.945)
    m.poll()
    m.submit_pending()
    sells = [p for p in fake.posts if p["side"] == "sell"]
    assert [p["qty"] for p in sells] == ["50", "50"]
    assert m.positions() == {"AAA": 50.0}  # second sell not yet filled


def test_duplicate_submission_is_idempotent(mirror, tmp_path):
    m, fake = mirror
    m.start([])
    m.sync([ev(1, "fill", "AAA", 10, 50.0, side="buy")])
    m.submit_pending()
    m2 = am.AlpacaMirror(tmp_path, http=fake)
    m2._state["orders"]["zt-1"]["status"] = "new"  # simulate crash before status was saved
    m2.submit_pending()
    assert m2.orders()[0]["status"] == "submitted" and len(fake.orders) == 1


def test_kill_switch_and_daily_cap(mirror):
    m, fake = mirror
    m.start([])
    m.set_enabled(False, note="test")
    m.sync([ev(1, "fill", "AAA", 10, 50.0, side="buy")])
    m.submit_pending()
    assert fake.posts == []
    m.set_enabled(True, note="test")
    m.max_orders_per_day = 1
    m.sync([ev(1, "fill", "AAA", 10, 50.0, side="buy"), ev(2, "fill", "BBB", 10, 20.0, side="buy")])
    m.submit_pending()
    assert len(fake.posts) == 1 and m.orders()[1]["status"] == "blocked_daily_cap"


def test_out_of_session_intent_expires_instead_of_filling_next_open(mirror, monkeypatch):
    m, fake = mirror
    m.start([])
    m.sync([ev(1, "fill", "AAA", 10, 50.0, side="buy")])
    monkeypatch.setattr(us_clock, "in_session", lambda *a: False)
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(hours=7))
    m.submit_pending()
    assert fake.posts == [] and m.orders()[0]["status"] == "expired_out_of_session"


def test_summary_stats(mirror):
    m, fake = mirror
    m.start([])
    m.sync([ev(1, "fill", "AAA", 100, 50.0, side="buy")])
    m.submit_pending()
    fake.fill("zt-1", 50.10)
    m.poll()
    s = m.summary()
    assert s["filled"] == 1 and s["avg_deviation_bps"] == pytest.approx(20.0, abs=0.01) and s["enabled"] is True
