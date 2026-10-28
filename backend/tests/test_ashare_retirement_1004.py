"""Retired deviation/share-capital authoring surfaces; history remains readable."""
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import factors, monitor_rules, signals, strategy, watchlist
from app.factors.registry import all_factors, factor_columns_view, get_factor
from app.indicators import pipeline
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.storage.repository import DataStore, KlineRepository
from app.strategy import config, custom_signals, custom_signals_ai
from app.strategy.ai_generator import AIStrategyGenerator, GUIDE_PATH
from app.strategy.engine import StrategyEngine
from app.strategy.prompt_builder import build_step1

DEVIATIONS = {"deviate_3d", "deviate_10d", "deviate_30d"}
SHARE_FACTORS = {"turnover_rate", "turnover_ratio_5d", "turnover_z_60d",
                 "turnover_mean_20d", "turnover_std_20d", "log_float_mv"}
SHARE_FIELDS = SHARE_FACTORS | {"total_shares", "float_shares", "market_cap", "float_market_cap"}
BUILTINS = Path(__file__).resolve().parents[1] / "app/strategy/builtin"
CODE = '''import polars as pl
META = {"id": "custom_probe", "params": [], "scoring": {}}
ENTRY_SIGNALS = ["signal_ma20_breakout"]
def filter(df, params):
    return pl.col("close") > pl.col("ma20")
'''


def bars():
    return pl.DataFrame([
        {"symbol": "AAPL", "date": date(2026, 1, 1) + timedelta(days=i),
         "open": 100.0+i, "high": 102.0+i, "low": 99.0+i, "close": 101.0+i,
         "volume": 1000.0+i, "amount": (101.0+i)*(1000.0+i),
         "raw_close": 101.0+i, "raw_high": 102.0+i, "raw_low": 99.0+i}
        for i in range(90)
    ])


def request_for(tmp_path):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        strategy_engine=StrategyEngine(strategy_dirs=[BUILTINS]),
    )))


def test_history_removes_deviation_but_keeps_normal_momentum(tmp_path):
    frame = pipeline.compute_enriched_history_window(bars(), tmp_path)
    assert not DEVIATIONS.intersection(frame.columns)
    assert "momentum_3d" not in frame.columns
    for n in (5, 10, 20, 30, 60):
        assert frame[f"momentum_{n}d"][-1] == pytest.approx(190.0 / (190.0 - n) - 1)
    assert frame["rsi_14"][-1] is not None


def test_live_aggregation_removes_only_deviation_specific_state(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = pipeline.compute_indicators(bars())
    repo._build_live_agg(bars()["date"][-1] + timedelta(days=1))
    cols = set(repo.get_live_agg().columns)
    assert "_close_3d_ago" not in cols
    assert {f"_close_{n}d_ago" for n in (5, 10, 20, 30, 60)} <= cols


@pytest.mark.parametrize("incremental", [False, True])
def test_quote_refresh_never_recreates_deviation_columns(tmp_path, monkeypatch, incremental):
    from app.services.quote_service import QuoteService
    from app.market_clock import us_clock

    history = bars()
    yesterday = history["date"][-1]
    today = yesterday + timedelta(days=1)
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = pipeline.compute_indicators(history)
    repo._build_live_agg(today)
    prev = repo._enriched_history_cache.tail(1)
    partition = tmp_path / "kline_daily/date=2026-03-31"
    partition.mkdir(parents=True)
    history.write_parquet(partition / "part.parquet")
    saved = []
    service = QuoteService()
    service._repo = SimpleNamespace(
        store=repo.store,
        get_live_agg=lambda: repo.get_live_agg() if incremental else pl.DataFrame(),
        get_enriched_latest=lambda: (prev, yesterday),
        get_instruments=lambda: pl.DataFrame(),
        get_historical_shares=lambda: pl.DataFrame(),
        flush_live_enriched_asset=lambda asset, frame: saved.append(frame),
    )
    monkeypatch.setattr(us_clock, "today", lambda: today)
    daily = history.tail(1).with_columns(pl.lit(today).alias("date"), pl.lit(191.0).alias("close"))
    service._flush_live_enriched(daily)
    assert len(saved) == 1  # do not accept a swallowed exception as a clean result
    assert not DEVIATIONS.intersection(saved[0].columns)
    assert "momentum_3d" not in saved[0].columns
    assert saved[0]["momentum_10d"][0] is not None


def test_removed_pipeline_symbols_and_public_columns():
    for name in ("attach_deviation_columns", "attach_deviation_columns_today",
                 "BENCHMARK_INDEX_SYMBOLS", "load_benchmark_momentum"):
        assert not hasattr(pipeline, name)
    assert "SPY" in CORE_INDEX_SYMBOLS
    assert not DEVIATIONS.intersection(pipeline.ENRICHED_COLUMNS)
    assert not (DEVIATIONS | {"turnover_rate"}).intersection(watchlist._WATCHLIST_COLS)
    assert "turnover_rate" in pipeline.ENRICHED_STORAGE_COLS


def test_signal_options_and_ai_use_same_filtered_field_catalog():
    opts = signals.get_options()
    fields = {f["key"] for f in opts["fields"]}
    grouped = {f["key"] for g in opts["groups"] for f in g["fields"]}
    assert not (DEVIATIONS | SHARE_FIELDS) & fields
    assert fields == grouped == set(custom_signals.allowed_fields())
    prompt = custom_signals_ai.build_messages("均线向上")[0]["content"]
    assert all(field not in prompt for field in DEVIATIONS | SHARE_FIELDS)
    assert "ma20_bias" in prompt and "momentum_20d" in prompt


@pytest.mark.parametrize("field", sorted(DEVIATIONS | SHARE_FIELDS | {"no_such_field"}))
@pytest.mark.parametrize("side", ["left", "right"])
def test_new_signal_rejects_retired_or_unknown_field_before_save(tmp_path, field, side):
    app = FastAPI()
    app.include_router(signals.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    condition = {"left": field if side == "left" else "close", "op": ">",
                 "right": "0" if side == "left" else f"field:{field}"}
    response = TestClient(app).post("/api/custom-signals", json={
        "id": "retired", "name": "拒绝退役字段", "kind": "entry", "conditions": [condition],
    })
    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert not (tmp_path / "user_data/custom_signals/retired.json").exists()


@pytest.mark.parametrize("field", sorted(DEVIATIONS))
@pytest.mark.parametrize("method", [strategy.save_config, strategy.patch_config])
def test_scoring_config_rejects_retired_deviation(tmp_path, method, field):
    request = request_for(tmp_path)
    with pytest.raises(HTTPException, match=field):
        method(strategy.SaveConfigRequest(strategy_id="bullish_alignment",
                                         overrides={"scoring": {field: 1.0}}), request)


@pytest.mark.parametrize("field", ["market_cap_min", "market_cap_max", "float_cap_min", "float_cap_max"])
@pytest.mark.parametrize("method", [strategy.save_config, strategy.patch_config])
def test_config_rejects_new_capital_filters_without_erasing_history(tmp_path, method, field):
    request = request_for(tmp_path)
    req = strategy.SaveConfigRequest(strategy_id="bullish_alignment",
                                    overrides={"basic_filter": {field: 1e9}})
    with pytest.raises(HTTPException, match=field):
        method(req, request)
    # A stored historical filter may pass through unchanged while editing other settings.
    config.save_override(tmp_path, "bullish_alignment", {"basic_filter": {field: 1e9}})
    method(req, request)
    assert config.load_override(tmp_path, "bullish_alignment")["basic_filter"][field] == 1e9


def test_public_factor_library_hides_exactly_share_factors_not_financials():
    full = {s.id for s in all_factors()}
    visible = {s["id"] for s in factors.list_factors(asset_type=None)["factors"]}
    assert full - visible == SHARE_FACTORS
    assert "roe_latest" in visible and "pb_latest" in visible
    assert SHARE_FACTORS <= {s["id"] for s in factor_columns_view()}
    assert all(get_factor(fid) is not None for fid in SHARE_FACTORS)


@pytest.mark.parametrize("field", sorted(DEVIATIONS | SHARE_FIELDS))
def test_new_strategy_code_cannot_reference_removed_fields(field):
    code = CODE.replace('pl.col("close")', f'pl.col("{field}")')
    result = AIStrategyGenerator().validate_code(code)
    assert result["valid"] is False
    assert field in result["error"]


@pytest.mark.parametrize("filename", ["strategy-guide-compact.md", "strategy-guide.md",
                                      "strategy-builder-step1.md", "strategy-example.md"])
def test_strategy_guides_do_not_advertise_missing_share_fields(filename):
    text = (GUIDE_PATH.parent / filename).read_text()
    assert all(field not in text for field in SHARE_FIELDS | {"market_cap_min", "float_cap_min"})


@pytest.mark.parametrize("field", ["market_cap_min", "market_cap_max", "float_cap_min", "float_cap_max"])
def test_new_strategy_prompt_rejects_unavailable_capital_filter(field):
    with pytest.raises(ValueError, match=field):
        build_step1("test", "", "long", "均线", basic_filter={field: 1e9})
    prompt = build_step1("test", "", "long", "均线", basic_filter={field: None, "price_min": 5})
    assert field not in prompt and "price_min" in prompt


def test_draft_list_hides_high_turnover_but_internal_registry_preserves_it(tmp_path):
    request = request_for(tmp_path)
    visible = strategy.list_strategies(request, include_research=True)["strategies"]
    assert "high_turnover_surge" not in {s["id"] for s in visible}
    assert "factor_rank_research" in {s["id"] for s in visible}
    assert request.app.state.strategy_engine.get("high_turnover_surge").meta["research_only"]


@pytest.mark.parametrize("method", [strategy.save_config, strategy.patch_config])
def test_config_cannot_add_missing_data_scoring_but_keeps_legacy_weights(tmp_path, method):
    request = request_for(tmp_path)
    engine = request.app.state.strategy_engine
    sid = "bullish_alignment"
    before = dict(engine.get(sid).meta["scoring"])
    assert "turnover_rate" in before
    req = strategy.SaveConfigRequest(strategy_id=sid, overrides={"scoring": before})
    method(req, request)  # unchanged old weights remain compatible
    added = {**before, "turnover_z_60d": 0.1}
    with pytest.raises(HTTPException, match="turnover_z_60d"):
        method(strategy.SaveConfigRequest(strategy_id=sid, overrides={"scoring": added}), request)
    assert engine.get(sid).meta["scoring"] == before
    assert "turnover_z_60d" not in config.load_override(tmp_path, sid).get("scoring", {})


@pytest.mark.parametrize("field", ["signal_deviate_3d", "signal_unknown", "csg_unknown"])
def test_monitor_save_rejects_unknown_signal_instead_of_silent_non_trigger(tmp_path, field):
    app = FastAPI()
    app.include_router(monitor_rules.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    response = TestClient(app).post("/api/monitor-rules", json={
        "id": "retired", "name": "invalid", "type": "signal", "scope": "symbols", "symbols": ["AAPL"],
        "conditions": [{"field": field, "op": "truth"}],
    })
    assert response.status_code == 400
    assert field in response.json()["detail"]
