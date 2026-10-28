"""Short candidates must not be evaluated or mixed as long news evidence."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl

from app.opportunity.card import OpportunityCard
from app.services import decision_scan_job, news_shadow as ns

DAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)


def short_candidate():
    return dict(symbol='XYZ', side='short', entry_zone=(99., 100.), invalidation=104.,
                target_zone=(88., 89.76), risk_per_share=4.5,
                sources=['phantom_4h_short'], opened=False)


def test_short_shadow_snapshot_keeps_direction_and_pays_off_as_short(tmp_path):
    assert ns.record_snapshot(tmp_path, DAY, [short_candidate()], now=NOW) == 1
    frame = pl.DataFrame([dict(symbol='XYZ', date=DAY + timedelta(days=i + 1),
                              open=px, high=px + .2, low=px - .2, close=px)
                          for i, px in enumerate([99.5, 95., 89., 88., 88., 88., 88., 88., 88., 88., 88.])])
    repo = SimpleNamespace(get_daily_batch=lambda *a, **k: frame)
    ns.resolve_outcomes(tmp_path, repo, today=DAY + timedelta(days=16))
    row = ns.load_rows(tmp_path)[0]
    assert row.get('side') == 'short'
    assert row['outcome']['status'] == 'closed'
    assert row['outcome']['r'] > 0


def test_short_news_rows_cannot_inflate_long_bearish_veto_evidence(tmp_path):
    ns._save_rows(tmp_path, [dict(as_of='2026-10-02', symbol='XYZ', side='short', high_bearish=True,
                                  high_bullish=False, outcome={'status': 'closed', 'r': -1.})])
    ev = ns.evidence(tmp_path)
    assert ev['snapshots'] == 1
    assert ev['bearish_veto']['n_with'] == 0
    assert ev['bullish_boost']['n_without'] == 0


def test_desk_news_adapter_forwards_card_direction(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(ns, 'record_snapshot', lambda d, day, rows: captured.extend(rows) or len(rows))
    card = OpportunityCard(id='s', symbol='XYZ', setup_type='strategy:phantom_4h_short', created_date=DAY,
                           entry_zone=(99, 100), invalidation=104, target_zone=(88, 89.76),
                           risk_per_share=4.5, reward_risk=2.3, side='short')
    out = SimpleNamespace(result=SimpleNamespace(cards=[], watch=[card]), summary={})
    decision_scan_job._news_shadow_snapshot(tmp_path, DAY, out)
    assert captured[0].get('side') == 'short'
