"""Report #11: query methods must share the mutation lock and detach order rows."""
import pytest
from app.services import alpaca_mirror as am


class OrdersChecked(dict):
    def values(self):
        assert am._lock._is_owned(), 'reader bypasses the broker-state lock'
        return super().values()


@pytest.mark.parametrize('method', ['orders', 'positions', 'summary'])
def test_read_methods_use_mutation_lock(tmp_path, method):
    mirror = am.AlpacaMirror(tmp_path, http=lambda *a: pytest.fail('network forbidden'))
    mirror._state['orders'] = OrdersChecked({'zt-1': {
        'cid': 'zt-1', 'seq': 1, 'symbol': 'X', 'side': 'sell',
        'status': 'filled', 'filled_qty': 3., 'deviation_bps': 1.}})
    getattr(mirror, method)()


def test_public_order_snapshot_does_not_alias_broker_state(tmp_path):
    mirror = am.AlpacaMirror(tmp_path, http=lambda *a: pytest.fail('network forbidden'))
    mirror._state['orders'] = {'zt-1': {'seq': 1, 'symbol': 'X', 'side': 'sell',
                                      'status': 'submitted', 'filled_qty': 0.}}
    snapshot = mirror.orders()
    with am._lock:
        mirror._state['orders']['zt-1'].update(status='filled', filled_qty=3.)
    assert snapshot[0]['status'] == 'submitted'
    snapshot[0]['symbol'] = 'MUTATED'
    assert mirror.orders()[0]['symbol'] == 'X'
