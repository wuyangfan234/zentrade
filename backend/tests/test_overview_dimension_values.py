from __future__ import annotations

from app.services.market_overview_builder import _dimension_values


def test_overview_dimension_values_ignore_legacy_nan_group():
    assert _dimension_values("人工智能;nan;芯片;NULL") == ["人工智能", "芯片"]
