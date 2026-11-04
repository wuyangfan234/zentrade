"""因子页审计复审回归 (11-04 独立复审): 首轮修复引入或遗漏的问题, 修复前全部失败。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.factors import store
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import (
    _REGISTRY,
    FactorSpec,
    get_factor,
    register_factor,
    unregister_factor,
)


@pytest.fixture()
def clean_registry():
    yield
    for fid in [k for k in list(_REGISTRY) if k.startswith(("uf_rv", "cf_rv", "ext_rv"))]:
        _REGISTRY.pop(fid, None)


def _panel(n: int = 40) -> pl.DataFrame:
    rows = []
    for i in range(n):
        for j, symbol in enumerate(("A", "B", "C", "D")):
            close = 10.0 + ((i * (j + 2)) % 7) + j
            rows.append({
                "symbol": symbol, "date": date(2026, 1, 1) + timedelta(days=i),
                "open": 12.0, "high": close + 2, "low": close - 2, "close": close,
                "volume": 1000.0 + i * 10 + j * 7, "amount": close * 1000,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


# ── 预热: 只有嵌套窗口才抬高, 普通公式与旧口径一致 ──────────────────────

@pytest.mark.parametrize(("formula", "warmup"), [
    ("close", 1),
    ("close > open", 1),
    ("ts_mean(close, 20)", 21),
    ("ts_mean(close, 20) / ts_mean(close, 60) - 1", 61),
    ("rank(ts_mean(close, 20)) + rank(ts_delta(close, 20))", 21),
])
def test_flat_formula_warmup_unchanged(formula, warmup):
    assert compile_formula(formula).warmup_bars == warmup


# ── 评分物化: 只补请求的列, 中间依赖不留在结果里 ────────────────────────

def test_scoring_does_not_leak_intermediate_columns(clean_registry):
    from app.strategy.scoring import materialize_scoring_columns

    store.register_definition({"id": "uf_rvbase", "kind": "custom", "label": "b", "formula": "close * 2", "status": "active"})
    store.register_definition({"id": "uf_rvdep", "kind": "custom", "label": "d", "formula": "uf_rvbase + 1", "status": "active"})
    panel = _panel()
    out = materialize_scoring_columns(panel, ["uf_rvdep"])
    assert set(out.columns) - set(panel.columns) == {"uf_rvdep"}


# ── DSL: 比较式落在数值位置 ────────────────────────────────────────────

@pytest.mark.parametrize("formula", [
    "-(close > open)",
    "abs(close > open)",
    "sign(close > open)",
    "clamp(close > open, 0, 1)",
    "power(close > open, 2)",
    "ts_delta(close > open, 5)",
    "winsorize(close > open, 2)",
])
def test_comparison_in_numeric_position_runs(formula):
    compiled = compile_formula(formula)
    assert compiled.ok, formula
    out = compiled.frame_transform(_panel())
    assert out is not None and out[FACTOR_COLUMN].dtype == pl.Float64


def test_winsorize_without_k_compiles():
    compiled = compile_formula("winsorize(close)")
    assert compiled.ok
    assert compiled.frame_transform(_panel()) is not None


# ── 依赖方元数据: 改被引用方的公式后, 引用方的预热跟着更新 ─────────────

def _factor_app(tmp_path):
    from app.api.factors import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    return app


def test_formula_update_rebuilds_dependents(tmp_path, clean_registry, monkeypatch):
    from app.api import factors as factors_api

    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *a, **k: None)
    monkeypatch.setattr(factors_api, "_invalidate_runtime", lambda request: None)
    client = TestClient(_factor_app(tmp_path))
    assert client.post("/api/factors/custom", json={"id": "uf_rvb", "label": "b", "formula": "close"}).status_code == 200
    assert client.post("/api/factors/custom", json={"id": "uf_rvd", "label": "d", "formula": "uf_rvb + 1"}).status_code == 200
    assert client.post("/api/factors/composite", json={"id": "cf_rvc", "label": "c", "members": {"uf_rvd": 1, "volume": 1}}).status_code == 200
    assert get_factor("uf_rvd").warmup_bars == 1
    r = client.post("/api/factors/custom/uf_rvb/update", json={"label": "b", "formula": "ts_mean(close, 100)"})
    assert r.status_code == 200
    assert get_factor("uf_rvb").warmup_bars == 101
    assert get_factor("uf_rvd").warmup_bars >= 101
    assert get_factor("cf_rvc").warmup_bars >= 101


# ── 扩展表快照字段: 当前可用, 但不能做历史检验 ─────────────────────────

def test_snapshot_ext_factor_is_not_history_available(clean_registry):
    from app.factors import registry
    from app.services import auto_mining

    snap = FactorSpec(id="uf_rvsnap_px", label="snap", group="扩展数据", formula_text="snapshot", kind="base",
                      tags=("ext", "rvsnap", "snapshot"))
    ts = FactorSpec(id="uf_rvts_px", label="ts", group="扩展数据", formula_text="timeseries", kind="base",
                    tags=("ext", "rvts"))
    for spec in (snap, ts):
        unregister_factor(spec.id)
        register_factor(spec)
    assert registry.factor_available(snap)
    assert not registry.factor_history_available(snap)
    assert registry.factor_history_available(ts)
    ids = {item["id"] for item in registry.factor_columns_view(available_only=True)}
    assert "uf_rvsnap_px" not in ids and "uf_rvts_px" in ids
    pool = auto_mining.screen_candidates("stock")
    assert "uf_rvsnap_px" not in pool and "uf_rvts_px" in pool


def test_ext_snapshot_config_tags_snapshot(tmp_path):
    from app.factors import ext_factors

    cfg_snap = SimpleNamespace(id="rvs", label="S", mode="snapshot")
    cfg_ts = SimpleNamespace(id="rvt", label="T", mode="timeseries")
    field = SimpleNamespace(name="px", label="价")
    orig_load, orig_fields = ext_factors._load_configs, ext_factors._numeric_fields
    try:
        ext_factors._load_configs = lambda root: [cfg_snap, cfg_ts]
        ext_factors._numeric_fields = lambda cfg: [field]
        specs = {s.id: s for s in ext_factors.ext_factor_specs(tmp_path)}
    finally:
        ext_factors._load_configs, ext_factors._numeric_fields = orig_load, orig_fields
    snap_id = ext_factors.ext_column_name("rvs", "px")
    ts_id = ext_factors.ext_column_name("rvt", "px")
    assert "snapshot" in specs[snap_id].tags
    assert "snapshot" not in specs[ts_id].tags


def test_library_reports_history_availability(tmp_path, clean_registry):
    snap = FactorSpec(id="uf_rvlib_px", label="snap", group="扩展数据", formula_text="snapshot", kind="base",
                      tags=("ext", "rvlib", "snapshot"))
    unregister_factor(snap.id)
    register_factor(snap)
    client = TestClient(_factor_app(tmp_path))
    items = {f["id"]: f for f in client.get("/api/factors").json()["factors"]}
    assert items["uf_rvlib_px"]["available"] is True
    assert items["uf_rvlib_px"]["history_available"] is False
    assert items["momentum_5d"]["history_available"] is True
