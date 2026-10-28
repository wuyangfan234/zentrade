"""Unknown post-execution billing must never be booked as zero."""
import socket

import pytest

from app.plugins.qveris import provider as qp
from app.services import qveris_gate as qg


def test_real_response_read_timeout_keeps_pending_and_blocks_second_post(tmp_path, monkeypatch):
    calls = []

    class TimeoutResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            raise socket.timeout('response body unknown')

    def urlopen(request, **kwargs):
        calls.append(request.method)
        return TimeoutResponse()

    monkeypatch.setattr(qp, '_api_key', lambda: 'offline-fixture')
    monkeypatch.setattr(qp.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(qg, 'daily_limit', lambda: 1.)
    with pytest.raises(qg.GateError):
        qg.fetch(tmp_path, 'options_chain', 'AAA', {}, fresh=lambda _: False)
    assert calls == ['POST']
    pending = tmp_path / 'qveris/ledger.pending'
    assert pending.exists()
    ledger = tmp_path / 'qveris/ledger.jsonl'
    assert not ledger.exists() or not ledger.read_text().strip()
    # Simulate clearing all in-process fault latches: durable intent is authoritative.
    monkeypatch.setattr(qg, '_ledger_faults', {})
    with pytest.raises(qg.GateError):
        qg.fetch(tmp_path, 'options_chain', 'BBB', {}, fresh=lambda _: False)
    assert calls == ['POST'] and pending.exists()
    assert qg.usage(tmp_path)['spent_today'] is None


def test_explicit_zero_charge_failure_is_resolved_not_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(qg, 'daily_limit', lambda: 1.)

    def explicit_zero(*args):
        raise qp.QverisError('known failure', status=400, charged=0.)

    with pytest.raises(qp.QverisError):
        qg.fetch(tmp_path, 'options_chain', 'AAA', {}, fresh=lambda _: False, execute=explicit_zero)
    assert qg.spent_today(tmp_path) == 0.
    assert not (tmp_path / 'qveris/ledger.pending').exists()
    qg.fetch(tmp_path, 'options_chain', 'BBB', {}, fresh=lambda _: False,
             execute=lambda *a: {'data': {}, 'charged': 1.})
    assert qg.spent_today(tmp_path) == 1.
