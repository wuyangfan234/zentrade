"""Offline broker-contract regressions: restart, cumulative fills, round closeout.

The HTTP stub is deliberately stateful: DELETE acknowledges a cancellation request,
not cancellation completion; a lost POST acknowledgement can still create a fill.
"""
from datetime import datetime, timedelta
import json

import pytest

from app.market_clock import us_clock
from app.services import alpaca_mirror as am

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=us_clock.tz)


def event(seq, kind="fill", side="short", qty=100, **extra):
    return {"seq": seq, "kind": kind, "side": side, "symbol": "XYZ", "qty": qty,
            "price": 100, "at": NOW.isoformat(), **extra}


def exit_event(seq=2, *, side="short", kind="close", qty=100, **extra):
    return event(seq, kind=kind, side=None, position_side=side, qty=qty, **extra)


class Broker:
    def __init__(self):
        self.remote = {}
        self.calls = []
        self.post_error = None
        self.get_error = None
        self.cancel_error = None
        self.cancel_completes = True
        self.cancel_fill_qty = None
        self.before_post = None
        self.immediate_fill = None

    @property
    def posts(self):
        return [body for method, _, body in self.calls if method == "POST"]

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            if self.before_post:
                self.before_post(body)
            cid = body["client_order_id"]
            if cid in self.remote:
                raise am.AlpacaHTTPError(422, "client_order_id must be unique")
            self.remote[cid] = {**body, "id": "remote-" + cid, "status": "accepted", "filled_qty": "0"}
            if self.immediate_fill:
                self.fill(cid, self.immediate_fill)
            if self.post_error:
                raise self.post_error
            return self.remote[cid].copy()
        if method == "GET":
            if self.get_error:
                raise self.get_error
            cid = path.split("=", 1)[1]
            if cid not in self.remote:
                raise am.AlpacaHTTPError(404, "order not found")
            return self.remote[cid].copy()
        if method == "DELETE":
            cid = path.rsplit("/", 1)[1].removeprefix("remote-")
            order = self.remote[cid]
            if self.cancel_completes:
                self.fill(cid, self.cancel_fill_qty if self.cancel_fill_qty is not None
                          else float(order["filled_qty"]), status="canceled")
            else:
                order["status"] = "pending_cancel"
            if self.cancel_error:
                raise self.cancel_error
            return {}  # 204 is not proof of a final state / final filled quantity
        raise AssertionError((method, path))

    def fill(self, cid, qty, status="filled", price=101):
        self.remote[cid].update(status=status, filled_qty=str(qty), filled_avg_price=str(price),
                                filled_at=NOW.isoformat())


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    monkeypatch.setattr(us_clock, "in_session", lambda *args: True)
    monkeypatch.setattr(am, "_default_http", lambda *args: pytest.fail("real broker HTTP is forbidden"))
    broker = Broker()
    m = am.AlpacaMirror(tmp_path, http=broker)
    m.start([])
    return m, broker


def opened(m, broker, *, side="short", qty=100):
    m.sync([event(1, side=side, qty=qty)])
    m.submit_pending()
    broker.fill("zt-1", qty)
    m.poll()


@pytest.mark.parametrize("side,exit_side", [("short", "buy"), ("buy", "sell")])
def test_tick_polls_open_before_considering_exit(mirror, side, exit_side):
    m, broker = mirror
    m.sync([event(1, side=side)])
    m.submit_pending()
    broker.fill("zt-1", 100)
    broker.calls.clear()
    m.tick([event(1, side=side), exit_event(side=side)])
    assert broker.calls[0][0] == "GET"
    assert broker.posts == [{"symbol": "XYZ", "qty": "100", "side": exit_side,
                             "type": "market", "time_in_force": "day", "client_order_id": "zt-2"}]
    broker.fill("zt-2", 100)
    m.tick([])
    assert m.positions() == {}


@pytest.mark.parametrize("status", ["partially_filled", "canceled", "expired", "rejected", "pending_cancel", "done_for_day"])
@pytest.mark.parametrize("side,sign", [("short", -1), ("buy", 1)])
def test_every_broker_state_records_cumulative_fill(mirror, status, side, sign):
    m, broker = mirror
    m.sync([event(1, side=side)])
    m.submit_pending()
    broker.fill("zt-1", 40, status=status, price=101)
    m.poll()
    assert m.positions() == {"XYZ": sign * 40}
    assert m.orders()[0]["filled_price"] == 101
    assert m.orders()[0]["deviation_bps"] == sign * 100


@pytest.mark.parametrize("side,sign", [("short", -1), ("buy", 1)])
def test_pending_exit_reserves_only_unfilled_quantity(mirror, side, sign):
    m, broker = mirror
    opened(m, broker, side=side)
    m.sync([exit_event(kind="partial_close", qty=50, side=side)])
    m.submit_pending()
    broker.fill("zt-2", 20, status="partially_filled")
    m.poll()
    # Two partial exits reserve only the first order's unfilled remainder.
    # A FINAL close instead settles/cancels earlier exits before sizing (see release edges).
    m.sync([exit_event(3, side=side, qty=50, kind='partial_close')])
    m.submit_pending()
    assert m.positions() == {"XYZ": sign * 80}
    assert broker.posts[-1]["qty"] == "50"
    assert len(broker.posts) == 3


@pytest.mark.parametrize("side", ["short", "buy"])
def test_terminal_partial_exit_releases_reservation_and_final_close_flattens_round(mirror, side):
    m, broker = mirror
    opened(m, broker, side=side)
    m.sync([exit_event(kind="partial_close", qty=50, side=side)])
    m.submit_pending()
    broker.fill("zt-2", 20, status="canceled")
    m.poll()
    m.sync([exit_event(3, qty=50, side=side)])
    m.submit_pending()
    assert broker.posts[-1]["qty"] == "80"  # close means flat, not the stale internal remainder
    broker.fill("zt-3", 80)
    m.poll()
    assert m.positions() == {}


def test_legacy_fill_sell_is_long_close_not_buy(mirror):
    m, broker = mirror
    opened(m, broker, side="buy", qty=10)
    m.sync([event(2, side="sell", qty=10)])
    m.submit_pending()
    assert m.orders()[-1]["intent"] == "close_long"
    assert broker.posts[-1]["side"] == "sell"
    broker.fill("zt-2", 10)
    m.poll()
    assert m.positions() == {}


@pytest.mark.parametrize("error", [TimeoutError("lost ACK"), am.AlpacaHTTPError(503, "upstream unavailable"),
                                   SystemExit("crash after broker acceptance")])
def test_post_attempt_is_durable_before_network_and_recovered_after_restart(mirror, tmp_path, monkeypatch, error):
    m, broker = mirror
    m.sync([event(1)])
    persisted = []
    broker.before_post = lambda body: persisted.append(json.loads(m.path.read_text())["orders"]["zt-1"])
    broker.post_error = error
    if isinstance(error, SystemExit):
        with pytest.raises(SystemExit):
            m.submit_pending()
    else:
        m.submit_pending()
    assert persisted[0].get("submit_attempted_at")
    assert persisted[0]["qty"] == 100
    broker.fill("zt-1", 100)
    broker.post_error = None
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(minutes=10))
    monkeypatch.setattr(us_clock, "in_session", lambda *args: False)
    m = am.AlpacaMirror(tmp_path, http=broker)
    m.tick([])
    assert m.positions() == {"XYZ": -100}
    assert len(broker.posts) == 1
    assert m.orders()[0]["status"] == "filled"


def test_unknown_submission_404_is_not_permission_to_resubmit_or_expire(mirror, tmp_path, monkeypatch):
    m, broker = mirror
    broker.post_error = TimeoutError("unknown result")
    m.sync([event(1)])
    m.submit_pending()
    broker.get_error = am.AlpacaHTTPError(404, "not visible yet")
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(minutes=10))
    for _ in range(2):
        m = am.AlpacaMirror(tmp_path, http=broker)
        m.tick([])
    assert len(broker.posts) == 1
    assert m.orders()[0]["status"] not in ("expired_out_of_session", "rejected")
    broker.get_error = None
    broker.fill("zt-1", 30, status="canceled")
    m.tick([])
    assert m.positions() == {"XYZ": -30}


@pytest.mark.parametrize("side", ["short", "buy"])
def test_cancel_and_reconcile_partial_open_before_exit(mirror, tmp_path, monkeypatch, side):
    m, broker = mirror
    m.sync([event(1, side=side)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled")
    broker.cancel_completes = False
    m.tick([exit_event(side=side)])
    assert len(broker.posts) == 1  # do not close 40 while another 60 can still fill
    assert any(method == "DELETE" for method, _, _ in broker.calls)
    broker.fill("zt-1", 70, status="pending_cancel")
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(minutes=8))
    m = am.AlpacaMirror(tmp_path, http=broker)
    m.tick([])
    assert len(broker.posts) == 1
    broker.fill("zt-1", 70, status="canceled")
    m.tick([])
    assert broker.posts[-1]["qty"] == "70"
    assert len(broker.posts) == 2
    broker.fill("zt-2", 70)
    m.tick([])
    m.tick([])
    assert m.positions() == {}
    assert len(broker.posts) == 2


@pytest.mark.parametrize("error", [TimeoutError("DELETE ACK lost"), am.AlpacaHTTPError(422, "already filled")])
def test_cancel_failure_still_reconciles_final_fill_before_cover(mirror, error):
    m, broker = mirror
    m.sync([event(1)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled")
    broker.cancel_fill_qty = 70
    broker.cancel_error = error
    m.tick([exit_event()])
    assert broker.posts[-1]["qty"] == "70"
    assert m.positions() == {"XYZ": -70}


def test_failed_cancel_and_failed_poll_never_use_stale_partial_as_final(mirror):
    m, broker = mirror
    m.sync([event(1)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled")
    m.poll()
    broker.cancel_completes = False
    broker.cancel_error = TimeoutError("DELETE")
    broker.get_error = TimeoutError("GET")
    m.tick([exit_event()])
    assert len(broker.posts) == 1
    broker.get_error = None
    broker.cancel_error = None
    broker.cancel_completes = True
    broker.cancel_fill_qty = 60
    m.tick([])
    assert len(broker.posts) == 2 and broker.posts[-1]["qty"] == "60"


def test_unsubmitted_open_is_suppressed_if_exit_already_known(mirror):
    m, broker = mirror
    m.tick([event(1), exit_event()])
    assert broker.posts == []
    assert m.positions() == {}


def test_exit_for_skipped_round_never_consumes_older_round_position(mirror):
    m, broker = mirror
    opened(m, broker)
    # An old round's failed close left a real exposure; a later round never opened.
    m.sync([exit_event(entry_seq=1)])
    m.submit_pending()
    broker.fill("zt-2", 0, status="rejected")
    m.poll()
    m.sync([event(3), exit_event(4, entry_seq=3)])
    m.submit_pending()
    assert len(broker.posts) == 2
    assert m.positions() == {"XYZ": -100}


def test_stale_explicit_exit_cannot_cover_new_round(mirror):
    m, broker = mirror
    opened(m, broker)
    m.sync([exit_event(entry_seq=1)])
    m.submit_pending()
    broker.fill("zt-2", 100)
    m.poll()
    m.sync([event(3)])
    m.submit_pending()
    broker.fill("zt-3", 100)
    m.poll()
    m.sync([exit_event(4, entry_seq=1)])
    m.submit_pending()
    assert len(broker.posts) == 3
    assert m.positions() == {"XYZ": -100}


def test_unknown_cover_reserves_quantity_and_does_not_duplicate_after_restart(mirror, tmp_path, monkeypatch):
    m, broker = mirror
    opened(m, broker)
    broker.post_error = TimeoutError("lost cover ACK")
    m.sync([exit_event(kind="partial_close", qty=80)])
    m.submit_pending()
    m = am.AlpacaMirror(tmp_path, http=broker)
    broker.get_error = TimeoutError("cannot reconcile yet")
    broker.post_error = None
    m.sync([exit_event(3, qty=20)])
    m.submit_pending()
    assert len(broker.posts) == 2  # unknown earlier cover must settle before final sizing
    assert 'zt-3' not in broker.remote
    broker.get_error = None
    broker.fill("zt-2", 80)
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(minutes=10))
    m.tick([])
    assert broker.posts[-1]['qty'] == '20'
    broker.fill("zt-3", 20)
    m.tick([])
    assert m.positions() == {}
    assert len(broker.posts) == 3


def test_immediate_post_fill_is_recorded_without_waiting_for_poll(mirror):
    m, broker = mirror
    broker.immediate_fill = 40
    m.sync([event(1, qty=40)])
    m.submit_pending()
    assert m.positions() == {"XYZ": -40}
    assert m.orders()[0]["filled_price"] == 101


@pytest.mark.parametrize("next_side", ["short", "buy"])
def test_new_round_waits_for_older_round_to_be_broker_flat(mirror, next_side):
    m, broker = mirror
    opened(m, broker)
    m.sync([exit_event(entry_seq=1)])
    m.submit_pending()
    m.sync([event(3, side=next_side)])
    m.submit_pending()
    assert len(broker.posts) == 2
    broker.fill("zt-2", 100)
    m.tick([])
    assert len(broker.posts) == 3
    assert broker.posts[-1]["side"] == ("sell" if next_side == "short" else "buy")


@pytest.mark.parametrize("bad_qty", [None, "NaN", "-1", "Infinity", "20"])
def test_terminal_response_needs_valid_final_cumulative_quantity(mirror, bad_qty):
    m, broker = mirror
    m.sync([event(1)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled")
    m.poll()
    broker.fill("zt-1", 70, status="canceled")
    broker.remote["zt-1"]["filled_qty"] = bad_qty
    broker.cancel_completes = False
    m.tick([exit_event()])
    assert len(broker.posts) == 1
    assert m.positions() == {"XYZ": -40}
    broker.fill("zt-1", 70, status="canceled")
    m.tick([])
    assert broker.posts[-1]["qty"] == "70"


def test_disk_failure_before_post_does_not_send(mirror, monkeypatch):
    m, broker = mirror
    m.sync([event(1)])
    monkeypatch.setattr(m, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        m.submit_pending()
    assert broker.posts == []


def test_restart_after_post_result_cannot_be_saved_recovers_once(mirror, tmp_path, monkeypatch):
    m, broker = mirror
    m.sync([event(1)])
    original_save = m._save
    calls = 0

    def failing_save():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("cannot persist POST response")
        original_save()

    monkeypatch.setattr(m, "_save", failing_save)
    with pytest.raises(OSError):
        m.submit_pending()
    broker.fill("zt-1", 100)
    m = am.AlpacaMirror(tmp_path, http=broker)
    m.tick([])
    assert m.positions() == {"XYZ": -100}
    assert len(broker.posts) == 1


def test_lower_fill_snapshot_does_not_erase_known_execution(mirror):
    m, broker = mirror
    m.sync([event(1)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled", price=102)
    m.poll()
    broker.fill("zt-1", 20, status="partially_filled", price=101)
    m.poll()
    assert m.positions() == {"XYZ": -40}
    assert m.orders()[0]["filled_price"] == 102


def test_exit_waiting_for_cancel_survives_kill_switch_and_session_boundary(mirror, tmp_path, monkeypatch):
    m, broker = mirror
    m.sync([event(1)])
    m.submit_pending()
    broker.fill("zt-1", 40, status="partially_filled")
    broker.cancel_completes = False
    m.tick([exit_event()])
    m.set_enabled(False)
    broker.fill("zt-1", 70, status="canceled")
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(hours=8))
    monkeypatch.setattr(us_clock, "in_session", lambda *args: False)
    m = am.AlpacaMirror(tmp_path, http=broker)
    m.tick([])
    assert len(broker.posts) == 1
    m.set_enabled(True)
    m.tick([])
    assert len(broker.posts) == 1
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(days=1))
    monkeypatch.setattr(us_clock, "in_session", lambda *args: True)
    m.tick([])
    assert len(broker.posts) == 2 and broker.posts[-1]["qty"] == "70"


def test_exit_waits_across_daily_cap_instead_of_becoming_terminal(mirror, monkeypatch):
    m, broker = mirror
    opened(m, broker)
    m.max_orders_per_day = 1
    m.sync([exit_event()])
    m.submit_pending()
    assert len(broker.posts) == 1
    monkeypatch.setattr(us_clock, "now", lambda: NOW + timedelta(days=1))
    m.tick([])
    assert len(broker.posts) == 2
