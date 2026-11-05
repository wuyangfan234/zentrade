"""因子采集扩容与任意周期指标的 DSL 回归测试。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors.dsl import FACTOR_COLUMN, compile_formula


def _series(formula: str, closes: list[float]) -> pl.Series:
    frame = pl.DataFrame({"symbol": ["A"] * len(closes), "date": [date(2026, 1, i + 1) for i in range(len(closes))], "close": closes})
    compiled = compile_formula(formula)
    assert compiled.ok, [e.to_dict() for e in compiled.errors]
    out = compiled.frame_transform(frame)
    assert out is not None
    return out[FACTOR_COLUMN]


def test_scientific_number_literals_compile_and_keep_value() -> None:
    values = _series("close + 1e-12 + 2.5e2 - 3E-4", [10.0, 20.0])
    assert values.to_list() == pytest.approx([259.99970000000104, 269.99970000000104])


def test_wilder_rsi_supports_arbitrary_periods_and_zero_loss() -> None:
    closes = [1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 4.0]
    values = _series("ts_rsi(close, 2)", closes).to_list()
    # 与系统矩阵相同的 adjust=False Wilder 指数平滑。
    gains, losses = [], []
    for i, close in enumerate(closes):
        delta = close - closes[i - 1] if i else None
        gains.append(max(delta, 0.0) if delta is not None else None)
        losses.append(max(-delta, 0.0) if delta is not None else None)
    ag = al = 0.0
    expected = []
    for gain, loss in zip(gains, losses, strict=True):
        if gain is not None:
            ag = 0.5 * ag + 0.5 * gain
            al = 0.5 * al + 0.5 * loss
        expected.append(100 - 100 / (1 + ag / (al or 1e-12)))
    assert values[1:] == pytest.approx(expected[1:], nan_ok=True)
    rising = _series("ts_rsi(close, 2)", [1.0, 2.0, 3.0, 4.0]).to_list()
    assert rising[-1] == pytest.approx(100.0)
    assert compile_formula("ts_rsi(close, 2)").warmup_bars == 3
    assert not compile_formula("ts_rsi(close, 1)").ok


def test_rsi_efficiency_ratio_and_sma_can_form_oversold_votes() -> None:
    formula = (
        "if_else(ts_rsi(close, 2) < 10, 1, 0) + "
        "if_else(abs(close - ts_delay(close, 10)) / "
        "(ts_sum(abs(close - ts_delay(close, 1)), 10) + 1e-12) < 0.15 and close < ts_mean(close, 20), 1, 0)"
    )
    compiled = compile_formula(formula)
    assert compiled.ok, [e.to_dict() for e in compiled.errors]
    assert compiled.dependencies == frozenset({"close"})
