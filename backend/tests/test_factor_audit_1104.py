"""因子页审计回归 (11-04): 每条对应一处已复现的缺陷, 修复前全部失败。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.factors import store
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import _REGISTRY, factor_available, get_factor


@pytest.fixture()
def clean_registry():
    yield
    for fid in [k for k in list(_REGISTRY) if k.startswith(("uf_aud", "cf_aud"))]:
        _REGISTRY.pop(fid, None)


def _panel(n: int = 60) -> pl.DataFrame:
    rows = []
    for i in range(n):
        for j, symbol in enumerate(("A", "B", "C", "D")):
            close = 10.0 + i * (j + 1) * 0.3 + j * 50
            rows.append({
                "symbol": symbol, "date": date(2026, 1, 1) + timedelta(days=i),
                "open": close - 1, "high": close + 2, "low": close - 2, "close": close,
                "volume": 1000.0 + i * 10 + j * 7, "amount": close * 1000, "turnover_rate": 0.1,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _custom(fid: str, formula: str) -> dict:
    return {"id": fid, "kind": "custom", "label": fid, "formula": formula, "status": "active"}


def _composite(fid: str, members: dict) -> dict:
    return {"id": fid, "kind": "composite", "label": fid, "members": members, "status": "active"}


# ── DSL ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("formula", [
    "rank(rank(ts_mean(volume, 5)))",
    "zscore(rank(ts_mean(close, 5)))",
    "rank(close * rank(ts_mean(volume, 5)))",
])
def test_nested_cross_section_runs(formula):
    compiled = compile_formula(formula)
    assert compiled.ok
    values = compiled.frame_transform(_panel())[FACTOR_COLUMN]
    assert values.is_finite().sum() > 0


@pytest.mark.parametrize("formula", ["close and volume", "if_else(close, 1, 2)", "close or (volume > 1)"])
def test_value_logic_is_a_compile_error(formula):
    compiled = compile_formula(formula)
    assert not compiled.ok
    assert compiled.errors[0].code == "E015"


def test_comparison_logic_still_compiles():
    for formula in ("(close > 10) and (volume > 1000)", "if_else(close > 20, 1, -1)"):
        compiled = compile_formula(formula)
        assert compiled.ok, formula
        assert compiled.frame_transform(_panel()) is not None


@pytest.mark.parametrize("formula", ["ts_mean(ts_sum(close, 20), 10)", "ts_delay(ts_mean(close, 20), 10)"])
def test_nested_window_warmup_covers_first_value(formula):
    compiled = compile_formula(formula)
    values = compiled.frame_transform(_panel(80)).filter(pl.col("symbol") == "A")[FACTOR_COLUMN].to_list()
    first = next(i for i, v in enumerate(values) if v is not None and v == v)
    assert compiled.warmup_bars >= first + 1


# ── 存储 / 注册表 ─────────────────────────────────────────────────────

def test_non_object_json_does_not_drop_later_factors(tmp_path, clean_registry):
    d = tmp_path / "user_data" / "custom_factors"
    d.mkdir(parents=True)
    (d / "uf_auda.json").write_text(json.dumps(_custom("uf_auda", "close")))
    (d / "uf_audb.json").write_text("[]")
    (d / "uf_audc.json").write_text(json.dumps(_custom("uf_audc", "volume")))
    loaded = store.load_into_registry(tmp_path)
    assert {"uf_auda", "uf_audc"} <= set(loaded)


def test_shared_base_column_is_not_a_cycle(clean_registry):
    store.register_definition(_composite("cf_audleaf", {"close": 1, "volume": 1}))
    store.register_definition(_composite("cf_audparent", {"cf_audleaf": 1, "volume": 1}))
    with pytest.raises(ValueError, match="循环"):
        store.register_definition({**_composite("cf_audleaf", {"cf_audparent": 1, "close": 1}), "version": 2})


def test_long_reverse_chain_loads_fully(tmp_path, clean_registry):
    d = tmp_path / "user_data" / "custom_factors"
    d.mkdir(parents=True)
    chain = {"cf_auda": {"cf_audb": 1, "open": 1}, "cf_audb": {"cf_audc": 1, "high": 1},
             "cf_audc": {"cf_audd": 1, "low": 1}, "cf_audd": {"cf_aude": 1, "close": 1},
             "cf_aude": {"close": 1, "volume": 1}}
    for fid, members in chain.items():
        (d / f"{fid}.json").write_text(json.dumps(_composite(fid, members)))
    loaded = store.load_into_registry(tmp_path)
    assert set(chain) <= set(loaded)


def test_availability_does_not_match_id_substring(clean_registry):
    store.register_definition(_custom("uf_audturnover_note", "close"))
    assert factor_available(get_factor("uf_audturnover_note"))
    assert not factor_available(get_factor("turnover_z_60d"))


# ── 检验 / 物化 ───────────────────────────────────────────────────────

def test_custom_referencing_custom_materializes(clean_registry):
    from app.backtest.factor import FactorBacktestService

    store.register_definition(_custom("uf_audbase", "close * 2"))
    store.register_definition(_custom("uf_auddep", "uf_audbase + 1"))
    store.register_definition(_composite("cf_audmix", {"uf_audbase": 1, "volume": 1}))
    for target in ("uf_auddep", "cf_audmix"):
        out = FactorBacktestService._compute_missing_factors(_panel(), {target})
        assert target in out.columns, target
        assert out[target].is_not_null().sum() > 0, target
    out = FactorBacktestService._compute_missing_factors(_panel(), {"uf_auddep"})
    assert (out["uf_auddep"] - (out["close"] * 2 + 1)).abs().max() < 1e-9


def test_scoring_materializes_nested_custom(clean_registry):
    from app.strategy.scoring import materialize_scoring_columns

    store.register_definition(_custom("uf_audbase", "close * 2"))
    store.register_definition(_custom("uf_auddep", "uf_audbase + 1"))
    out = materialize_scoring_columns(_panel(), ["uf_auddep"])
    assert "uf_auddep" in out.columns and out["uf_auddep"].is_not_null().all()


def test_monthly_annualization_uses_real_last_period():
    from app.backtest.factor import FactorBacktestService

    # 月频请求 01-02 → 02-13, 实际只有 01-02 → 02-02 一期
    nav = [{"date": "2026-01-02", "Q1": 1.0, "Q2": 1.10}]
    stats = FactorBacktestService._calc_group_stats(
        nav, date(2026, 1, 2), date(2026, 2, 13), "monthly", period_end=date(2026, 2, 2),
    )
    q2 = next(s for s in stats if s["label"] == "Q2")
    assert q2["annual_return"] == pytest.approx(1.10 ** (365.25 / 31) - 1, rel=1e-3)


def test_evaluate_monthly_uses_last_rebalance_date():
    from app.backtest.factor import FactorBacktestService, FactorConfig

    days = [date(2026, 1, 2) + timedelta(days=i) for i in range(43)]
    days = [d for d in days if d.weekday() < 5]
    rows = []
    for i, d in enumerate(days):
        for k in range(4):
            rows.append({"symbol": f"S{k}", "date": d, "close": 100.0 * (1 + 0.002 * k) ** i, "fct": float(k)})
    panel = pl.DataFrame(rows)
    service = FactorBacktestService(SimpleNamespace())
    cfg = FactorConfig(factor_name="fct", symbols=None, start=days[0], end=days[-1], n_groups=2, rebalance="monthly", slippage_bps=0)
    result = service._evaluate_panel(panel, cfg, "r", 0.0)
    assert result.error is None
    top = result.group_stats[-1]
    held_days = (date(2026, 2, 2) - date(2026, 1, 2)).days
    assert top["annual_return"] == pytest.approx((1 + top["total_return"]) ** (365.25 / held_days) - 1, rel=1e-3)


def test_newey_west_uses_unrounded_ic():
    from app.backtest import factor as factor_mod

    rng = np.random.default_rng(11)
    raw = list(rng.normal(0.004, 0.02, 40))
    rounded = [round(x, 4) for x in raw]
    item_series = [{"date": str(i), "ic": v} for i, v in enumerate(rounded)]
    assert factor_mod._ic_values_for_stats(item_series, raw) == raw


# ── 删除引用 / 缓存 ──────────────────────────────────────────────────

def _factor_app(tmp_path):
    from app.api.factors import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    return app


def test_delete_scan_finds_dsl_signal_and_candidate_references(tmp_path, clean_registry):
    from app.api.factors import _find_references

    store.register_definition(_custom("uf_auda", "close"))
    d = tmp_path / "user_data"
    (d / "custom_factors").mkdir(parents=True)
    (d / "custom_signals").mkdir(parents=True)
    (d / "custom_factors" / "uf_audb.json").write_text(json.dumps(_custom("uf_audb", "uf_auda * 2")))
    (d / "custom_signals" / "s1.json").write_text(json.dumps(
        {"id": "s1", "conditions": [{"left": "close", "op": ">", "right": "uf_auda"}]}))
    (d / "research_candidates.json").write_text(json.dumps([
        {"id": "rc1", "kind": "factor", "config": {"factor_name": "uf_auda"}},
        {"id": "rc2", "kind": "strategy", "config": {"factor_names": ["close", "uf_auda"]}},
    ]))
    refs = _find_references(tmp_path, "uf_auda")
    assert "custom_factors/uf_audb.json" in refs
    assert "user_data/custom_signals/s1.json" in refs
    assert any("rc1" in r for r in refs) and any("rc2" in r for r in refs)
    assert _find_references(tmp_path, "uf_aud") == []


def test_formula_update_and_force_delete_invalidate_strategy_cache(tmp_path, clean_registry, monkeypatch):
    from app.api import factors as factors_api
    from app.factors import dsl

    calls: list[str] = []
    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *a, **k: None)
    monkeypatch.setattr(factors_api, "_invalidate_runtime", lambda request: calls.append("inv"))
    client = TestClient(_factor_app(tmp_path))
    assert client.post("/api/factors/custom", json={"id": "uf_audx", "label": "x", "formula": "close"}).status_code == 200
    calls.clear()
    dsl.compile_formula_cached("close")
    assert dsl.compile_formula_cached.cache_info().currsize > 0
    assert client.post("/api/factors/custom/uf_audx/update", json={"label": "x", "formula": "volume"}).status_code == 200
    assert calls == ["inv"]
    assert client.delete("/api/factors/custom/uf_audx?force=true").status_code == 200
    assert calls == ["inv", "inv"]


# ── 自动挖掘 ─────────────────────────────────────────────────────────

def test_auto_mining_pool_respects_asset_type(monkeypatch):
    from app.services import auto_mining

    seen: dict[str, list[str]] = {}

    class _Svc:
        def __init__(self, engine):
            pass

        def run_batch(self, cfg):
            seen[cfg.asset_type] = list(cfg.factor_names)
            return SimpleNamespace(results=[])

    monkeypatch.setattr(auto_mining, "FactorBacktestService", _Svc)
    for asset in ("stock", "etf"):
        auto_mining.screen_all_factors(None, asset_type=asset, start=None, end=date(2026, 11, 1), profile="balanced")
    assert seen["etf"], "ETF 自动挖掘候选池不能为空"
    assert "turnover_rate" not in seen["etf"]
    assert all(factor_available(get_factor(f)) for f in seen["stock"])


def test_auto_mining_signature_ignores_diagnostics():
    from app.services.mining_jobs import compute_run_signature

    base = {"factor_names": ["close"], "auto": True, "auto_screening": {"pool": ["close"], "elapsed_ms": 100}}
    other = json.loads(json.dumps(base))
    other["auto_screening"]["elapsed_ms"] = 101
    assert compute_run_signature(base, {"x": 1}) == compute_run_signature(other, {"x": 1})
    changed = json.loads(json.dumps(base))
    changed["auto_screening"]["pool"] = ["open"]
    assert compute_run_signature(base, {"x": 1}) != compute_run_signature(changed, {"x": 1})
    # 原请求对象不被改写 (存盘快照保留耗时)
    assert base["auto_screening"]["elapsed_ms"] == 100


# ── 发现页决定 ──────────────────────────────────────────────────────

def _week(monkeypatch, fd):
    monkeypatch.setattr(fd, "load_week", lambda dd, week=None: {"run_id": "r1", "candidates": [{"signature": "sig1"}]})


def test_save_with_failed_promote_is_not_recorded(tmp_path, monkeypatch):
    from app.services import factor_discover as fd

    _week(monkeypatch, fd)

    def boom(run_id, sig):
        raise RuntimeError("artifact gone")

    with pytest.raises(fd.PromoteError):
        fd.decide(tmp_path, "2026-W45", "sig1", "save", promoter=boom)
    assert fd._decisions(tmp_path) == {}


def test_discard_after_save_removes_candidate(tmp_path, monkeypatch):
    from app.services import factor_discover as fd

    _week(monkeypatch, fd)
    removed: list[str] = []
    fd.decide(tmp_path, "2026-W45", "sig1", "save", promoter=lambda r, s: {"id": "rc_1"})
    assert fd._decisions(tmp_path)["2026-W45|sig1"]["candidate_id"] == "rc_1"
    out = fd.decide(tmp_path, "2026-W45", "sig1", "discard", promoter=None, remover=removed.append)
    assert removed == ["rc_1"]
    assert out["removed_candidate_id"] == "rc_1"
    assert fd._decisions(tmp_path)["2026-W45|sig1"]["action"] == "discard"


def test_decide_api_returns_409_when_promote_fails(tmp_path, monkeypatch):
    from app.api import factor_lab
    from app.services import factor_discover as fd

    _week(monkeypatch, fd)

    def boom(run_id, sig):
        raise RuntimeError("artifact gone")

    monkeypatch.setattr(factor_lab, "_promoter", lambda request: boom)
    app = FastAPI()
    app.include_router(factor_lab.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    r = TestClient(app).post("/api/factor-lab/candidates/decide", json={"week": "2026-W45", "signature": "sig1", "action": "save"})
    assert r.status_code == 409
    assert fd._decisions(tmp_path) == {}


def test_discover_observe_pass_is_labelled(monkeypatch, tmp_path):
    """Sharpe-only OR stays (user chose B) but each candidate says which gate it passed."""
    from app.services import factor_discover as fd

    row = {"signature": "s", "definition_json": json.dumps({"kind": "factor_rank", "scoring": {"close": 1}, "directions": {"close": "high"}}),
           "confidence": "low", "valid_folds": 1, "oos_positive_fold_ratio": 0.0, "oos_sharpe": 0.6,
           "oos_max_drawdown": -0.9, "oos_n_trades": 1, "oos_return": 0.1}
    from app.services.mining_jobs import MiningRunStore

    path = MiningRunStore(tmp_path).artifact_path("weekly-2026-W45", "candidates")
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([row]).write_parquet(path)
    out = fd._candidates(tmp_path, "weekly-2026-W45")
    assert out[0]["passed_oos"] is True
    assert out[0]["oos_gate"] == "observe"


def test_funnel_counts_oos_pass_before_backtest():
    from app.services import factor_discover as fd

    cands = [{"signature": "a", "nav": {}, "stats": {}}, {"signature": "b", "error": "RuntimeError"}]
    out = fd._assemble("weekly-2026-W45", {"request": {}}, {}, ["X"], date(2026, 1, 1), date(2026, 2, 1), None, cands, oos_passed=2)
    assert out["funnel"]["oos_passed"] == 2
    assert out["funnel"]["passed"] == 1


# ── 采集状态机 ──────────────────────────────────────────────────────

def _item(item_id: str, stage: str, *, source: str = "rss") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"id": item_id, "source": source, "source_name": "probe", "title": item_id, "link": "",
            "published": now, "fetched_at": now, "body": "body", "stage": stage,
            "prefilter": "manual" if source == "manual" else "llm"}


def test_triage_non_json_is_a_format_failure(tmp_path, monkeypatch):
    from app.services import factor_intake as fi

    fi._save_items(tmp_path, {"bad": _item("bad", "queued")})
    monkeypatch.setattr(fi, "_call", lambda *a, **k: "not JSON at all")
    fi.process_queue(tmp_path)
    got = fi.load_items(tmp_path)["bad"]
    assert got["failure"] == "model_format"
    assert got["reason"] != "非美股个股"
    assert got.get("last_model_text") == "not JSON at all"


def test_extract_items_object_is_a_format_failure(tmp_path, monkeypatch):
    from app.services import factor_intake as fi

    fi._save_items(tmp_path, {"bad": _item("bad", "to_extract", source="manual")})
    monkeypatch.setattr(fi, "_rules_vocab", lambda dd: "")
    monkeypatch.setattr(fi, "_vocab", lambda: {"base": "close", "ops": "", "lib": ""})
    monkeypatch.setattr(fi, "_call", lambda *a, **k: json.dumps({"items": {"name_zh": "x"}}))
    fi.process_queue(tmp_path)
    got = fi.load_items(tmp_path)["bad"]
    assert got["stage"] == "archived"
    assert got["failure"] == "model_format"
    assert got["attempts"] >= 1


def test_screen_error_does_not_enter_review(tmp_path, monkeypatch):
    from app.services import factor_discover, factor_intake as fi

    it = _item("x", "to_screen")
    it["formulas"] = [{"name": "f", "dsl": "close", "direction": "high", "quote_hash": "h"}]
    fi._save_items(tmp_path, {"x": it})
    monkeypatch.setattr(factor_discover, "can_run_heavy", lambda now=None: True)
    monkeypatch.setattr(factor_discover, "watchlist_symbols", lambda: [])
    import app.backtest.worker as worker
    monkeypatch.setattr(worker, "run_worker_task", lambda task: {"screens": {"x:0": {"error": "RuntimeError"}}})
    monkeypatch.setattr(worker, "make_worker_task", lambda *a, **k: None)
    fi.screen_pending(tmp_path, None)
    got = fi.load_items(tmp_path)["x"]
    assert got["stage"] != "pending"
    assert got["failure"] == "screen_error"


def test_retry_clears_old_screen(tmp_path):
    from app.services import factor_intake as fi

    it = _item("m", "archived", source="manual")
    it["formulas"] = [{"name": "f", "dsl": "close", "screen": {"duplicate": True, "corr": 0.99}}]
    it.update(reason="重复", failure="duplicate", duplicate=True)
    fi._save_items(tmp_path, {"m": it})
    fi.retry(tmp_path, "m")
    got = fi.load_items(tmp_path)["m"]
    assert got["stage"] == "to_screen"
    assert "screen" not in got["formulas"][0]


def test_manual_process_does_not_shift_next_auto_tick(tmp_path):
    from app.services import factor_intake as fi

    st = {"last_tick": "2026-11-04T14:00:00+00:00"}
    fi._write_json(fi._dir(tmp_path) / "run_state.json", st)
    fi._record_manual_process(tmp_path, datetime(2026, 11, 4, 14, 10, tzinfo=timezone.utc), {"model": {}})
    out = fi._read_json(fi._dir(tmp_path) / "run_state.json", {})
    assert out["last_tick"] == "2026-11-04T14:00:00+00:00"
    assert out["last_process"].startswith("2026-11-04T14:10")


# ── 信号 create-only ─────────────────────────────────────────────────

def test_signal_create_only_rejects_existing_id(tmp_path):
    from app.api import signals

    app = FastAPI()
    app.include_router(signals.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    app.state.strategy_engine = None
    client = TestClient(app)
    body = {"id": "dup", "name": "a", "kind": "entry", "enabled": True,
            "conditions": [{"left": "close", "op": ">", "right": "1", "leftDays": 0, "rightDays": 0}]}
    assert client.post("/api/custom-signals?create_only=true", json=body).status_code == 200
    second = client.post("/api/custom-signals?create_only=true", json={**body, "name": "b"})
    assert second.status_code == 409
    # 编辑路径 (无 create_only) 仍是覆盖
    assert client.post("/api/custom-signals", json={**body, "name": "c"}).status_code == 200
