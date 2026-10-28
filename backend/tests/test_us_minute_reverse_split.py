from datetime import date, datetime

import polars as pl
import pytest

from app.services.kline_sync import store_adj_factor_events
from app.services.minute_adjust import apply_minute_adjustment


@pytest.mark.parametrize('include_forward', [False, True])
def test_reverse_split_prices_do_not_depend_on_other_symbols(tmp_path, include_forward):
    factors = [{'symbol': 'REV', 'trade_date': date(2024, 6, 10), 'ex_factor': 0.1, 'vol_factor': 0.1}]
    rows = [{'symbol': 'REV', 'datetime': datetime(2024, 6, 7, 15, 59),
             'open': 3.65, 'high': 3.70, 'low': 3.60, 'close': 3.65,
             'volume': 1000.0, 'amount': 3650.0}]
    if include_forward:
        factors.append({'symbol': 'FWD', 'trade_date': date(2024, 6, 10), 'ex_factor': 10.0, 'vol_factor': 10.0})
        rows.append({**rows[0], 'symbol': 'FWD'})
    store_adj_factor_events(tmp_path / 'adj_factor', pl.DataFrame(factors))
    frame = pl.DataFrame(rows)
    adjusted = apply_minute_adjustment(frame, tmp_path).filter(pl.col('symbol') == 'REV')
    assert adjusted['close'][0] == pytest.approx(36.5)
    assert adjusted['amount'][0] / adjusted['volume'][0] == pytest.approx(36.5)
    assert adjusted['volume'][0] == 1000.0
    assert frame.filter(pl.col('symbol') == 'REV')['close'][0] == 3.65
