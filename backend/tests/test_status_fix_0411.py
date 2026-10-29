"""0.41.1: 状态页「盘后管道 注意」「个股新闻 注意」误报 (10-27 盘中重部署实例)。

1. 收盘前 SPAC/权证类零星实时行行数超 1% 但成交额可忽略 → 不判快照 (否则盘中重部署触发修复管道)。
2. 盘中修复管道 zen_trade=skipped, 但上一交易日已定版 → 盘后管道显示 ok。
3. 翻译单轮偶发失败批次 (下一轮自愈) 不标黄; 连续多轮才标黄。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_time import CN_TZ
from app.services.data_integrity import scan_recent_integrity

TODAY = date(2026, 8, 24)
FRIDAY = date(2026, 8, 21)


def _ts(day, t):
    return int(datetime.combine(day, t, tzinfo=CN_TZ).timestamp() * 1000)


def _part(root, day, n_batch, live, *, batch_px=50.0, batch_vol=1_000_000.0, live_px=10.0, live_vol=20.0):
    part = root / "kline_daily" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    syms = [f"B{i:04d}" for i in range(n_batch)] + [s for s, _ in live]
    pl.DataFrame({
        "symbol": syms, "date": [day] * len(syms),
        "open": [1.0] * len(syms), "high": [1.0] * len(syms), "low": [1.0] * len(syms),
        "close": [batch_px] * n_batch + [live_px] * len(live),
        "volume": [batch_vol] * n_batch + [live_vol] * len(live),
        "amount": [batch_px * batch_vol] * n_batch + [live_px * live_vol] * len(live),
        "quote_ts": [None] * n_batch + [_ts(day, t) for _, t in live],
    }).write_parquet(part / "part.parquet")


def _today(root):
    _part(root, TODAY, 1, [("WFC", time(10, 0))])


def test_many_tiny_live_rows_with_negligible_dollar_volume_is_clean(tmp_path):
    """10-26 重现: 7812 行权威 + 140 行 09:30-15:59 的 SPAC/权证实时行 (1.79%), 成交额占比 0.00004%。"""
    live = [(f"SPAC{i:03d}", time(9 + i % 7, 30)) for i in range(140)]
    _part(tmp_path, FRIDAY, 7812, live)
    _today(tmp_path)
    assert scan_recent_integrity(tmp_path, today=TODAY) == []


def test_midday_shutdown_with_real_dollar_volume_is_still_flagged(tmp_path):
    """反例: 行数超 1% 且实时行有真实成交额 (大票盘中快照) → 仍判快照。"""
    live = [(f"BIG{i:03d}", time(11, 58)) for i in range(800)]
    _part(tmp_path, FRIDAY, 7000, live, live_px=200.0, live_vol=500_000.0)
    _today(tmp_path)
    assert [(i.day, i.kind) for i in scan_recent_integrity(tmp_path, today=TODAY)] == [(FRIDAY, "snapshot")]


def test_small_partition_still_not_relaxed_by_dollar_rule(tmp_path):
    _part(tmp_path, FRIDAY, 50, [("X", time(15, 58))])
    _today(tmp_path)
    assert [(i.day, i.kind) for i in scan_recent_integrity(tmp_path, today=TODAY)] == [(FRIDAY, "snapshot")]


# ── 状态页 ──────────────────────────────────────────────────────────


def _app(tmp_path, monkeypatch, jobs, now):
    from app.api.trading import router
    from app.market_clock import us_clock
    from app.services import preferences
    from app.services.pipeline_jobs import job_store
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 16, "minute": 35})
    monkeypatch.setattr(job_store, "list_recent", lambda n=20: list(jobs))
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = None
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _row(body, cid):
    return next(c for c in body["components"] if c["id"] == cid)


def _job(jid, started, finished, zen):
    return {"id": jid, "status": "succeeded", "started_at": started, "finished_at": finished,
            "result": {"daily_days": 1, "zen_trade": zen, "stage_errors": []}}


REPAIR = _job("9c05", "2026-10-27T18:42:20Z", "2026-10-27T18:53:46Z",
              {"status": "skipped", "reason": "regular_session_not_closed"})
FINAL_1026 = _job("9911", "2026-10-26T20:35:00Z", "2026-10-26T20:46:03Z",
                  {"status": "completed", "cards": 0, "watch": 252})


def test_midday_repair_after_yesterday_finalized_is_ok(tmp_path, monkeypatch):
    from app.market_clock import us_clock
    now = datetime(2026, 10, 27, 15, 13, tzinfo=us_clock.tz)
    body = _app(tmp_path, monkeypatch, [REPAIR, FINAL_1026], now).get("/api/trading/system-status").json()
    row = _row(body, "daily_pipeline")
    assert row["state"] == "ok", row
    assert "盘中数据修复" in row["summary"] and "观察 252" in row["summary"]


def test_after_todays_schedule_without_finalize_still_warns(tmp_path, monkeypatch):
    """今天 16:35 后仍只有盘中修复 (今天没定版) → 照样 warn, 不掩盖真缺口。"""
    from app.market_clock import us_clock
    now = datetime(2026, 10, 27, 17, 30, tzinfo=us_clock.tz)
    body = _app(tmp_path, monkeypatch, [REPAIR, FINAL_1026], now).get("/api/trading/system-status").json()
    assert _row(body, "daily_pipeline")["state"] == "warn"


def _news_state(tmp_path, **extra):
    d = tmp_path / "news"
    d.mkdir(parents=True, exist_ok=True)
    st = {"last_run_at": "2026-10-27T19:10:06Z", "universe": 400, "ingest": {"raw": 1, "kept": 1, "added": 1},
          "ingest_errors": [], "translate": {"llm": 0, "fallback": 2, "failed_batches": 1, "pending_after": 10}, **extra}
    (d / "state.json").write_text(json.dumps(st))


def test_single_round_translate_failure_is_not_warn(tmp_path, monkeypatch):
    from app.market_clock import us_clock
    _news_state(tmp_path, translate_fail_streak=1)
    now = datetime(2026, 10, 27, 15, 13, tzinfo=us_clock.tz)
    row = _row(_app(tmp_path, monkeypatch, [], now).get("/api/trading/system-status").json(), "news")
    assert row["state"] == "ok", row
    assert "下一轮自动重译" in row["summary"]


def test_consecutive_translate_failures_warn(tmp_path, monkeypatch):
    from app.market_clock import us_clock
    _news_state(tmp_path, translate_fail_streak=3)
    now = datetime(2026, 10, 27, 15, 13, tzinfo=us_clock.tz)
    row = _row(_app(tmp_path, monkeypatch, [], now).get("/api/trading/system-status").json(), "news")
    assert row["state"] == "warn" and "连续 3 轮" in row["summary"]


def test_news_tick_tracks_translate_fail_streak(tmp_path, monkeypatch):
    from app.services import news_ingest as ni
    from app.services import news_job, news_translate
    now = datetime(2026, 10, 2, 18, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(news_job, "universe", lambda loop=None, core_only=False: ["LITE"])
    monkeypatch.setattr(ni, "ingest", lambda *a, **k: {"raw": 0, "kept": 0, "added": 0, "errors": []})
    seq = iter([1, 1, 0])
    monkeypatch.setattr(news_translate, "translate_pending_sync",
                        lambda d, **kw: {"llm": 1, "fallback": 0, "failed_batches": next(seq), "pending_after": 0})
    streaks = []
    for _ in range(3):
        news_job.tick(tmp_path, None, None, now=now, force=True)
        streaks.append(json.loads((tmp_path / "news" / "state.json").read_text())["translate_fail_streak"])
    assert streaks == [1, 2, 0]
