"""Review regressions: replay compatibility and reference-only authoring checks."""
import polars as pl
import pytest

from app.factors.registry import SHARE_CAPITAL_FACTOR_IDS, SHARE_CAPITAL_FIELDS
from app.strategy import custom_signals
from app.strategy.ai_generator import AIStrategyGenerator

DEVIATIONS = {"deviate_3d", "deviate_10d", "deviate_30d"}


def signal(condition):
    return {"id": "legacy", "name": "legacy", "kind": "entry", "conditions": [condition]}


def strategy_code(expression='pl.col("close") > pl.col("ma20")', **meta):
    metadata = {"id": "custom_review", "name": "Review", "params": [], "scoring": {}, **meta}
    return f'''import polars as pl
META = {metadata!r}
ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = []
def filter(df, params):
    return {expression}
'''


@pytest.mark.parametrize("field", sorted(SHARE_CAPITAL_FACTOR_IDS))
@pytest.mark.parametrize("prefix", ["field:", ""])
def test_historical_right_field_compiles_and_injects(field, prefix):
    saved = signal({"left": "vol_ratio_5d", "op": ">", "right": prefix + field})
    frame = pl.DataFrame({"vol_ratio_5d": [2.0], field: [1.0]})
    exprs = custom_signals.build_expressions([saved])
    assert "csg_legacy" in exprs
    assert custom_signals.inject(frame, exprs)["csg_legacy"].to_list() == [True]
    # The same saved definition is not a valid new/edited definition.
    with pytest.raises(ValueError):
        custom_signals.validate(saved)


@pytest.mark.parametrize("field", sorted(DEVIATIONS))
@pytest.mark.parametrize("side", ["left", "right"])
def test_deleted_deviation_never_compiles_even_with_stale_frame(field, side):
    saved = signal({"left": field if side == "left" else "close", "op": ">",
                    "right": "0" if side == "left" else f"field:{field}"})
    frame = pl.DataFrame({"close": [2.0], field: [1.0]})
    exprs = custom_signals.build_expressions([saved])
    assert "csg_legacy" not in exprs
    assert "csg_legacy" not in custom_signals.inject(frame, exprs).columns


@pytest.mark.parametrize("text", ["deviate_local", "turnover_rate", "deviate_3d",
                                  "signal_deviate_3d", "signal_limit_up"])
def test_metadata_display_text_is_not_a_field_reference(text):
    result = AIStrategyGenerator().validate_code(strategy_code(name=text, description=text))
    assert result["valid"], result["error"]


def test_local_name_is_not_a_retired_column_reference():
    code = strategy_code().replace('    return ', '    deviate_local = 1\n    return ')
    result = AIStrategyGenerator().validate_code(code)
    assert result["valid"], result["error"]


@pytest.mark.parametrize("deviation", sorted(DEVIATIONS))
@pytest.mark.parametrize("assignment", ["ENTRY_SIGNALS", "EXIT_SIGNALS"])
def test_retired_deviation_signal_declarations_are_rejected(deviation, assignment):
    code = strategy_code() + f'\n{assignment} = ["signal_{deviation}"]\n'
    result = AIStrategyGenerator().validate_code(code)
    assert not result["valid"]
    assert f"signal_{deviation}" in result["error"]


@pytest.mark.parametrize("field", sorted(DEVIATIONS | SHARE_CAPITAL_FIELDS))
@pytest.mark.parametrize("expression", ['pl.col("{field}") > 0', 'df["{field}"] > 0',
                                        'market.field("{field}") > 0', 'market.{field} > 0'])
def test_real_column_references_remain_rejected(field, expression):
    result = AIStrategyGenerator().validate_code(strategy_code(expression.format(field=field)))
    assert not result["valid"]
    assert field in result["error"]


@pytest.mark.parametrize("field", sorted(DEVIATIONS | SHARE_CAPITAL_FIELDS))
def test_scoring_keys_remain_rejected(field):
    result = AIStrategyGenerator().validate_code(strategy_code(scoring={field: 1.0}))
    assert not result["valid"]
    assert field in result["error"]


@pytest.mark.parametrize("field", ["turnover_rate", "deviate_3d", "signal_deviate_3d"])
def test_matrix_constructor_scoring_is_a_real_field_reference(field):
    code = f'''from app.strategy.builtin.factor_rank_research import FactorRankResearchMatrixStrategy
META = {{"id": "matrix_review", "name": "turnover_rate", "scoring": {{}}}}
EXECUTION_BACKEND = "matrix_native"
SCORING = {{"{field}": 1.0}}
MATRIX_STRATEGY = FactorRankResearchMatrixStrategy(SCORING, {{}})
'''
    result = AIStrategyGenerator().validate_code(code)
    assert not result["valid"]
    assert field in result["error"]


@pytest.mark.parametrize("code", ["import os", "eval('1')", "x.__globals__", "x['__builtins__']"])
def test_reference_checks_do_not_weaken_safety_checks(code):
    result = AIStrategyGenerator().validate_code(strategy_code() + '\n' + code)
    assert not result["valid"]
    assert "禁止" in result["error"]
