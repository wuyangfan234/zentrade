"""Attach custom / composite registry factors to a matrix-native backtest matrix (0.47.0).

Matrix-native strategies (factor_rank_research, mined factor strategies) score through
`matrix_feature`, which only knows built-in computed fields. A user factor (`uf_*`, DSL) or a
composite factor (`cf_*`) used to fail with "unsupported matrix feature". Same approach as the
fundamental factors: after the matrix is loaded, compute the factor on a polars panel with the exact
path used by the factor inspector (`FactorBacktestService._compute_missing_factors`) and attach the
values as read-only matrix fields aligned to (timestamp, symbol).

Cross-sectional factors (rank / zscore / composite) must see the whole cross-section at once, so
they are computed in one batch and the symbol count is capped; time-series-only factors are
computed in symbol batches to bound memory.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

PANEL_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
SYMBOL_BATCH = 512
MAX_CROSS_SECTIONAL_SYMBOLS = 2500


def registry_factor_names(names) -> frozenset[str]:
    """Names that are custom / composite registry factors (the ones the matrix cannot compute)."""
    from app.factors.registry import get_factor

    out = set()
    for name in names or ():
        spec = get_factor(str(name))
        if spec is not None and spec.kind in ("custom", "composite"):
            out.add(str(name))
    return frozenset(out)


def _cross_sectional(name: str) -> bool:
    from app.factors.dsl import compile_formula
    from app.factors.registry import get_factor

    spec = get_factor(name)
    if spec is None:
        return False
    if spec.kind == "composite":
        return True
    try:
        return bool(compile_formula(spec.formula_text).cross_sectional)
    except Exception:  # unknown formula: treat as cross-sectional (single batch, capped)
        return True


def _warmup_days(names) -> int:
    from app.factors.registry import get_factor

    bars = max((int(getattr(get_factor(n), "warmup_bars", 1) or 1) for n in names), default=1)
    return int(bars * 1.6) + 15


def attach_matrix_registry_fields(
    market: Any,
    engine: Any,
    names,
    *,
    asset_type: str = "stock",
    expected_generation: str | None = None,
) -> Any:
    """Return a copy of `market` with the requested registry factors as fields (NaN where unknown)."""
    from app.backtest.factor import FactorBacktestService

    requested = sorted(n for n in registry_factor_names(names) if n not in market.fields)
    if not requested:
        return market
    labels = [str(label)[:10] for label in market.timestamp_labels]
    if not labels:
        return market
    first = date.fromisoformat(labels[0])
    last = date.fromisoformat(labels[-1])
    load_start = first - timedelta(days=_warmup_days(requested))
    symbols = list(market.symbols)
    cross = any(_cross_sectional(n) for n in requested)
    if cross and len(symbols) > MAX_CROSS_SECTIONAL_SYMBOLS:
        raise ValueError(
            f"截面型自定义因子回测最多 {MAX_CROSS_SECTIONAL_SYMBOLS} 只股票, 当前 {len(symbols)} 只"
        )
    batches = [symbols] if cross or len(symbols) <= SYMBOL_BATCH else [
        symbols[i:i + SYMBOL_BATCH] for i in range(0, len(symbols), SYMBOL_BATCH)
    ]
    shape = market.shape
    out = {name: np.full(shape, np.nan, dtype=np.float32) for name in requested}
    rows = pl.DataFrame({"date": [date.fromisoformat(d) for d in labels], "_r": np.arange(len(labels), dtype=np.int64)})
    cols = pl.DataFrame({"symbol": symbols, "_c": np.arange(len(symbols), dtype=np.int64)})
    kwargs = {"expected_generation": expected_generation} if expected_generation is not None else {}
    for batch in batches:
        panel = engine.load_panel(batch, load_start, last, columns=list(PANEL_COLUMNS), asset_type=asset_type, **kwargs)
        if panel is None or panel.is_empty():
            continue
        panel = FactorBacktestService._compute_missing_factors(panel, set(requested))
        keep = [name for name in requested if name in panel.columns]
        if not keep:
            continue
        frame = (
            panel.select(["symbol", pl.col("date").cast(pl.Date), *keep])
            .join(rows, on="date", how="inner")
            .join(cols, on="symbol", how="inner")
        )
        if frame.is_empty():
            continue
        r = frame["_r"].to_numpy()
        c = frame["_c"].to_numpy()
        for name in keep:
            values = frame[name].cast(pl.Float64, strict=False).to_numpy().astype(np.float32)
            values[~np.isfinite(values)] = np.nan
            out[name][r, c] = values
    for array in out.values():
        array.flags.writeable = False
    merged = {**dict(market.fields), **out}
    return dataclasses.replace(market, fields=MappingProxyType(merged))
