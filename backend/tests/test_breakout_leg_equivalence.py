"""Vectorized leg search must preserve the original nested-loop semantics."""
import numpy as np
import pytest

from app.opportunity.setups.breakout import _strongest_leg


def _reference(closes, first, last, leg_max_days):
    best = (-1, -1, 0.0)
    for i in range(max(first, 0), last):
        for j in range(i + 1, min(i + leg_max_days, len(closes))):
            gain = (closes[j] - closes[i]) / closes[i] * 100 if closes[i] > 0 else 0
            if gain > best[2]:
                best = (i, j, gain)
    return best


@pytest.mark.parametrize('values,days', [
    ([10.0, 12.0, 11.0, 15.0], 3),
    ([10.0, 20.0, 10.0, 20.0], 3),
    ([5.0, 5.0, 5.0, 5.0], 30),
    ([0.0, 10.0, 20.0], 30),
    ([10.0, np.nan, 12.0, 15.0], 30),
    ([10.0, 12.0, np.nan, 15.0], 2),
    ([10.0, 12.0, 15.0], 1),
    ([10.0, 12.0, 15.0], 0),
])
def test_vectorized_leg_matches_nested_loop_for_boundaries(values, days):
    closes = np.array(values)
    expected = _reference(closes, 0, len(closes) - 1, days)
    actual = _strongest_leg(closes, 0, len(closes) - 1, days)
    assert actual[:2] == expected[:2]
    assert actual[2] == pytest.approx(expected[2])


def test_vectorized_leg_matches_seeded_price_paths():
    rng = np.random.default_rng(7)
    for t in range(200):
        n = int(rng.integers(2, 260))
        closes = np.cumprod(1 + rng.normal(0, 0.03, n)) * rng.uniform(1, 200)
        if t % 7 == 0:
            closes = np.round(closes, 0)
        if t % 11 == 0:
            closes[int(rng.integers(0, n))] = 0.0
        first, last = n - min(60, n - 1) - 1, n - 1
        expected = _reference(closes, first, last, 30)
        actual = _strongest_leg(closes, first, last, 30)
        assert actual[:2] == expected[:2]
        assert actual[2] == pytest.approx(expected[2], abs=1e-10)
