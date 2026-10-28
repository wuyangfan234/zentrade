"""Fail closed before publishing incomplete or non-finite adjustment events."""
from datetime import date, datetime

import polars as pl
import pytest

from app.plugins.alpaca import provider as alp
from app.services.adjustment_events import events_for_symbols
from app.services.kline_sync import store_adj_factor_events, adj_factor_basis


def bar(day, close, volume):
    return {'t': f'{day}T04:00:00Z', 'c': close, 'v': volume}


def test_one_failed_adjustment_chunk_aborts_the_whole_fetch(monkeypatch):
    monkeypatch.setattr(alp, '_load_credentials', lambda: ('test', 'test'))
    monkeypatch.setattr(alp, '_BATCH', 1)
    def source(key, secret, symbols, timeframe, start, end, adjustment, feed, *args):
        if symbols == ['B'] and adjustment == 'all':
            raise RuntimeError('injected missing adjusted batch')
        scale = 2 if adjustment == 'raw' else 1
        yield symbols[0], bar('2024-06-07', 50 * scale, 1000 / scale)
        yield symbols[0], bar('2024-06-10', 50, 1000)
    monkeypatch.setattr(alp, '_iter_bars', source)
    with pytest.raises(RuntimeError, match='injected'):
        alp.AlpacaProvider().get_adj_factors(['A', 'B'], datetime(2024, 6, 1), datetime(2024, 6, 11))


def test_unpaired_days_are_rejected_not_relabelled_as_a_later_action():
    raw = [('A', bar('2024-06-07', 100, 1000)), ('A', bar('2024-06-10', 50, 2000)), ('A', bar('2024-06-11', 52, 2200))]
    adjusted = [('A', bar('2024-06-07', 50, 2000)), ('A', bar('2024-06-11', 52, 2200))]
    with pytest.raises(ValueError, match='coverage'):
        events_for_symbols(raw, adjusted)


@pytest.mark.parametrize('bad', [float('inf'), float('nan'), -1.0])
def test_bad_volume_cannot_create_an_event(bad):
    raw = [('A', bar('2024-06-07', 100, 1000)), ('A', bar('2024-06-10', 100, 1000))]
    adjusted = [('A', bar('2024-06-07', 100, bad)), ('A', bar('2024-06-10', 100, 1000))]
    with pytest.raises(ValueError, match='volume'):
        events_for_symbols(raw, adjusted)


@pytest.mark.parametrize('column', ['ex_factor', 'vol_factor'])
@pytest.mark.parametrize('bad', [0.0, -1.0, float('inf'), float('nan')])
def test_invalid_factor_never_overwrites_the_last_valid_table(tmp_path, column, bad):
    directory = tmp_path / 'adj_factor'
    good = {'symbol': 'A', 'trade_date': date(2024, 6, 10), 'ex_factor': 2.0, 'vol_factor': 2.0}
    store_adj_factor_events(directory, pl.DataFrame([good]))
    before = (directory / 'all.parquet').read_bytes()
    with pytest.raises(ValueError, match='factor'):
        store_adj_factor_events(directory, pl.DataFrame([{**good, column: bad}]))
    assert (directory / 'all.parquet').read_bytes() == before
    assert adj_factor_basis(directory) == 'event_v2'
