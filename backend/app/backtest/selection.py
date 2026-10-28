"""Deterministic, point-in-time economic ranking (never ticker/input order)."""
from itertools import groupby

import numpy as np

SELECTION_POLICY = {
    "id": "score_adv20_prior_v1",
    "keys": ["score_desc", "prior_20_session_mean_close_times_volume_desc", "prior_session_dollar_volume_desc"],
    "as_of": "strictly_before_entry_session",
    "missing_liquidity": "rank_last",
    "exact_cutoff_tie": "skip_entire_tied_group",
}


def prior_liquidity(close: np.ndarray, volume: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """20 observed market sessions, excluding the current session for every fill mode.

    Missing/nonfinite prices or volumes are not fabricated as zero-volume observations.
    Using previous sessions also makes intraday entry overrides safe from daily lookahead.
    """
    dollars = np.asarray(close, dtype=np.float64) * np.asarray(volume, dtype=np.float64)
    valid = np.isfinite(dollars) & (close > 0) & (volume >= 0)
    values = np.where(valid, dollars, 0.0)
    sums = np.vstack([np.zeros(close.shape[1]), np.cumsum(values, axis=0)])
    counts = np.vstack([np.zeros(close.shape[1]), np.cumsum(valid, axis=0)])
    stop = np.arange(close.shape[0])
    start = np.maximum(stop - 20, 0)
    n = counts[stop] - counts[start]
    adv = np.divide(sums[stop] - sums[start], n, out=np.full(close.shape, -1.0), where=n > 0)
    last = np.full(close.shape, -1.0)
    last[1:] = np.where(valid[:-1], dollars[:-1], -1.0)
    return adv, last


def select_candidates(candidates: list, slots: int, key, execution: dict) -> list:
    """Do not arbitrarily pick members of an economically indistinguishable cutoff group."""
    if slots <= 0:
        execution["buy_no_slot"] = execution.get("buy_no_slot", 0) + len(candidates)
        return []
    selected = []
    for _, group in groupby(sorted(candidates, key=key, reverse=True), key=key):
        group = list(group)
        if len(selected) + len(group) > slots:
            execution["buy_unresolved_tie"] = execution.get("buy_unresolved_tie", 0) + len(group)
            # Do not replace a tied higher-ranked group with lower-quality candidates.
            break
        selected.extend(group)
        if len(selected) == slots:
            break
    execution["buy_no_slot"] = execution.get("buy_no_slot", 0) + max(0, len(candidates) - len(selected))
    return selected
