"""Offline real build -> lifecycle -> intent -> fill admission regressions.
Only market/protection/notification I/O is stubbed, never source_tradable.
"""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account
from app.market_clock import us_clock
from app.opportunity.card import CardState
from app.opportunity.engine import ScanResult, load_params
from app.services import decision_desk as dd, decision_scan, decision_scan_job as job, shortable
from app.services.trading_loop import TradingLoop

BASE = date(2026, 10, 2)
SHORT, LONG = 'phantom_4h_short', 'phantom_4h'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 10, 2, 17, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, 'now', lambda: now[0])
    monkeypatch.setattr(us_clock, '_calendar_provider', None)
    monkeypatch.setattr(shortable, '_query', lambda s: {'shortable': True, 'easy_to_borrow': True})
    return now


def daily(symbol):
    dates = [BASE - timedelta(days=x) for x in reversed(range(220))]
    return pl.DataFrame({'symbol': [symbol]*220, 'date': dates, 'open': [100.]*220,
                         'high': [102.]*220, 'low': [98.]*220, 'close': [100.]*220, 'volume': [2e6]*220})


def build(data_dir, picks=None, statuses=None, *, borrow=lambda s: True, evaluation=None):
    picks = picks or {SHORT: ['XYZ']}
    registry = dd.SourceRegistry(data_dir)
    for src, state in (statuses or {s: 'enabled' for s in picks}).items():
        registry.set_status(src, state)
    if evaluation is None:
        evaluation = {'sources': {src: {'n': 100, 'mean': .3, 'ci_low': .1} for src in picks}}
    save(data_dir/'decision_desk/evaluation.json', evaluation)
    symbols = sorted({s for ss in picks.values() for s in ss})
    out = decision_scan.build(as_of=BASE, strategy_picks=picks,
        s1s2=ScanResult(as_of=BASE, gate='green', params_revision=3, scanned=len(symbols)),
        frames={s: daily(s) for s in symbols}, evaluation=evaluation, registry=registry,
        watchlist=set(), stop_losses={}, sector_map={s: 'Sector'+s for s in symbols},
        portfolio_risk_used_pct=0, active_symbols=set(), params=load_params(), shortable=borrow)
    save(data_dir/'decision_desk/runs/2026-10-02.json', {'summary': out.summary, 'ranked': out.ranked})
    return out, registry


def loop_for(data_dir, out, clock, monkeypatch):
    syms = [c.symbol for c in out.result.cards + out.result.watch]
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir),
        get_instruments=lambda: pl.DataFrame({'symbol': syms, 'sector': ['Sector'+s for s in syms]}),
        get_daily=lambda s, start, end, columns=None: pl.DataFrame({'date': [end], 'close': [100.], 'raw_close': [100.]}),
        get_daily_batch=lambda symbols, start, end: pl.concat([daily(s) for s in symbols]))
    loop = TradingLoop(repo, Account())
    monkeypatch.setattr(loop, '_enqueue_events', lambda: None)
    loop.boot()
    loop.opp_svc.save_scan(out.result)
    clock[0] = datetime(2026, 10, 5, 10, tzinfo=us_clock.tz)
    return loop


def quotes(clock, symbols=('XYZ',), price=99.5):
    at = clock[0]
    return {s: {'symbol': s, 'price': price, 'feed': 'tickflow', 'timestamp': at.isoformat(),
                'open': price, 'open_timestamp': at.replace(hour=9, minute=30, second=0).isoformat()} for s in symbols}


def tick(loop, clock, symbols=('XYZ',), price=99.5):
    loop.intraday_fill_check(quotes(clock, symbols, price))
    clock[0] += timedelta(seconds=15)


def opens(loop):
    return [e for e in loop.ledger.read_events() if e['kind'] == 'fill']


def intents(loop):
    return [e for e in loop.ledger.read_events() if e['kind'] == 'entry_intent' and e['status'] == 'pending']


@pytest.mark.parametrize('chosen', ['long', 'short'])
def test_conflict_is_structured_and_never_promotes_or_enters(tmp_path, clock, monkeypatch, chosen):
    src, other = (LONG, SHORT) if chosen == 'long' else (SHORT, LONG)
    out, _ = build(tmp_path, {src: ['XYZ'], other: ['XYZ']}, {src: 'enabled', other: 'observe'})
    card = out.result.watch[0]
    dec = card.evidence['decision']
    assert dec.get('direction_conflict') is True
    assert dec['sides'] == ['long', 'short'] and 'direction_conflict' in dec['hard_blockers']
    assert dec['resonance'] <= 1
    assert out.ranked[0]['score'] == out.weights[src]['weight']
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    for _ in range(2):
        tick(loop, clock, price=sum(card.entry_zone)/2)
    assert loop.opp_svc.all_cards()[0].state == CardState.WATCH
    assert opens(loop) == intents(loop) == []


@pytest.mark.parametrize('change', ['observe', 'disabled', 'negative', 'cap0', 'corrupt', 'missing_source'])
def test_current_permission_overrides_run_and_pending_intent(tmp_path, clock, monkeypatch, change):
    out, reg = build(tmp_path)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock)
    assert len(intents(loop)) == 1
    if change in ('observe', 'disabled'):
        reg.set_status(SHORT, change)
    elif change == 'cap0':
        reg.set_cap(SHORT, 0)
    elif change == 'negative':
        save(tmp_path/'decision_desk/evaluation.json', {'sources': {SHORT: {'n': 100, 'mean': -.3, 'ci_low': -.5}}})
    elif change == 'missing_source':
        save(tmp_path/'decision_desk/evaluation.json', {'sources': {}})
    else:
        (tmp_path/'decision_desk/evaluation.json').write_text('{')
    assert job.source_tradable(tmp_path, SHORT) is False
    tick(loop, clock)
    assert opens(loop) == []
    assert loop.opp_svc.all_cards()[0].state == CardState.WATCH
    assert any('source_not_tradable' in b['reason'] for b in loop.entry_blockers)


@pytest.mark.parametrize('status', ['observe', 'enabled'])
def test_negative_card_stays_watch_even_with_empty_run_and_corrupt_evidence(tmp_path, clock, monkeypatch, status):
    out, _ = build(tmp_path, statuses={SHORT: status},
        evaluation={'sources': {SHORT: {'n': 100, 'mean': -.3, 'ci_low': -.6}}})
    save(tmp_path/'decision_desk/runs/2026-10-05.json', {'summary': {'weights': {}}})
    (tmp_path/'decision_desk/evaluation.json').write_text('{')
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    for _ in range(2):
        tick(loop, clock)
    assert opens(loop) == intents(loop) == []
    assert loop.opp_svc.all_cards()[0].state == CardState.WATCH


@pytest.mark.parametrize('risk', [0, -1, float('nan'), None, True])
def test_frozen_zero_or_invalid_risk_never_defaults_to_account_risk(tmp_path, clock, monkeypatch, risk):
    out, _ = build(tmp_path)
    c = out.result.cards[0]
    out.result.cards = [replace(c, evidence={**c.evidence, 'decision': {**c.evidence['decision'], 'risk_pct': risk}})]
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    for _ in range(2):
        tick(loop, clock)
    assert opens(loop) == intents(loop) == []
    assert loop.opp_svc.all_cards()[0].state == CardState.WATCH


def test_unborrowable_never_enters_but_later_fresh_permission_can_promote(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, borrow=lambda s: False)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    monkeypatch.setattr(shortable, '_query', lambda s: {'shortable': False, 'easy_to_borrow': False})
    for _ in range(2):
        tick(loop, clock)
    assert opens(loop) == intents(loop) == []
    assert loop.opp_svc.all_cards()[0].state == CardState.WATCH
    monkeypatch.setattr(shortable, '_query', lambda s: {'shortable': True, 'easy_to_borrow': True})
    clock[0] += timedelta(minutes=2)
    tick(loop, clock)
    tick(loop, clock)
    assert len(opens(loop)) == 1


def test_borrow_revoked_between_intent_and_fill(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock)
    assert len(intents(loop)) == 1
    monkeypatch.setattr(shortable, '_query', lambda s: {'shortable': False, 'easy_to_borrow': False})
    clock[0] += timedelta(minutes=2)
    tick(loop, clock)
    assert opens(loop) == []
    assert any('borrow' in b['reason'] for b in loop.entry_blockers)


@pytest.mark.parametrize('value', ['false', 1, None, [], {}])
def test_borrow_provider_requires_actual_booleans(tmp_path, clock, value):
    check = shortable.checker(tmp_path, query=lambda s: {'shortable': value, 'easy_to_borrow': True})
    assert check('XYZ') is False


def test_borrow_cache_is_timestamped_short_lived_and_failure_never_uses_stale_true(tmp_path, clock):
    calls = []
    def query(s):
        calls.append(s)
        if len(calls) > 1:
            raise ConnectionError('offline')
        return {'shortable': True, 'easy_to_borrow': True}
    check = shortable.checker(tmp_path, query=query)
    assert check('XYZ') is True
    saved = json.loads((tmp_path/'decision_desk/shortable.json').read_text())['symbols']['XYZ']
    assert saved.get('checked_at') == clock[0].isoformat()
    clock[0] += timedelta(minutes=2)
    assert check('XYZ') is False
    assert shortable.checker(tmp_path, query=query)('XYZ') is False


def test_explore_quota_counts_pending_and_actual_trading_day_across_restart(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB']}, evaluation={'sources': {SHORT: {'n': 0}}})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock, ('AAA', 'BBB'))
    assert len(intents(loop)) == 1
    # Same frame/restart cannot create a second reservation.
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    clock[0] += timedelta(seconds=30)
    tick(loop, clock, ('AAA', 'BBB'))
    tick(loop, clock, ('AAA', 'BBB'))
    assert [e['symbol'] for e in opens(loop)] == ['AAA']
    assert next(c for c in loop.opp_svc.all_cards() if c.symbol == 'BBB').state == CardState.WATCH
    clock[0] = datetime(2026, 10, 6, 10, tzinfo=us_clock.tz)
    tick(loop, clock, ('AAA', 'BBB'))
    tick(loop, clock, ('AAA', 'BBB'))
    assert [e['symbol'] for e in opens(loop)] == ['AAA', 'BBB']


def test_manual_disable_then_reenable_requires_new_intent(tmp_path, clock, monkeypatch):
    out, reg = build(tmp_path)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock)
    reg.set_status(SHORT, 'disabled')
    tick(loop, clock)
    reg.set_status(SHORT, 'enabled')
    tick(loop, clock)
    assert opens(loop) == []
    tick(loop, clock)
    assert len(opens(loop)) == 1


def test_explore_zero_cap_not_tradable(tmp_path, clock):
    _, reg = build(tmp_path, evaluation={'sources': {SHORT: {'n': 0}}})
    reg.set_cap(SHORT, 0)
    assert not reg.effective(SHORT, dd.source_weight({'n': 0}))['tradable']
    assert not job.source_tradable(tmp_path, SHORT)


@pytest.mark.parametrize('bad_quote', [None, 0, -1, float('nan'), float('inf'), 'stale'])
def test_any_missing_mark_when_short_held_blocks_new_entry_not_cover(tmp_path, clock, monkeypatch, bad_quote):
    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB'], LONG: ['ZZZ']})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    # Real initial short opening; later AAA cover remains independent of BBB valuation.
    tick(loop, clock, ('AAA',))
    tick(loop, clock, ('AAA',))
    loop.ledger.append('fill', {'symbol': 'HELD', 'side': 'buy', 'qty': 1, 'price': 100., 'fee': 1.,
        'trade_date': '2026-10-05', 'card_id': 'held'}, at=clock[0].isoformat())
    for _ in range(2):
        q = quotes(clock, ('AAA', 'BBB', 'ZZZ'), price=99.5)
        q['ZZZ']['price'] = q['ZZZ']['open'] = 100.5
        if bad_quote is not None:
            q['HELD'] = quotes(clock, ('HELD',))['HELD']
            if bad_quote == 'stale':
                q['HELD']['timestamp'] = (clock[0] - timedelta(minutes=3)).isoformat()
            else:
                q['HELD']['price'] = bad_quote
        loop.intraday_fill_check(q)
        clock[0] += timedelta(seconds=15)
    assert {e['symbol'] for e in opens(loop)} == {'AAA', 'HELD'}
    assert any(b['reason'] == 'valuation_incomplete' and 'HELD' in b['missing_symbols'] for b in loop.entry_blockers)
    # Existing AAA target cover works despite missing HELD quote and a failed borrow service.
    monkeypatch.setattr(shortable, '_query', lambda s: None)
    tick(loop, clock, ('AAA',), price=80.)
    assert loop.ledger.position('AAA') is None


def test_new_short_in_same_batch_makes_missing_other_holding_observable(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB']})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    loop.ledger.append('fill', {'symbol': 'HELD', 'side': 'buy', 'qty': 1, 'price': 100., 'fee': 1.,
        'trade_date': '2026-10-05', 'card_id': 'held'}, at=clock[0].isoformat())
    tick(loop, clock, ('AAA', 'BBB'))
    tick(loop, clock, ('AAA', 'BBB'))
    assert loop.ledger.position('BBB') is None
    assert any(b['reason'] == 'valuation_incomplete' for b in loop.entry_blockers)


def test_final_entry_receives_only_fresh_quote_marks(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    captured = []
    monkeypatch.setattr(loop.trader, 'try_fill_entry', lambda *a, **kw: captured.append(kw))
    tick(loop, clock)
    assert captured and captured[0].get('marks') == {'XYZ': 99.5}


def test_scheduler_prequote_phase_does_not_cancel_valid_short_entry_intent(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB']})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock, ('AAA',))
    tick(loop, clock, ('AAA',))
    assert loop.ledger.position('AAA') is not None
    provider = SimpleNamespace(name='iex', get_execution_quotes=lambda syms: list(quotes(clock, syms).values()))
    for _ in range(2):
        loop.run_intraday(provider)
        clock[0] += timedelta(seconds=15)
    assert loop.last_error is None
    assert {e['symbol'] for e in opens(loop)} == {'AAA', 'BBB'}


def test_fill_borrow_refresh_rejects_revocation_inside_cache_ttl(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path)
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock)
    monkeypatch.setattr(shortable, '_query', lambda s: {'shortable': False, 'easy_to_borrow': False})
    tick(loop, clock)
    assert opens(loop) == []
    assert any(b['reason'] == 'borrow_unavailable' for b in loop.entry_blockers)


def test_old_conflict_card_without_structured_flag_still_cannot_enter(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, {SHORT: ['XYZ'], LONG: ['XYZ']})
    c = out.result.watch[0]
    dec = {k: v for k, v in c.evidence['decision'].items() if k not in ('direction_conflict', 'sides', 'hard_blockers')}
    out.result.watch = [replace(c, evidence={**c.evidence, 'decision': dec})]
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    tick(loop, clock)
    tick(loop, clock)
    assert opens(loop) == intents(loop) == []


def test_forced_active_explore_cards_cannot_bypass_final_quota(tmp_path, clock, monkeypatch):
    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB']}, evaluation={'sources': {SHORT: {'n': 0}}})
    out.result.cards = [replace(c, state=CardState.ACTIVE) for c in out.result.cards + out.result.watch]
    out.result.watch = []
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    # Deliberately bypass ONLY lifecycle to test the independently enforced final guard.
    monkeypatch.setattr(loop.opp_svc, 'advance_lifecycle', lambda *a, **k: None)
    tick(loop, clock, ('AAA', 'BBB'))
    assert len(intents(loop)) == 1
    tick(loop, clock, ('AAA', 'BBB'))
    assert len(opens(loop)) == 1


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('downgrade', [False, True])
def test_execution_tier_changes_between_frames(tmp_path, clock, monkeypatch, restart, downgrade):
    from app.services.entry_guard import exploration_claims

    out, _ = build(tmp_path, {SHORT: ['AAA', 'BBB']})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    evidence_path = tmp_path/'decision_desk/evaluation.json'
    if not downgrade:
        save(evidence_path, {'sources': {SHORT: {'n': 0}}})
    tick(loop, clock, ('AAA',))
    assert intents(loop)[0]['proposal']['evidence']['decision']['entry_tier'] == ('proven' if downgrade else 'explore')
    save(evidence_path, {'sources': {SHORT: {'n': 0} if downgrade else {'n': 100, 'mean': .3, 'ci_low': .1}}})
    tick(loop, clock, ('AAA',))
    assert [e['symbol'] for e in opens(loop)] == ['AAA']
    if restart:
        now = clock[0]
        loop = loop_for(tmp_path, out, clock, monkeypatch)
        clock[0] = now
    # BBB now requests exploration regardless of AAA's executed tier.
    save(evidence_path, {'sources': {SHORT: {'n': 0}}})
    tick(loop, clock, ('AAA', 'BBB'))
    tick(loop, clock, ('AAA', 'BBB'))
    assert [e['symbol'] for e in opens(loop)] == (['AAA'] if downgrade else ['AAA', 'BBB'])
    assert opens(loop)[0].get('entry_tier') == ('explore' if downgrade else 'proven')
    assert exploration_claims(loop.ledger, [], clock[0]) == {opens(loop)[0 if downgrade else 1]['card_id']}


@pytest.mark.parametrize('frozen_only', [False, True])
def test_legacy_entry_without_guard_persists_fallback_tier(tmp_path, clock, monkeypatch, frozen_only):
    from app.services.entry_guard import exploration_claims

    out, _ = build(tmp_path, {LONG: ['AAA']}, evaluation={'sources': {LONG: {'n': 0}}})
    loop = loop_for(tmp_path, out, clock, monkeypatch)
    card = replace(out.result.cards[0], state=CardState.ACTIVE)
    price = sum(card.entry_zone)/2
    assert loop.trader.try_fill_entry(card, price=price, trade_date=clock[0].date(), at=clock[0].isoformat()) is None
    clock[0] += timedelta(seconds=15)
    current = replace(card, evidence={}) if frozen_only else card
    fill = loop.trader.try_fill_entry(current, price=price, trade_date=clock[0].date(), at=clock[0].isoformat())
    assert fill is not None
    assert fill.get('entry_tier') == 'explore'
    assert exploration_claims(loop.ledger, [], clock[0]) == {card.id}


@pytest.mark.parametrize('registry', [[], {'phantom_4h_short': []}, {'phantom_4h_short': 'enabled'}])
def test_malformed_registry_is_denied_without_throwing(tmp_path, clock, registry):
    build(tmp_path)
    save(tmp_path/'decision_desk/sources.json', registry)
    assert job.source_tradable(tmp_path, SHORT) is False
