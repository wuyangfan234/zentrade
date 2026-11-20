"""Regression tests for 5 adversarial code-review fixes (2026-11-20).

1. Half-day early close (e.g. 13:00 ET) must use us_clock.session_close(day)
   instead of hardcoded us_clock.sessions[-1].close (16:00).
2. DuckDB single-view refresh paths must acquire repo._lock via repo.refresh_view
   to avoid concurrent CatalogException against active readers.
3. swing_settlement.replay_card must not evaluate the post-15:30/12:30 bar on
   the final holding day after the time stop.
4. formula_miner.mutate must not emit degenerate constant formulas (x-x, x/x),
   and rank_lab must reject constant-output rankers with R010.
5. GET /api/kline/daily must only inject today's live candle when today falls
   within the requested [start, end] date window.
"""
from __future__ import annotations

import inspect
import random
import threading
from datetime import date, datetime, time
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock

ET = us_clock.tz
HALF = date(2026, 11, 27)
THX = date(2026, 11, 26)
PREV = date(2026, 11, 25)
NEXT = date(2026, 11, 30)


def _at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


@pytest.fixture
def halfday(monkeypatch):
    def cal(s: date, e: date) -> list[date]:
        out, d = [], s
        while d <= e:
            if d.weekday() < 5 and d != THX:
                out.append(d)
            d = date.fromordinal(d.toordinal() + 1)
        return out

    monkeypatch.setattr(us_clock, "_calendar_provider", cal)
    monkeypatch.setattr(us_clock, "_close_provider", lambda d: time(13, 0) if d == HALF else None)
    us_clock._cal_cache.clear()
    yield
    us_clock._cal_cache.clear()


# ── 1. Half-day early close ──────────────────────────────────────────────────


def test_half_day_post_close_manage_runs_and_pending_exit_executes_next_open(
    halfday, tmp_path, monkeypatch
):
    from app.ledger.core import Account, Ledger
    from app.services import decision_scan_job
    from app.services.trading_loop import TradingLoop

    lg = Ledger(tmp_path / "ledger")
    acct = Account(initial_equity=100_000.0)
    lg.init_account(acct)
    lg.append(
        "fill",
        {
            "symbol": "AAA",
            "side": "buy",
            "qty": 100,
            "price": 100.0,
            "fee": 0.0,
            "trade_date": PREV.isoformat(),
            "stop_price": 95.0,
            "invalidation": 95.0,
            "target_zone": [110.0, 112.0],
            "risk_per_share": 5.0,
            "max_holding_days": 5,
        },
        at=_at(PREV, 10, 0).isoformat(),
    )
    fake_repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_official_daily=lambda sym, s, e: pl.DataFrame(
            {"symbol": [sym], "date": [HALF], "raw_close": [90.0], "close": [90.0]}
        ),
        get_daily=lambda sym, s, e: pl.DataFrame(
            {"symbol": [sym], "date": [HALF], "raw_close": [90.0], "close": [90.0]}
        ),
    )
    loop = TradingLoop(fake_repo, acct)
    loop.boot()
    monkeypatch.setattr(decision_scan_job, "_watchlist", lambda: {"AAA"})
    monkeypatch.setattr(loop, "_sync_card_book", lambda: None)
    monkeypatch.setattr(loop, "_enqueue_events", lambda: None)
    monkeypatch.setattr(loop.opp_svc, "advance_lifecycle", lambda *a, **k: None)

    # Before 13:00 on a half day, post_close_manage must still skip.
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 12, 45))
    early = loop.post_close_manage(HALF, confirmed_official=True)
    assert early["skipped"] == "regular_session_not_closed"

    # At 13:30 ET (after 13:00 early close), post_close_manage must run and emit exit_intent.
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 13, 30))
    res = loop.post_close_manage(HALF, confirmed_official=True)
    assert res.get("skipped") is None
    assert res["events"] == 1
    assert res["nav"] is not None

    intents = [e for e in loop.ledger.read_events() if e["kind"] == "exit_intent"]
    assert len(intents) == 1
    assert intents[0]["reason"] == "invalidation"
    assert intents[0]["at"] == _at(HALF, 13, 0).isoformat()

    # Next trading day open: pending exit formed after 13:00 on HALF must execute.
    closed = loop.trader.execute_pending_exit(
        "AAA", open_price=88.0, trade_date=NEXT, at=_at(NEXT, 9, 31).isoformat()
    )
    assert closed is not None
    assert closed["kind"] == "close"
    assert loop.ledger.position("AAA") is None


def test_half_day_pending_exit_rejects_pre_close_intent_and_accepts_post_early_close(
    halfday,
):
    from app.trader.trader import SwingTrader

    plan = {"seq": 1, "trade_date": PREV.isoformat(), "at": _at(PREV, 10, 0).isoformat()}
    too_early = [
        {"kind": "exit_intent", "trade_date": HALF.isoformat(), "at": _at(HALF, 12, 59).isoformat()}
    ]
    assert SwingTrader._pending_exit(plan, too_early) is None

    after_half = [
        {"kind": "exit_intent", "trade_date": HALF.isoformat(), "at": _at(HALF, 13, 30).isoformat()}
    ]
    got = SwingTrader._pending_exit(plan, after_half)
    assert got is not None and got["at"] == _at(HALF, 13, 30).isoformat()


def test_half_day_close_gates_in_pipeline_quote_earnings_index_desk(
    halfday, tmp_path, monkeypatch
):
    from app.jobs import daily_pipeline as dp
    from app.services import desk_overview, earnings_guard, index_sync
    from app.services.quote_service import QuoteService

    # QuoteService.mark_official_close
    qs = QuoteService.__new__(QuoteService)
    qs._official_close_days = set()
    qs._official_close_path = lambda: tmp_path / "official_close_days.json"
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 12, 50))
    assert qs.mark_official_close(HALF) is False
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 13, 15))
    assert qs.mark_official_close(HALF) is True

    # daily_pipeline._run_zen_post_close must not skip on clock gate at 13:15 ET
    monkeypatch.setattr(dp, "_get_app_state_attr", lambda name: None)
    r = dp._run_zen_post_close(SimpleNamespace(), HALF)
    assert r == {"status": "skipped", "reason": "official_close_not_confirmed"}

    # earnings_guard.reduce_window_missed on half day
    ev = {"date": NEXT.isoformat(), "timing": "bmo"}
    assert earnings_guard.reduce_day(ev) == HALF
    assert earnings_guard.reduce_window_missed(ev, _at(HALF, 12, 59)) is False
    assert earnings_guard.reduce_window_missed(ev, _at(HALF, 13, 0)) is True

    # index_sync._closed on half day
    assert index_sync._closed(HALF, _at(HALF, 12, 59)) is False
    assert index_sync._closed(HALF, _at(HALF, 13, 0)) is True

    # desk_overview._after_close on half day
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 12, 59))
    assert desk_overview._after_close(HALF) is False
    monkeypatch.setattr(us_clock, "now", lambda: _at(HALF, 13, 0))
    assert desk_overview._after_close(HALF) is True


# ── 2. DuckDB view refresh locking ───────────────────────────────────────────


def test_view_refresh_helpers_acquire_repo_lock_and_survive_concurrent_reads(tmp_path):
    from app.jobs import daily_pipeline as dp
    from app.services import extend_history as eh
    from app.storage.repository import DataStore, KlineRepository

    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    p_day = tmp_path / "kline_daily" / "date=2026-11-19"
    p_day.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["AAA", "BBB"],
            "date": [date(2026, 11, 19)] * 2,
            "open": [100.0, 200.0],
            "high": [101.0, 201.0],
            "low": [99.0, 199.0],
            "close": [100.5, 200.5],
            "volume": [1000.0, 2000.0],
        }
    ).write_parquet(p_day / "part.parquet")
    p_inst = tmp_path / "instruments"
    p_inst.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["AAA", "BBB"], "name": ["A", "B"]}).write_parquet(
        p_inst / "part.parquet"
    )

    # Deterministic lock proof: while repo._lock is held, every refresh helper must block.
    for label, fn in (
        ("daily_pipeline._refresh_single_view", lambda: dp._refresh_single_view(repo, "kline_daily")),
        ("daily_pipeline._refresh_instruments_view", lambda: dp._refresh_instruments_view(repo)),
        ("extend_history._refresh_single_view", lambda: eh._refresh_single_view(repo, "kline_daily")),
    ):
        done = threading.Event()

        def _worker(call=fn, ev=done):
            call()
            ev.set()

        with repo._lock:
            t = threading.Thread(target=_worker)
            t.start()
            assert not done.wait(timeout=0.08), f"{label} bypassed repo._lock"
        t.join(timeout=5)
        assert done.is_set()

    # Source contract: no caller outside storage/repository.py may call repo.db.execute directly.
    from app.services import kline_sync as ks

    for mod in (dp, ks, eh):
        src = inspect.getsource(mod)
        assert "repo.db.execute(" not in src, f"{mod.__name__} still calls bare repo.db.execute("

    # Concurrent stress test: writer refreshes view while 2 readers query it.
    errors: list[tuple[str, str]] = []

    def _writer():
        for _ in range(80):
            dp._refresh_single_view(repo, "kline_daily")

    def _reader():
        for _ in range(80):
            try:
                rows = repo.execute_all("SELECT count(*) FROM kline_daily")
                assert rows and rows[0][0] == 2
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))
                return

    threads = [
        threading.Thread(target=_writer),
        threading.Thread(target=_reader),
        threading.Thread(target=_reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert errors == []


# ── 3. replay_card final-day post-15:30 lookahead ────────────────────────────


def test_replay_card_final_hold_day_does_not_look_past_time_stop():
    from app.services import swing_settlement as ss

    # Long: fills day 0 bar 1 near 99.5, flat at 101.0 through day 4 bar 11 (15:00-15:30),
    # then day 4 bar 12 (15:30-16:00) jumps above target=108.0. Must exit at 15:30 time stop.
    flat = [(101.0, 101.2, 100.8, 101.0)] * 13
    day0 = [(101.0, 101.2, 100.8, 101.0), (99.5, 100.0, 99.2, 99.8), *flat[2:]]
    day4_late_spike = [*flat[:12], (109.0, 110.0, 108.5, 109.5)]

    res_long = ss.replay_card("long", (99.0, 100.0), 95.0, 108.0, [day0, flat, flat, flat, day4_late_spike])
    assert res_long["status"] == "closed"
    assert res_long["why"] == "time"
    assert res_long["exit"] == pytest.approx(101.0 * (1 - ss.SLIP), abs=1e-4)

    # Half-day (7 bars): bar 5 is 12:00-12:30, bar 6 is 12:30-13:00 (after time stop).
    day4_half_spike = [*flat[:6], (109.0, 110.0, 108.5, 109.5)]
    res_half = ss.replay_card("long", (99.0, 100.0), 95.0, 108.0, [day0, flat, flat, flat, day4_half_spike])
    assert res_half["why"] == "time"
    assert res_half["exit"] == pytest.approx(101.0 * (1 - ss.SLIP), abs=1e-4)

    # Short mirror: final bar drops below target after 15:30 -> must still exit by time stop.
    s_flat = [(99.0, 99.2, 98.8, 99.0)] * 13
    s_day0 = [(99.0, 99.2, 98.8, 99.0), (100.5, 100.8, 100.2, 100.4), *s_flat[2:]]
    s_day4_drop = [*s_flat[:12], (91.0, 91.5, 90.0, 90.5)]
    res_short = ss.replay_card("short", (100.0, 101.0), 105.0, 92.0, [s_day0, s_flat, s_flat, s_flat, s_day4_drop])
    assert res_short["why"] == "time"
    assert res_short["exit"] == pytest.approx(99.0 * (1 + ss.SLIP), abs=1e-4)

    # Positive control: target hit on day 4 bar 11 (before 15:30) still closes as target.
    day4_pre_1530 = [*flat[:11], (101.0, 108.5, 101.0, 108.2), flat[12]]
    res_hit = ss.replay_card("long", (99.0, 100.0), 95.0, 108.0, [day0, flat, flat, flat, day4_pre_1530])
    assert res_hit["why"] == "target"


# ── 4. formula_miner degenerate mutations & rank_lab R010 constant guard ─────


def test_formula_miner_mutate_rejects_degenerate_x_minus_x_and_nonneg_ratio():
    from app.services import formula_miner as fm
    from app.services import rank_lab as rl

    parents = [
        "(close/ts_max(high,10)-1)-(close/ts_max(high,14)-1)",
        "ts_mean(((high-low)/close),10)/max(ts_mean(abs(((high-low)/close)),14),0.0001)",
        "ts_mean(abs(close/prev_close-1),5)/max(ts_mean(abs(abs(close/prev_close-1)),10),0.0001)",
        "ts_mean(high/max(low,0.0001)-1,5)/max(ts_mean(high/max(low,0.0001)-1,10),0.0001)",
    ]
    p0, _ = rl._pit_panels()
    for parent in parents:
        mutants = [m for s in range(100) if (m := fm.mutate(parent, random.Random(s))) is not None]
        assert mutants, f"expected valid non-degenerate mutations for {parent}"
        for m in set(mutants):
            assert rl.check_formula(m).ok
            vals = rl.factor_frame(p0, m, "high")["value"].drop_nulls()
            assert vals.n_unique() > 1, f"degenerate constant mutant emitted: {m}"

    # Signed atom equal-window efficiency ratio is non-constant and remains valid.
    signed = "ts_mean((close/prev_close-1),10)/max(ts_mean(abs((close/prev_close-1)),14),0.0001)"
    signed_mutants = {fm.mutate(signed, random.Random(s)) for s in range(100)}
    assert "ts_mean((close/prev_close-1),14)/max(ts_mean(abs((close/prev_close-1)),14),0.0001)" in signed_mutants


@pytest.mark.parametrize(
    "formula",
    [
        "(close/ts_max(high,14)-1)-(close/ts_max(high,14)-1)",
        "ts_mean(((high-low)/close),14)/max(ts_mean(abs(((high-low)/close)),14),0.0001)",
        "ts_sum(if_else((close>prev_close) and (close<prev_close),1,0),120)/120",
    ],
)
def test_rank_lab_check_formula_rejects_constant_formulas_with_r010(formula):
    from app.services import rank_lab as rl

    rl._PIT_CACHE.clear()
    with pytest.raises(rl.RankFormulaError) as exc:
        rl.check_formula(formula)
    assert exc.value.code == "R010"


def test_rank_lab_check_values_rejects_constant_matrix_with_r010(tmp_path):
    from test_rank_lab import _write_pool

    from app.services import rank_lab as rl

    data_dir, _, _ = _write_pool(tmp_path)
    pool = rl.load_pool(data_dir)
    assert pool is not None
    const_vals = np.ones((len(pool.days), len(pool.syms)), dtype=np.float64)
    with pytest.raises(rl.RankFormulaError) as exc:
        rl.check_values(pool, const_vals)
    assert exc.value.code == "R010"


# ── 5. /api/kline/daily historical range live candle guard ───────────────────


def test_kline_daily_only_injects_live_candle_within_requested_window(monkeypatch):
    from app.api import kline as kline_api

    today_et = date(2026, 11, 20)
    monkeypatch.setattr(kline_api.us_clock, "today", lambda: today_et)

    hist_df = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": [date(2025, 2, 27), date(2025, 2, 28)],
            "open": [120.0, 121.0],
            "high": [122.0, 123.0],
            "low": [119.0, 120.0],
            "close": [121.0, 122.0],
            "volume": [1_000_000.0, 1_000_000.0],
        }
    )
    recent_df = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "date": [date(2026, 11, 19)],
            "open": [180.0],
            "high": [182.0],
            "low": [179.0],
            "close": [181.0],
            "volume": [1_500_000.0],
        }
    )
    live_df = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "open": [190.0],
            "high": [195.0],
            "low": [189.0],
            "close": [194.0],
            "volume": [2_000_000.0],
            "amount": [380_000_000.0],
            "change_pct": [0.02],
        }
    )

    class _Repo:
        def resolve_asset_type(self, symbol: str) -> str:
            return "stock"

        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame()

        def get_daily_asset(self, asset_type: str, symbol: str, start: date, end: date) -> pl.DataFrame:
            if end < date(2026, 1, 1):
                return hist_df
            return recent_df.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    class _QS:
        def get_enriched_today(self):
            return live_df, today_et

    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = _Repo()
    app.state.quote_service = _QS()
    client = TestClient(app)

    # Historical query ending before today: must NOT append today's live candle.
    r_hist = client.get(
        "/api/kline/daily",
        params={"symbol": "AAA", "start_date": "2025-02-01", "end_date": "2025-02-28"},
    )
    assert r_hist.status_code == 200
    assert [str(row["date"]) for row in r_hist.json()["rows"]] == ["2025-02-27", "2025-02-28"]

    # Query covering today: MUST still append today's live candle.
    r_now = client.get(
        "/api/kline/daily",
        params={"symbol": "AAA", "start_date": "2026-11-01", "end_date": "2026-11-20"},
    )
    assert r_now.status_code == 200
    rows_now = r_now.json()["rows"]
    assert [str(row["date"]) for row in rows_now] == ["2026-11-19", "2026-11-20"]
    assert rows_now[-1]["is_live"] is True

    # Default query (no end_date): MUST still append today's live candle.
    r_def = client.get("/api/kline/daily", params={"symbol": "AAA"})
    assert r_def.status_code == 200
    assert str(r_def.json()["rows"][-1]["date"]) == "2026-11-20"
