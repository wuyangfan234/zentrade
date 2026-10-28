"""Static reference regressions; not a general Python data-flow sandbox."""

import pytest

from app.strategy.ai_generator import AIStrategyGenerator


def strategy(body, *, imports="import polars as pl", signature="df, params", declarations=""):
    return f'''{imports}
META = {{"id": "ast_edges", "name": "AST edges", "params": [], "scoring": {{}}}}
{declarations}
def filter({signature}):
{body}
'''


def assert_retired(code, field):
    result = AIStrategyGenerator().validate_code(code)
    assert not result["valid"]
    assert field in result["error"]
    assert "已停用或缺数据" in result["error"]


@pytest.mark.parametrize("field", ["turnover_rate", "deviate_3d"])
def test_imported_column_alias_is_a_reference(field):
    assert_retired(strategy(f"    return c({field!r}) > 0",
                           imports="from polars import col as c"), field)


@pytest.mark.parametrize("signature", [
    "df, params, field='turnover_rate'",
    "df, params, *, field='turnover_rate'",
    "df, params, field='turnover_rate', /",
])
def test_function_default_binds_a_referenced_field(signature):
    assert_retired(strategy("    return pl.col(field) > 0", signature=signature),
                   "turnover_rate")


@pytest.mark.parametrize("name", ["ENTRY_SIGNALS", "EXIT_SIGNALS"])
@pytest.mark.parametrize("mutation", [
    "{name} += ['signal_deviate_3d']",
    "{name}.append('signal_deviate_3d')",
    "{name}.extend(['signal_deviate_3d'])",
])
def test_mutated_signal_declarations_are_references(name, mutation):
    declarations = f"{name} = []\n" + mutation.format(name=name)
    assert_retired(strategy("    return pl.col('close') > 0", declarations=declarations),
                   "signal_deviate_3d")


@pytest.mark.parametrize("imports, literal", [
    ("import polars as pl", "pl.lit"),
    ("import polars as pl\nfrom polars import lit as literal", "literal"),
    ("import polars as pl\nimport polars as p", "p.lit"),
])
@pytest.mark.parametrize("text", ["deviate_3d", "turnover_rate", "signal_deviate_3d"])
def test_select_literal_text_is_not_a_column_reference(imports, literal, text):
    code = strategy(f"    label = df.select({literal}({text!r})).item()\n"
                    "    return pl.col('close') > 0", imports=imports)
    result = AIStrategyGenerator().validate_code(code)
    assert result["valid"], result["error"]


@pytest.mark.parametrize("expression", [
    "df.select(pl.lit('deviate_3d'), pl.col('turnover_rate'))",
    "df.select(pl.lit(pl.col('turnover_rate')))",
])
def test_literal_pruning_does_not_hide_real_column_calls(expression):
    assert_retired(strategy(f"    label = {expression}\n    return pl.col('close') > 0"),
                   "turnover_rate")


def test_unused_default_and_local_list_are_not_field_references():
    result = AIStrategyGenerator().validate_code(strategy(
        "    labels = []\n    labels.append('signal_deviate_3d')\n"
        "    return pl.col('close') > 0",
        signature="df, params, label='turnover_rate'",
    ))
    assert result["valid"], result["error"]
