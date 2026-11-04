"""DSL 连写区间与非整数幂 (11-05): 修复前两类都错。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.factors.dsl import FACTOR_COLUMN, compile_formula


def _panel(closes: list[float]) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame({
        "symbol": ["A"] * n,
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": [10.0] * n, "high": [30.0] * n, "low": [-10.0] * n,
        "close": closes, "volume": [1000.0] * n, "amount": [1e4] * n,
    })


def _values(formula: str, closes: list[float]) -> list:
    compiled = compile_formula(formula)
    assert compiled.ok, (formula, [e.code for e in compiled.errors])
    out = compiled.frame_transform(_panel(closes))
    assert out is not None
    return out[FACTOR_COLUMN].to_list()


# ── 连写比较 = 区间 (a < b < c 即 a < b and b < c) ─────────────────────

@pytest.mark.parametrize(("formula", "expected"), [
    ("10 < close < 20", [0.0, 1.0, 0.0, 0.0, 0.0]),
    ("10 <= close <= 20", [0.0, 1.0, 1.0, 1.0, 0.0]),
    ("20 > close > 10", [0.0, 1.0, 0.0, 0.0, 0.0]),
    ("0 < close < open < 25", [1.0, 0.0, 0.0, 0.0, 0.0]),
])
def test_chained_comparison_is_a_range(formula, expected):
    # close: 5 (区间下方) / 15 (区间内) / 20 / 10 (边界) / -4 (负数)
    assert _values(formula, [5.0, 15.0, 20.0, 10.0, -4.0]) == expected


def test_chained_comparison_works_inside_logic_and_if_else():
    assert _values("(10 < close < 20) and (volume > 1)", [5.0, 15.0]) == [0.0, 1.0]
    assert _values("if_else(10 < close < 20, close, 0)", [5.0, 15.0]) == [0.0, 15.0]


def test_single_comparison_unchanged():
    assert _values("close > 10", [5.0, 15.0]) == [0.0, 1.0]


# ── 非整数幂: 底数为负时结果为空, 不是 NaN ────────────────────────────

def test_fractional_power_of_negative_is_null():
    vals = _values("power(close, 0.5)", [4.0, -4.0, 0.0])
    assert vals[0] == pytest.approx(2.0)
    assert vals[1] is None
    assert vals[2] == 0.0


def test_integer_power_of_negative_still_computes():
    assert _values("power(close, 2)", [-3.0, 2.0]) == [9.0, 4.0]
    assert _values("power(close, -1)", [-2.0, 4.0]) == [-0.5, 0.25]


def test_fractional_negative_exponent_of_zero_is_null():
    # 0 的负指数是除零 (inf), 同样按无效值置空
    vals = _values("power(close, -0.5)", [4.0, 0.0])
    assert vals[0] == pytest.approx(0.5)
    assert vals[1] is None
