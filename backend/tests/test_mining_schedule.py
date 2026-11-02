from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import mining_schedule, preferences
from app.services.mining_jobs import MiningRunStore


class FakeRepo:
    def __init__(self, data_dir: Path, *, latest: date = date(2026, 8, 14)) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)
        self.latest = latest
        self.generation = "generation-1"

    def latest_enriched_date(self, asset_type: str = "stock") -> date | None:
        assert asset_type == "stock"
        return self.latest

    def get_matrix_data_generation(self, asset_type: str = "stock") -> str:
        assert asset_type == "stock"
        return self.generation

    def get_instruments_asset(self, asset_type: str = "stock") -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["BAC"],
            "name": ["示例"],
            "total_shares": [1_000_000.0],
            "float_shares": [800_000.0],
        })


class FakeManager:
    def __init__(self, data_dir: Path) -> None:
        self.store = MiningRunStore(data_dir)
        self.calls: list[dict] = []

    def start(self, request, fingerprint, *, force: bool, source: str, run_id: str):
        call = {
            "request": request,
            "fingerprint": fingerprint,
            "force": force,
            "source": source,
            "run_id": run_id,
        }
        self.calls.append(call)
        manifest = self.store.create(request, fingerprint, run_id=run_id)
        return {"run_id": manifest["run_id"]}


POOL = ["NVDA", "MU", "RKLB"]


@pytest.fixture(autouse=True)
def fixed_pool(monkeypatch):
    """调度测试的 enriched 是占位文件, 股票池固定注入 (池本身见 test_mining_pool.py)。"""
    from app.services import mining_pool

    monkeypatch.setattr(mining_pool, "build_mining_pool", lambda *_a, **_k: mining_pool.MiningPool(
        symbols=list(POOL), max_symbols=200, by_source={"watchlist": 3, "theme": 0, "liquidity": 0}))


@pytest.fixture
def scheduled_state(tmp_path: Path, monkeypatch):
    repo = FakeRepo(tmp_path)
    manager = FakeManager(tmp_path)
    state = SimpleNamespace(repo=repo, mining_manager=manager, strategy_engine=None)
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    _write_prerequisites(tmp_path, repo.latest, days=1200)
    return state


def _friday(week_offset: int = 0) -> datetime:
    return datetime(2026, 8, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai")) + timedelta(
        weeks=week_offset
    )


def _write_prerequisites(data_dir: Path, latest: date, *, days: int) -> None:
    enriched = data_dir / "kline_daily_enriched"
    trading_dates: list[date] = []
    for offset in range(days):
        day = latest - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        trading_dates.append(day)
        partition = enriched / f"date={day.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").write_bytes(b"enriched")
    regime = data_dir / "regime_history" / "part.parquet"
    regime.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "date": sorted(trading_dates),
        "state": ["range"] * len(trading_dates),
    }).write_parquet(regime)


def test_market_date_and_iso_week_use_market_timezone():
    utc = ZoneInfo("UTC")
    instant = datetime(2026, 8, 13, 16, 30, tzinfo=utc)

    # 美东 (夏令时 UTC-4): UTC 16:30 = 美东 12:30, 同日 (P1-2b)
    assert mining_schedule.market_date(instant) == date(2026, 8, 13)
    assert mining_schedule.iso_week(date(2027, 1, 1)) == (2026, 53)


def test_fingerprint_retries_generation_change_and_returns_stable_token(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    generations = iter([
        "generation-1",
        "generation-2",
        "generation-2",
        "generation-2",
    ])
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _asset: next(generations))

    fingerprint = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )

    assert fingerprint["generation"] == "generation-2"


def test_implementation_metadata_is_recursive_content_based_and_root_independent(
    tmp_path,
) -> None:
    roots = [tmp_path / "first" / "app", tmp_path / "second" / "app"]
    for root in roots:
        nested = root / "backtest"
        nested.mkdir(parents=True)
        (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (nested / "runtime.py").write_text("RESULT = 1\n", encoding="utf-8")
        (nested / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    first = mining_schedule._implementation_metadata(roots[0])
    second = mining_schedule._implementation_metadata(roots[1])
    (roots[1] / "backtest" / "runtime.py").write_text("RESULT = 2\n", encoding="utf-8")
    changed = mining_schedule._implementation_metadata(roots[1])

    assert first == second
    assert first["file_count"] == 2
    assert str(tmp_path) not in str(first)
    assert first["digest"] != changed["digest"]


def test_fingerprint_covers_result_implementation_digest(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    first = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )
    monkeypatch.setattr(
        mining_schedule,
        "_implementation_metadata",
        lambda _root: {"file_count": 1, "digest": "changed-runtime"},
    )
    second = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )

    assert first["implementation"] != second["implementation"]
    assert first["digest"] != second["digest"]


def test_selected_strategy_metadata_changes_with_same_size_source_edit(tmp_path) -> None:
    source = tmp_path / "strategies" / "custom" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    strategy = SimpleNamespace(execution_backend="matrix_native", file_path=source)
    state = SimpleNamespace(
        strategy_engine=SimpleNamespace(get=lambda _strategy_id: strategy)
    )

    first = mining_schedule._selected_strategy_metadata(
        state,
        ["demo"],
        tmp_path,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = mining_schedule._selected_strategy_metadata(
        state,
        ["demo"],
        tmp_path,
    )

    assert first[0]["source"]["size"] == second[0]["source"]["size"]
    assert first[0]["source"]["sha256"] != second[0]["source"]["sha256"]
    assert first[0]["source_tree"]["digest"] != second[0]["source_tree"]["digest"]


def test_fingerprint_rejects_continuously_changing_generation(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    generations = iter(["a", "b", "c", "d"])
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _asset: next(generations))

    with pytest.raises(ValueError, match="changed"):
        mining_schedule.build_data_fingerprint(
            repo,
            state,
            {"asset_type": "stock", "strategy_ids": []},
        )


def test_disabled_and_before_scheduled_weekday_do_not_enqueue(
    scheduled_state,
    monkeypatch,
):
    state = scheduled_state
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": False,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    assert mining_schedule.run_weekly_mining(state, now=_friday())["status"] == "disabled"

    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    thursday = _friday() - timedelta(days=1)
    assert mining_schedule.run_weekly_mining(state, now=thursday)["status"] == "weekday_mismatch"
    assert state.mining_manager.calls == []


def test_later_workday_catches_up_once_in_same_iso_week(scheduled_state, monkeypatch):
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 3,
            "mining_budget_profile": "balanced",
        },
    )

    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["status"] == "enqueued"
    assert second == {"status": "already_claimed", "run_id": first["run_id"]}
    assert len(scheduled_state.mining_manager.calls) == 1


def test_same_week_and_fingerprint_enqueue_once(scheduled_state):
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["status"] == "enqueued"
    assert second == {"status": "already_claimed", "run_id": first["run_id"]}
    assert len(scheduled_state.mining_manager.calls) == 1
    call = scheduled_state.mining_manager.calls[0]
    assert call["force"] is False
    assert call["source"] == "scheduled"
    assert call["run_id"] == first["run_id"]
    assert call["run_id"] == call["fingerprint"]["source_claim"]
    assert call["request"]["asset_type"] == "stock"
    assert call["request"]["symbols"] == POOL          # 有界主题池, 不再是全市场 (None)
    assert call["request"]["universe"]["max_symbols"] == 200
    assert call["request"]["strategy_ids"] == []
    assert call["request"]["require_regime"] is False  # P1-3b
    assert call["request"]["end"] == "2026-08-14"
    assert len(call["request"]["factor_names"]) <= 48


def test_profile_change_cannot_bypass_same_week_claim(scheduled_state, monkeypatch):
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    scheduled_state.mining_manager.store.transition_status(
        first["run_id"], "failed", error="worker failed"
    )
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "strict",
        },
    )

    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert second == {"status": "already_claimed", "run_id": first["run_id"]}
    assert len(scheduled_state.mining_manager.calls) == 1


def test_new_week_creates_new_claim_but_same_week_metadata_change_does_not(
    scheduled_state,
):
    manager = scheduled_state.mining_manager
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    next_week = mining_schedule.run_weekly_mining(scheduled_state, now=_friday(1))

    latest_file = (
        scheduled_state.repo.store.data_dir
        / "kline_daily_enriched"
        / "date=2026-08-14"
        / "part.parquet"
    )
    latest_file.write_bytes(b"changed-enriched-metadata")
    changed = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["run_id"] != next_week["run_id"]
    assert changed == {"status": "already_claimed", "run_id": first["run_id"]}
    assert len(manager.calls) == 2




def test_insufficient_data_records_visible_skipped_prerequisite(tmp_path, monkeypatch):
    repo = FakeRepo(tmp_path)
    manager = FakeManager(tmp_path)
    state = SimpleNamespace(repo=repo, mining_manager=manager, strategy_engine=None)
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "strict",
        },
    )
    _write_prerequisites(tmp_path, repo.latest, days=30)

    result = mining_schedule.run_weekly_mining(state, now=_friday())
    manifest = manager.store.get(result["run_id"])

    assert result["status"] == "skipped_prerequisite"
    assert manifest is not None
    assert manifest["status"] == "skipped_prerequisite"
    assert "insufficient" in manifest["error"]
    assert manager.calls == []


def test_pipeline_failure_does_not_trigger_mining(monkeypatch):
    mining_calls = []
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_args: False)
    monkeypatch.setattr(
        "app.services.mining_schedule.run_weekly_mining",
        lambda state: mining_calls.append(state),
    )

    daily_pipeline._scheduled_pipeline_task(lambda: None)

    assert mining_calls == []


def test_enqueue_failure_does_not_escape_successful_pipeline(monkeypatch):
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_args: True)

    def fail_enqueue(_state):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", fail_enqueue)

    daily_pipeline._scheduled_pipeline_task(lambda: None)


def test_empty_pool_is_skipped_instead_of_falling_back_to_full_market(scheduled_state, monkeypatch):
    from app.services import mining_pool

    monkeypatch.setattr(mining_pool, "build_mining_pool", lambda *_a, **_k: mining_pool.MiningPool(
        symbols=[], max_symbols=200, by_source={"watchlist": 0, "theme": 0, "liquidity": 0}))
    result = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    manifest = scheduled_state.mining_manager.store.get(result["run_id"])
    assert result["status"] == "skipped_prerequisite"
    assert "pool is empty" in manifest["error"]
    assert scheduled_state.mining_manager.calls == []


def test_weekly_scope_adds_merged_custom_factors_only(scheduled_state, tmp_path, monkeypatch):
    """0.47.0: factors the user merged into the library (active / watch) join the weekly scope;
    drafts and retired ones do not; never more than 48."""
    from app.factors import registry, store
    defs = [
        {"id": "uf_wk_active", "kind": "custom", "label": "a", "formula": "close / ts_delay(close, 5) - 1", "status": "active", "updated_at": "2026-11-01T00:00:00"},
        {"id": "uf_wk_watch", "kind": "custom", "label": "w", "formula": "close / ts_delay(close, 3) - 1", "status": "watch", "updated_at": "2026-11-02T00:00:00"},
        {"id": "uf_wk_draft", "kind": "custom", "label": "d", "formula": "close / ts_delay(close, 2) - 1", "status": "draft"},
        {"id": "uf_wk_retired", "kind": "custom", "label": "r", "formula": "close / ts_delay(close, 4) - 1", "status": "retired"},
    ]
    for d in defs:
        registry.unregister_factor(d["id"])
        store.register_definition(d)
        store.save_one(tmp_path, d)
    try:
        first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
        names = scheduled_state.mining_manager.calls[0]["request"]["factor_names"]
        assert first["status"] == "enqueued"
        assert "uf_wk_active" in names and "uf_wk_watch" in names
        assert "uf_wk_draft" not in names and "uf_wk_retired" not in names
        assert len(names) <= 48 and len(set(names)) == len(names)
        # room is the 48 cap minus the built-in cohort; newest first when it overflows
        assert mining_schedule.weekly_extra_factors(tmp_path, set(), 1) == ["uf_wk_watch"]
        assert mining_schedule.weekly_extra_factors(tmp_path, set(), 0) == []
    finally:
        for d in defs:
            registry.unregister_factor(d["id"])
