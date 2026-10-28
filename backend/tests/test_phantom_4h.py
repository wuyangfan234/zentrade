"""phantom_4h.scan_series 逐根结果 == 对每个前缀调用 oasis-quant 原函数 simplified_phantom_scan 的结果。

原函数逐字搬入 app/vendor/oasis_phantom (函数级 sha256 与上游 52730bb 一致, 见 test_vendor_functions_are_verbatim)。
"""
import ast
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import phantom_4h
from app.vendor.oasis_phantom.phantom_mtf_scan import simplified_phantom_scan
from app.vendor.oasis_phantom.resample_4h_tv import _resample_4h_tv

VENDOR = Path(__file__).parents[1] / "app" / "vendor" / "oasis_phantom"
# 上游 oasis-quant @52730bb 的函数源码 sha256 (ast.get_source_segment)
UPSTREAM_SHA = {
    "phantom_state_machine.py": {
        "rma": "f1fd7f0cf16cfe8a967bf69cfeaaa381ae3051e6028fb528c8075332a8abc450",
        "ema": "a3ae67e189cdcdc574911af689fdcb0339a662e1c7f0e5f8eefcd98240d594ab",
        "atr": "4503951a422f0a5fe43ccd4e867e5c963ab43af56abde32fef5cfa725469c1cb",
    },
}


def _funcs(path: Path) -> dict[str, str]:
    src = path.read_text(encoding="utf-8")
    return {n.name: hashlib.sha256(ast.get_source_segment(src, n).encode()).hexdigest()
            for n in ast.parse(src).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_vendor_functions_are_verbatim():
    got = _funcs(VENDOR / "phantom_state_machine.py")
    for name, sha in UPSTREAM_SHA["phantom_state_machine.py"].items():
        assert got[name] == sha, name
    # 关键函数存在且未被改名
    for name in ("phantom_shift", "phantom_trail", "leg_pivots", "structure_bias", "dmi"):
        assert name in got
    assert "simplified_phantom_scan" in _funcs(VENDOR / "phantom_mtf_scan.py")
    assert "_resample_4h_tv" in _funcs(VENDOR / "resample_4h_tv.py")


def _synthetic_4h(seed: int, n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.where(np.arange(n) % 120 < 70, 0.004, -0.003)
    c = 50 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    idx = pd.date_range("2025-01-02 09:30", periods=n, freq="4h")
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1e6}, index=idx)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_series_equals_prefix_calls_of_original(seed):
    df = _synthetic_4h(seed)
    series = phantom_4h.scan_series(df)
    hits = 0
    for j in range(phantom_4h.MIN_BARS - 1, len(df)):
        r = simplified_phantom_scan(df.iloc[:j + 1], "X", "4h")
        row = series.iloc[j]
        assert row["action"] == r["action"], (seed, j)
        assert row["direction"] == r["direction"], (seed, j)
        assert row["trigger"] == r["trigger"], (seed, j)
        assert row["score"] == pytest.approx(r["score"], abs=1e-9), (seed, j)
        if np.isfinite(r["protection"]):
            assert row["protection"] == pytest.approx(r["protection"], rel=1e-12), (seed, j)
        hits += r["action"] in ("多头条件齐备", "空头条件齐备")
    assert hits > 0  # 样本里必须出现过“条件齐备”, 否则对照没有意义


def test_series_daily_atr_protection_matches_original():
    df = _synthetic_4h(5)
    da = pd.Series(2.5, index=df.index)
    series = phantom_4h.scan_series(df, daily_atr_pct=da)
    for j in (80, 150, 259):
        r = simplified_phantom_scan(df.iloc[:j + 1], "X", "4h", daily_atr_pct=2.5)
        assert series["protection"].iloc[j] == pytest.approx(r["protection"], rel=1e-12)


def test_resample_tv_bins_on_wall_clock_across_dst():
    idx = pd.date_range("2025-10-30 09:30", "2025-11-05 15:30", freq="30min")
    idx = idx[[(t.hour, t.minute) >= (9, 30) and t.hour < 16 and t.dayofweek < 5 for t in idx]]
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    bins = sorted({t.strftime("%H:%M") for t in _resample_4h_tv(df).index})
    assert bins == ["09:30", "13:30"]
