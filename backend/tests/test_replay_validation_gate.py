"""An explicitly quarantined dataset must not enter a strategy replay."""
import json

import pytest

from scripts.replay_setups_realistic import load_adjusted_daily


def test_quarantined_dataset_is_rejected_before_reading_bars(tmp_path):
    (tmp_path / 'VALIDATION_PENDING.json').write_text(json.dumps({
        'status': 'quarantined', 'reason': 'unvalidated adjustment events',
    }))
    with pytest.raises(ValueError, match='quarantined'):
        load_adjusted_daily(tmp_path)
