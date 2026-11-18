"""策略文档防漂移: 文档里的示例必须过现行校验并能装载; 语法标准里的清单必须与代码常量逐项一致。

docs/strategy-development.md  手动策略开发指南 (带示例)
docs/strategy-standard.md     策略语法标准 (带 <!-- std:名字 --> 标记的清单)
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest import matrix
from app.factors.registry import CAPITAL_FILTER_FIELDS, SHARE_CAPITAL_FIELDS
from app.strategy import engine as engine_mod
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

DOCS = Path(__file__).resolve().parents[2] / "docs"
GUIDE = DOCS / "strategy-development.md"
STANDARD = DOCS / "strategy-standard.md"

pytestmark = pytest.mark.skipif(not GUIDE.exists() or not STANDARD.exists(), reason="docs/ 不在这份代码树里")


def _examples(path: Path) -> list[str]:
    """文档里的完整策略示例: 带 META 的 python 代码块 (参数片段之类不算)。"""
    blocks = re.findall(r"```python\n(.*?)```", path.read_text(encoding="utf-8"), re.S)
    return [b for b in blocks if re.search(r"^META\s*=", b, re.M)]


def _doc_list(name: str) -> set[str]:
    text = STANDARD.read_text(encoding="utf-8")
    m = re.search(rf"<!-- std:{re.escape(name)} -->\n```text\n(.*?)```", text, re.S)
    assert m, f"语法标准里找不到清单 std:{name}"
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    assert lines == sorted(lines), f"std:{name} 请按字母序排列"
    assert len(lines) == len(set(lines)), f"std:{name} 有重复项"
    return set(lines)


def _local_set(func, name: str) -> set[str]:
    """函数体里 `name = {...}` (或 `{...} - 别的`) 的字符串集合字面量。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value.left if isinstance(node.value, ast.BinOp) else node.value
            assert isinstance(value, ast.Set), f"{name} 不再是集合字面量, 请同步改这条测试"
            return {ast.literal_eval(e) for e in value.elts}
    raise AssertionError(f"代码里找不到 {name}, 请同步改这条测试")


EXAMPLES = _examples(GUIDE) + _examples(STANDARD) if GUIDE.exists() and STANDARD.exists() else []


def test_guide_has_one_example_per_main_backend():
    backends = []
    for code in _examples(GUIDE):
        m = re.search(r'^EXECUTION_BACKEND\s*=\s*"(\w+)"', code, re.M)
        backends.append(m.group(1) if m else None)
    assert sorted(backends) == ["matrix_native", "polars_expr", "python_history_legacy"]


@pytest.mark.parametrize("code", EXAMPLES, ids=lambda c: re.search(r'"id":\s*"(\w+)"', c).group(1))
def test_doc_example_passes_validation_and_loads(code, tmp_path):
    result = AIStrategyGenerator.__new__(AIStrategyGenerator).validate_code(code)
    assert result["valid"], result["error"]
    sid = result["meta"]["id"]
    assert sid.startswith("custom_")
    (tmp_path / f"{sid}.py").write_text(code, encoding="utf-8")
    eng = StrategyEngine(strategy_dirs=[tmp_path])
    loaded = eng.get(sid)
    declared = re.search(r'^EXECUTION_BACKEND\s*=\s*"(\w+)"', code, re.M).group(1)
    assert loaded.execution_backend == declared
    scoring = result["meta"].get("scoring") or {}
    assert not scoring or abs(sum(scoring.values()) - 1.0) < 1e-6


def _market():
    """3 只票 80 根: AAA 第 60 根起放量上行 (此前横盘); CCC 前 30 根未上市。"""
    rows = []
    start = date(2024, 1, 1)
    for a, sym in enumerate(("AAA", "BBB", "CCC")):
        for t in range(80):
            if sym == "CCC" and t < 30:
                continue
            c, v = 20.0 + a, 1e6
            if sym == "AAA" and t >= 60:
                c += (t - 59) * 0.5
                v = 3e6 if t == 60 else 1e6
            rows.append({"symbol": sym, "name": sym, "date": start + timedelta(days=t), "open": c, "high": c * 1.01,
                         "low": c * 0.99, "close": c, "volume": v, "amount": c * v})
    return matrix.build_market_data_matrix(pl.DataFrame(rows), field_columns={"amount"})


def test_matrix_example_fires_on_breakout_day_only_and_keeps_contract(tmp_path):
    code = next(c for c in _examples(GUIDE) if "matrix_native" in c)
    (tmp_path / "custom_matrix_breakout.py").write_text(code, encoding="utf-8")
    strat = StrategyEngine(strategy_dirs=[tmp_path]).get("custom_matrix_breakout").matrix_strategy
    market = _market()
    sig = strat.compute_signals(market, {})
    matrix.validate_signal_matrix(sig, market.shape)                 # 形状 / 类型 / 只读 / 分数有限
    assert np.argwhere(sig.entry == 1).tolist() == [[60, 0]]          # 只在放量突破那一根; 之后量比不够
    assert strat.required_warmup_bars({"window": 50}) > 50
    assert strat.required_fields() <= {"open", "high", "low", "close", "volume"}
    # 突破线不含当日 (防未来数据): 若把当日收盘算进自己的 N 日最高, close > 最高 永远不成立, 上面就是 0 命中
    assert sig.score.dtype == np.float32 and float(sig.score[:10].sum()) == 0.0   # 预热区分数填 0


# ── 语法标准的清单 = 代码常量 ────────────────────────────────────────

def test_standard_allowed_imports_match_code():
    assert _doc_list("allowed-imports") == set(AIStrategyGenerator._ALLOWED_IMPORT_MODULES)


def test_standard_forbidden_lists_match_code():
    fn = AIStrategyGenerator._validate_safety.__func__
    assert _doc_list("forbidden-calls") == _local_set(fn, "forbidden_calls")
    assert _doc_list("forbidden-attrs") == _local_set(fn, "forbidden_dunder_attrs")
    assert _doc_list("forbidden-subscripts") == _local_set(fn, "forbidden_subscript_strs")


def test_standard_execution_backends_match_engine():
    src = inspect.getsource(engine_mod)
    m = re.search(r"valid_backends\s*=\s*(\{[^}]*\})", src)
    assert m, "engine.py 里找不到 valid_backends, 请同步改这条测试"
    assert _doc_list("execution-backends") == set(ast.literal_eval(m.group(1)))


def test_standard_capital_fields_match_registry():
    assert _doc_list("capital-filter-fields") == set(CAPITAL_FILTER_FIELDS)
    assert _doc_list("share-capital-fields") == set(SHARE_CAPITAL_FIELDS)


def test_standard_signal_matrix_dtypes_match_code():
    sig = matrix.make_signal_matrix((2, 2))
    actual = {f"{name} {getattr(sig, name).dtype}" for name in
              ("entry", "exit", "score", "entry_signal_code", "exit_signal_code")}
    assert _doc_list("signal-matrix-dtypes") == actual
    assert not any(getattr(sig, n).flags.writeable for n in ("entry", "exit", "score"))


@pytest.mark.parametrize("bad, needle", [
    ("import os\n", "禁止 import os"),
    ("x = open('a')\n", "禁止调用 open()"),
    ("y = ().__class__\n", "禁止访问属性 __class__"),
])
def test_standard_rules_are_actually_enforced(bad, needle):
    code = next(c for c in _examples(GUIDE) if "polars_expr" in c) + "\n" + bad
    result = AIStrategyGenerator.__new__(AIStrategyGenerator).validate_code(code)
    assert not result["valid"] and needle in result["error"]


def test_scoring_must_sum_to_one_as_the_standard_says():
    code = next(c for c in _examples(GUIDE) if "polars_expr" in c)
    broken = code.replace('"momentum_20d": 0.5', '"momentum_20d": 0.4')
    assert broken != code
    result = AIStrategyGenerator.__new__(AIStrategyGenerator).validate_code(broken)
    assert not result["valid"] and "权重总和必须为 1.0" in result["error"]
