"""Audit 2026-10-26 batch 3a: backtest engine numbers (M-12 M-13 M-14 M-15 L-7)."""
import asyncio
import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI

from app.api import backtest as api
from app.backtest import worker
from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions, TradeRecord
from app.backtest.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)
from app.backtest.walkforward import aggregate_oos, generate_folds
from app.config import settings

# ---------------------------------------------------------------- M-12

_ROWS = [
    (date(2026, 1, 5), 100.0, 100.0, 100.0, 100.0),
    (date(2026, 1, 6), 100.0, 110.0, 99.0, 100.0),  # signal day: high 110 before the close fill
    (date(2026, 1, 7), 100.5, 101.0, 100.0, 100.8),
    (date(2026, 1, 8), 101.0, 102.0, 100.5, 101.5),
    (date(2026, 1, 9), 101.5, 102.0, 101.0, 102.0),
]


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["AAA"] * len(_ROWS),
        "name": ["AAA"] * len(_ROWS),
        "date": [r[0] for r in _ROWS],
        "open": [r[1] for r in _ROWS],
        "high": [r[2] for r in _ROWS],
        "low": [r[3] for r in _ROWS],
        "close": [r[4] for r in _ROWS],
        "volume": [1_000_000.0] * len(_ROWS),
        "score": [50.0] * len(_ROWS),
        "signal_limit_up": [False] * len(_ROWS),
        "signal_limit_down": [False] * len(_ROWS),
    })


def _cfg(**risk) -> MatcherConfig:
    return MatcherConfig(
        matching="close_t", entry_fill="close_t", exit_fill="close_t",
        cost_model="us_per_share_v1", commission_per_share=0.0, commission_min=0.0,
        slippage_bps=0.0, max_positions=1, initial_capital=10_000.0, **risk,
    )


def _matrix():
    market = build_market_data_matrix(_panel())
    entry = np.zeros(market.shape, dtype=np.uint8)
    entry[1, 0] = 1
    score = np.full(market.shape, 50.0, dtype=np.float32)
    signals = make_signal_matrix(market.shape, entry=entry, score=score)
    return build_market_matrix_from_signals(market, signals, entry_delay_bars=0, exit_delay_bars=0)


_RISKS = {
    "trailing_stop": {"trailing_stop_pct": 0.05},
    "trailing_tp": {
        "trailing_take_profit_activate_pct": 0.05,
        "trailing_take_profit_drawdown_pct": 0.03,
    },
}


@pytest.mark.parametrize("risk", list(_RISKS))
@pytest.mark.parametrize("mode", ["position", "full", "position_legacy", "full_legacy"])
def test_m12_close_t_entry_peak_excludes_pre_fill_high(mode, risk):
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    cfg = _cfg(**_RISKS[risk])
    if mode == "position":
        res = engine.simulate_market_matrix(_matrix(), cfg)
    elif mode == "full":
        res = engine.simulate_independent_market_matrix(_matrix(), 1, cfg)
    else:
        panel = _panel()
        entries = pl.Series([d == date(2026, 1, 6) for d in panel["date"]])
        exits = pl.Series([False] * panel.height)
        fn = (
            engine.simulate_portfolio_legacy
            if mode == "position_legacy"
            else engine.simulate_independent_candidates_legacy
        )
        res = fn(panel, entries, exits, cfg)
    assert len(res.trades) == 1
    trade = res.trades[0]
    assert trade.entry_price == 100.0
    assert trade.exit_reason == "end"
    assert str(trade.exit_date)[:10] == "2026-01-09"
    assert abs(trade.pnl_pct - 0.02) < 1e-9


def test_m12_open_entry_still_counts_entry_day_high():
    """open_t+1 fills at the open, so the entry-day high is after the fill and counts."""
    rows = [
        (date(2026, 1, 5), 100.0, 100.0, 100.0, 100.0),  # signal
        (date(2026, 1, 6), 100.0, 110.0, 99.0, 100.0),  # fill at open 100, then high 110
        (date(2026, 1, 7), 100.5, 101.0, 100.0, 100.8),
        (date(2026, 1, 8), 101.0, 102.0, 100.5, 101.5),
    ]
    panel = pl.DataFrame({
        "symbol": ["AAA"] * 4, "date": [r[0] for r in rows],
        "open": [r[1] for r in rows], "high": [r[2] for r in rows],
        "low": [r[3] for r in rows], "close": [r[4] for r in rows],
        "volume": [1_000_000.0] * 4,
    })
    market = build_market_data_matrix(panel)
    entry = np.zeros(market.shape, dtype=np.uint8)
    entry[0, 0] = 1
    signals = make_signal_matrix(
        market.shape, entry=entry, score=np.full(market.shape, 50.0, dtype=np.float32)
    )
    matrix = build_market_matrix_from_signals(market, signals, entry_delay_bars=1, exit_delay_bars=0)
    cfg = MatcherConfig(
        matching="open_t+1", entry_fill="open_t+1", exit_fill="close_t",
        cost_model="us_per_share_v1", commission_per_share=0.0, commission_min=0.0,
        slippage_bps=0.0, max_positions=1, initial_capital=10_000.0, trailing_stop_pct=0.05,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    for res in (
        engine.simulate_market_matrix(matrix, cfg),
        engine.simulate_independent_market_matrix(matrix, 1, cfg),
    ):
        assert [t.exit_reason for t in res.trades] == ["trailing_stop"]
        assert str(res.trades[0].exit_date)[:10] == "2026-01-07"


# ---------------------------------------------------------------- M-13

ROUTES = {
    "backtest": "/api/backtest/strategy/stream",
    "optimize": "/api/backtest/optimize/stream",
    "walkforward": "/api/backtest/walkforward/stream",
}


async def _invoke(app, path, params, timeout=2.0):
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(),
        "query_string": urlencode(params).encode(), "root_path": "",
        "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 50000), "server": ("test", 80),
    }
    bodies: list[str] = []
    first = True
    never = asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        raise AssertionError("unreachable")

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            bodies.append(message["body"].decode())

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    except TimeoutError:
        bodies.append("<timeout>")
    return bodies


@pytest.fixture
def sse_app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(api, "_running_jobs", {})
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(
        earliest_daily_date=lambda: date(2020, 1, 1),
        earliest_minute_date=lambda: None,
    )
    app.state.strategy_engine = None
    return app


def _sse_params(kind):
    params = {"strategy_id": f"b3a_{kind}", "symbols": "AAA", "start": "2026-01-05", "end": "2026-03-31"}
    if kind != "backtest":
        params["param_grid"] = '{"n": [5]}'
    return params


@pytest.mark.parametrize("kind", list(ROUTES))
def test_m13_failed_job_is_not_replayed_on_same_key_retry(sse_app, kind, monkeypatch):
    starts: list[str] = []

    def fake_run(task, progress=None, cancel_event=None):
        starts.append(task["kind"])
        if len(starts) == 1:
            raise RuntimeError("transient publish in progress")
        return {"ok": True, "stats": {"total_return": 0.1}, "error": None}

    monkeypatch.setattr(worker, "run_worker_task", fake_run)

    async def exercise():
        first = await _invoke(sse_app, ROUTES[kind], _sse_params(kind))
        assert any("transient publish" in b for b in first)
        second = await _invoke(sse_app, ROUTES[kind], _sse_params(kind))
        assert len(starts) == 2
        assert any("event: done" in b for b in second)

    asyncio.run(exercise())


def test_m13_cancelled_backtest_restarts_on_same_key(sse_app, monkeypatch):
    starts: list[str] = []
    started = threading.Event()

    def fake_run(task, progress=None, cancel_event=None):
        starts.append(task["kind"])
        if len(starts) == 1:
            started.set()
            cancel_event.wait(2.0)
            return {"error": "cancelled", "stats": {}}
        return {"stats": {"total_return": 0.2}, "error": None}

    monkeypatch.setattr(worker, "run_worker_task", fake_run)
    params = _sse_params("backtest")

    async def exercise():
        task = asyncio.create_task(_invoke(sse_app, ROUTES["backtest"], params))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 2.0)
        job = next(iter(api._running_jobs.values()))
        job.cancel_event.set()
        first = await task
        assert any("回测已取消" in b for b in first)
        second = await _invoke(sse_app, ROUTES["backtest"], params)
        assert len(starts) == 2
        assert any("event: done" in b for b in second)

    asyncio.run(exercise())


def test_m13_successful_job_still_replayed_for_reconnect(sse_app, monkeypatch):
    starts: list[str] = []

    def fake_run(task, progress=None, cancel_event=None):
        starts.append(task["kind"])
        return {"stats": {"total_return": 0.1}, "error": None}

    monkeypatch.setattr(worker, "run_worker_task", fake_run)

    async def exercise():
        await _invoke(sse_app, ROUTES["backtest"], _sse_params("backtest"))
        second = await _invoke(sse_app, ROUTES["backtest"], _sse_params("backtest"))
        assert len(starts) == 1
        assert any("event: done" in b for b in second)

    asyncio.run(exercise())


# ---------------------------------------------------------------- M-14

def _wf_records(train, test, step, daily=0.001):
    folds = generate_folds(date(2026, 1, 1), date(2026, 12, 31), train, test, step)
    records = [
        {
            "index": f.index,
            "test_start": str(f.test_start),
            "test_end": str(f.test_end),
            "is_score": 1.0,
            "oos_objective": 1.0,
            "oos_stats": {"total_return": (1 + daily) ** ((f.test_end - f.test_start).days + 1) - 1},
        }
        for f in folds
    ]
    return folds, records


def test_m14_overlapping_oos_windows_are_not_compounded_twice():
    folds, records = _wf_records(60, 63, 21)
    assert len(folds) == 12  # fold layout unchanged
    summary = aggregate_oos(records, "sharpe")
    chained = [r for r in records if r["index"] % 3 == 0]
    expected = 1.0
    for r in chained:
        expected *= 1 + r["oos_stats"]["total_return"]
    span = (folds[-1].test_end - folds[0].test_start).days + 1
    assert summary["n_folds"] == 12
    assert summary["n_compounded_folds"] == len(chained)
    assert summary["compounded_oos_basis"] == "non_overlapping"
    assert abs(summary["compounded_oos_return"] - round(expected - 1, 4)) < 1e-9
    assert summary["compounded_oos_return"] <= (1.001 ** span - 1) + 1e-3
    assert [p["fold"] for p in summary["oos_equity_curve"]] == [r["index"] for r in chained]


def test_m14_step_equal_test_keeps_every_fold():
    _, records = _wf_records(60, 63, 63)
    summary = aggregate_oos(records, "sharpe")
    expected = 1.0
    for r in records:
        expected *= 1 + r["oos_stats"]["total_return"]
    assert summary["n_compounded_folds"] == len(records)
    assert abs(summary["compounded_oos_return"] - round(expected - 1, 4)) < 1e-9


# ---------------------------------------------------------------- M-15

def _trade(day, pnl):
    return TradeRecord(
        symbol="A", entry_date=date(2026, 1, 2), exit_date=day, entry_price=100,
        exit_price=100 * (1 + pnl), pnl_pct=pnl, duration=1, exit_reason="signal",
    )


def test_m15_full_mode_max_drawdown_uses_initial_peak():
    trades = [_trade(date(2026, 1, 5), -0.10), _trade(date(2026, 1, 6), 0.05)]
    res = BacktestEngine._calc_independent_candidate_result(trades, 2, {}, options=SimulationOptions())
    assert res.stats["max_drawdown"] == -0.1
    assert res.stats["max_drawdown"] == min(p["value"] for p in res.drawdown_curve)


def test_m15_position_mode_max_drawdown_uses_initial_capital():
    stats = BacktestEngine._calc_portfolio_stats_from_values(
        [900_000.0, 945_000.0, 950_000.0], [0.9, 0.9, 0.9],
        [_trade(date(2026, 1, 7), -0.05)], 1_000_000.0, include_monte_carlo=False,
    )
    assert stats["max_drawdown"] == -0.1


def test_m15_position_mode_curve_and_stats_agree():
    panel = pl.DataFrame({
        "symbol": ["AAA"] * 4, "name": ["AAA"] * 4,
        "date": [date(2026, 1, 5) + timedelta(days=i) for i in range(4)],
        "open": [100.0, 100.0, 90.0, 95.0], "high": [100.0, 100.0, 90.0, 95.0],
        "low": [100.0, 100.0, 90.0, 95.0], "close": [100.0, 90.0, 92.0, 95.0],
        "volume": [1_000_000.0] * 4, "score": [50.0] * 4,
        "signal_limit_up": [False] * 4, "signal_limit_down": [False] * 4,
    })
    entries = pl.Series([True, False, False, False])
    exits = pl.Series([False] * 4)
    cfg = MatcherConfig(
        matching="close_t", cost_model="us_per_share_v1", commission_per_share=0.0,
        commission_min=0.0, slippage_bps=0.0, max_positions=1, initial_capital=10_000.0,
    )
    res = BacktestEngine(repo=None).simulate_portfolio(panel, entries, exits, cfg)  # type: ignore[arg-type]
    assert res.stats["max_drawdown"] == pytest.approx(min(p["value"] for p in res.drawdown_curve), abs=1e-4)
    assert res.stats["max_drawdown"] < 0


# ---------------------------------------------------------------- L-7

def _wrapped_parquet_error() -> ValueError:
    import app.backtest.engine as eng
    from app.backtest.strategy import ResolvedFeaturePlan

    def boom(*_a, **_k):
        raise OSError("Couldn't deserialize thrift: TProtocolException: Invalid data")

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=Path("/nonexistent")),
        get_instruments_asset=lambda _a: None,
        get_matrix_data_generation=lambda _a: "gen-1",
    )
    cols = frozenset({"symbol", "date", "close"})
    plan = ResolvedFeaturePlan(
        base_columns=cols, intermediate_columns=frozenset(), indicator_columns=frozenset(),
        signal_columns=frozenset(), matrix_columns=cols, instrument_columns=frozenset(),
        warmup_bars=1, execution_backend="matrix_native",
    )
    original = eng.load_market_data_matrix_from_parquet
    eng.load_market_data_matrix_from_parquet = boom
    try:
        BacktestEngine(repo).load_market_data_matrix_for_backtest(
            None, date(2020, 1, 1), date(2026, 10, 23), plan, asset_type="stock"
        )
    except ValueError as exc:
        return exc
    finally:
        eng.load_market_data_matrix_from_parquet = original
    raise AssertionError("expected wrapped ValueError")


def test_l7_prewarm_retries_engine_wrapped_oserror():
    from app import main
    from app.enriched_generation import EnrichedGenerationUnavailableError

    wrapped = _wrapped_parquet_error()
    assert isinstance(wrapped.__cause__, OSError)
    assert main._prewarm_retryable(wrapped)
    assert main._prewarm_retryable(OSError("thrift"))
    assert main._prewarm_retryable(EnrichedGenerationUnavailableError("busy"))
    assert not main._prewarm_retryable(ValueError("unsupported matrix feature"))
    assert not main._prewarm_retryable(RuntimeError("boom"))
