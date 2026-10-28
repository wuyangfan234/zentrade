"""Extra US math/retirement guards uncovered during full UI audit."""
import polars as pl
import pytest
from app.strategy.scoring import scoring_value_expr
from app.factors.registry import factor_columns_view
from app.api.factors import list_factors


def test_vwap_bias_uses_share_volume_not_hands():
    rows=pl.DataFrame({'close':[110.], 'volume':[100.], 'amount':[10000.]})
    assert rows.select(scoring_value_expr(rows.columns, 'vwap_bias').alias('bias'))['bias'][0]==pytest.approx(.1)


def test_active_factor_picker_has_no_chinese_limit_factors():
    assert not any('limit_up_count' in x['id'] for x in factor_columns_view())
    assert not any('limit_up_count' in x['id'] for x in list_factors(asset_type='stock')['factors'])
