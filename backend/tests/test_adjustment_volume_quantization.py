"""Adjusted volume is an integer; quantization must not be read as a split."""
from datetime import date

import pytest

from app.services.adjustment_events import events_for_symbols, events_from_bars


def _bar(day: str, close: float, volume: float) -> tuple[str, dict]:
    return "ABTC", {"t": f"{day}T05:00:00Z", "c": close, "v": volume}


def test_live_abtc_integer_volume_quantization_is_not_a_split():
    """Alpaca SIP 2021-01-04..08 for ABTC (after a large cumulative reverse split):
    the adjusted volume is rounded to 19..514 shares, so all/raw volume ratios wobble by
    several percent while the price level is unchanged. No corporate action happened."""
    raw = [_bar("2021-01-04", 3.39, 905508), _bar("2021-01-05", 3.47, 591065),
           _bar("2021-01-06", 4.185, 15426458), _bar("2021-01-07", 4.93, 7937150),
           _bar("2021-01-08", 5.98, 10911519)]
    adjusted = [_bar("2021-01-04", 101700, 30), _bar("2021-01-05", 104100, 19),
                _bar("2021-01-06", 125550, 514), _bar("2021-01-07", 147900, 264),
                _bar("2021-01-08", 179400, 363)]
    assert events_for_symbols(raw, adjusted) == []


def test_price_only_split_never_invents_a_volume_event_but_real_split_keeps_volume_factor():
    # 2:1 split: price level halves, adjusted volume doubles on both sides of the boundary.
    raw = {date(2024, 6, 7): (100.0, 1000.0), date(2024, 6, 10): (50.0, 2000.0)}
    adj = {date(2024, 6, 7): (50.0, 2000.0), date(2024, 6, 10): (50.0, 2000.0)}
    (event,) = events_from_bars(raw, adj)
    assert event["ex_factor"] == pytest.approx(2.0)
    assert event["vol_factor"] == pytest.approx(2.0)


def test_volume_factor_without_price_event_is_ignored_even_when_large():
    """A volume-level jump without any price-level change cannot be a split/merge: price and
    volume adjustments always move together. Treat it as source noise, not an event."""
    raw = {date(2024, 6, 7): (10.0, 1000.0), date(2024, 6, 10): (10.0, 1000.0)}
    adj = {date(2024, 6, 7): (10.0, 1000.0), date(2024, 6, 10): (10.0, 1500.0)}
    assert events_from_bars(raw, adj) == []


def test_quantized_reverse_split_uses_the_less_noisy_price_ratio_for_volume():
    """1:20 reverse split on a thin stock: 790 raw shares adjust to 39.5 and round to 40, so the
    volume ratio says 0.0506. The price ratio (0.05) has the smaller rounding error here."""
    raw = {date(2024, 3, 5): (0.5, 790.0), date(2024, 3, 6): (10.0, 41.0)}
    adj = {date(2024, 3, 5): (10.0, 40.0), date(2024, 3, 6): (10.0, 41.0)}
    (event,) = events_from_bars(raw, adj)
    assert event["ex_factor"] == pytest.approx(0.05)
    assert event["vol_factor"] == pytest.approx(0.05)
