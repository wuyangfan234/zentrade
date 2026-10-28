"""Independent release-review counterexamples, promoted to permanent regression tests."""
import pytest
import test_entry_admission_guards as t
from test_mirror_closeout import Broker, NOW, exit_event, opened
from app.services import alpaca_mirror as am, entry_guard
from app.market_clock import us_clock


def test_exploration_quota_after_permission_downgrade(tmp_path, monkeypatch):
    clock = t.clock.__wrapped__(monkeypatch)
    out, _ = t.build(tmp_path, {t.SHORT: ['AAA', 'BBB']})
    loop = t.loop_for(tmp_path, out, clock, monkeypatch)
    t.tick(loop, clock, ('AAA', 'BBB'))
    assert len(t.intents(loop)) == 2
    t.save(tmp_path/'decision_desk/evaluation.json', {
        'sources': {t.SHORT: {'n': 20, 'mean': .3, 'ci_low': .1}}})
    for _ in range(4):
        t.tick(loop, clock, ('AAA', 'BBB'))
    assert len(t.opens(loop)) == 1
    fill = t.opens(loop)[0]
    assert fill['entry_tier'] == 'explore'
    assert entry_guard.exploration_claims(loop.ledger, loop.trader._cards.values(), clock[0]) == {fill['card_id']}


@pytest.mark.parametrize('side', ['short', 'buy'])
@pytest.mark.parametrize('delayed_cancel', [False, True])
def test_final_close_settles_prior_exit_before_flattening(tmp_path, monkeypatch, side, delayed_cancel):
    monkeypatch.setattr(us_clock, 'now', lambda: NOW)
    monkeypatch.setattr(us_clock, 'in_session', lambda *a: True)
    broker = Broker()
    mirror = am.AlpacaMirror(tmp_path, http=broker)
    mirror.start([])
    opened(mirror, broker, side=side)
    mirror.sync([exit_event(kind='partial_close', qty=50, side=side)])
    mirror.submit_pending()
    broker.fill('zt-2', 20, status='partially_filled')
    mirror.poll()
    broker.cancel_completes = not delayed_cancel
    mirror.sync([exit_event(3, qty=50, side=side)])
    mirror.submit_pending()
    if delayed_cancel:
        assert 'zt-3' not in broker.remote
        broker.fill('zt-2', 20, status='canceled')
        mirror = am.AlpacaMirror(tmp_path, http=broker)
        mirror.tick([])
    assert broker.posts[-1]['client_order_id'] == 'zt-3'
    assert broker.posts[-1]['qty'] == '80'
    broker.fill('zt-3', 80)
    mirror = am.AlpacaMirror(tmp_path, http=broker)
    for _ in range(3):
        mirror.tick([])
    assert mirror.positions() == {}
    assert len(broker.posts) == 3
