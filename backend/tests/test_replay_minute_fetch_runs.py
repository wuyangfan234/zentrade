"""Replay minute fetch must request only the needed day runs, not the whole multi-year span."""
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

import scripts.replay_setups_realistic as replay


class RecordingProvider:
    def __init__(self):
        self.calls = []

    def get_minute(self, symbols, start, end):
        self.calls.append((start.date(), end.date()))
        rows = []
        d = start.date()
        while d <= end.date():
            if d.weekday() < 5:
                rows.append({"symbol": symbols[0], "datetime": datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30),
                             "open": 10.0, "close": 10.0})
            d += timedelta(days=1)
        return pl.DataFrame(rows)


def test_fetch_splits_far_apart_needs_into_separate_requests(tmp_path):
    provider = RecordingProvider()
    source = replay.MinuteSource(provider, Path(tmp_path))
    need = {"SYM": {date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5), date(2024, 6, 3), date(2024, 6, 4)}}
    source.fetch_many(need)
    assert provider.calls == [(date(2022, 1, 3), date(2022, 1, 5)), (date(2024, 6, 3), date(2024, 6, 4))]
    for d in need["SYM"]:
        assert source.day("SYM", d).height == 1


def test_days_within_a_short_gap_share_one_request(tmp_path):
    provider = RecordingProvider()
    source = replay.MinuteSource(provider, Path(tmp_path))
    source.fetch_many({"SYM": {date(2024, 6, 3), date(2024, 6, 10)}})
    assert provider.calls == [(date(2024, 6, 3), date(2024, 6, 10))]
