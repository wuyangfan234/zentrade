"""审计 2026-10-26 第二批: 半日市 / 收盘口径 (M-1 M-2 L-1~L-6) + 行情任务链路 (M-4 M-5 M-6)。

半日市样例: 2026-11-27 (感恩节次日) 13:00 收盘, 入场截止 / 时间平仓 12:30; 11-26 感恩节休市。
"""
from datetime import date, datetime, time
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock

ET = us_clock.tz
HALF, THX, NEXT = date(2026, 11, 27), date(2026, 11, 26), date(2026, 11, 30)


@pytest.fixture
def halfday(monkeypatch):
    def cal(s, e):
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


def at(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


# ---------- M-2 机会板入场截止 ----------

def test_entry_close_and_board_entry_day_follow_half_day(halfday):
    from app.services import swing_desk as sd
    from app.services import swing_verdict as sv
    assert us_clock.entry_close(HALF) == time(12, 30) and us_clock.entry_close(NEXT) == time(15, 30)
    assert sv.entry_day(at(HALF, 12, 15)) == HALF
    assert sv.entry_day(at(HALF, 12, 45)) == NEXT          # 交易环 12:30 后不再入场, 机会板同口径
    assert sd._entry_day(at(HALF, 12, 45), HALF) == NEXT
    assert sd.window_over(2, HALF, at(HALF, 13, 5)) is True    # 第 2 天 13:00 已收盘
    assert sd.window_over(2, HALF, at(HALF, 12, 55)) is False
    assert sd._last_entry_close(date(2026, 11, 24)).replace(tzinfo=None) == datetime(2026, 11, 27, 13, 0)  # 第 2 个交易日 = 11-27 半日市


def test_refresh_window_ends_at_half_day_close(halfday):
    from app.services import swing_desk as sd
    assert sd.refresh_due(at(HALF, 12, 55)) is True
    assert sd.refresh_due(at(HALF, 13, 30)) is False       # L-4
    assert sd.refresh_due(at(NEXT, 15, 59)) is True


# ---------- M-1 收盘后到 16:35 的「休市」结论 ----------

def test_closed_verdict_next_day_is_next_trading_day_not_pipeline_day(halfday, monkeypatch):
    from app.services import opp_radar as orad
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_pipeline_schedule", lambda: {"hour": 16, "minute": 35})
    for now, want in ((at(date(2026, 10, 26), 16, 5), "2026-10-27"), (at(HALF, 13, 5), "2026-11-30")):
        ck = orad.clock(now)
        assert ck["next"]["day"] == now.date().isoformat()          # 下一事件 = 今天的盘后管道
        v = orad._verdict(now, ck, {}, [])
        assert v["code"] == "closed" and v["next_day"] == want


# ---------- L-1 半日市分钟K ----------

def test_minute_filter_and_expected_count_on_half_day(halfday):
    from app.services import kline_sync as ks
    rows = [datetime(2026, 11, 27, 9, 30) + __import__("datetime").timedelta(minutes=i) for i in range(390)]
    df = pl.DataFrame({"symbol": ["A"] * 390, "datetime": rows})
    assert ks._filter_regular_session(df).height == 210
    full = pl.DataFrame({"symbol": ["A"] * 390, "datetime": [r.replace(day=25) for r in rows]})
    assert ks._filter_regular_session(full).height == 390
    assert ks.minute_expected_count(HALF, at(HALF, 18, 0), delay_minutes=16) == 210


# ---------- L-2 行情时段 ----------

def test_quote_phase_and_monitor_window_on_half_day(halfday, monkeypatch):
    from app.services.quote_service import QuoteService
    for now, phase, cont in ((at(HALF, 12, 59), "regular", True), (at(HALF, 13, 30), "close_final", False),
                             (at(THX, 11, 0), None, False)):
        monkeypatch.setattr(us_clock, "now", lambda now=now: now)
        if phase:
            assert QuoteService._market_phase() == phase
        assert QuoteService._is_continuous_trading() is cont
    monkeypatch.setattr(us_clock, "now", lambda: at(HALF, 13, 10))
    assert datetime.fromtimestamp(QuoteService._final_boundary_ms("close_final") / 1000, ET).time() == time(13, 0)
    assert QuoteService._past_final_deadline("close_final") is False
    monkeypatch.setattr(us_clock, "now", lambda: at(HALF, 13, 31))
    assert QuoteService._past_final_deadline("close_final") is True


# ---------- L-3 盘中强势 5 分钟K ----------

def test_intraday_strength_drops_post_close_bars_on_half_day(halfday):
    from app.services import intraday_strength as ids
    ts = [at(HALF, 9, 30) + __import__("datetime").timedelta(minutes=5 * k) for k in range(78)]
    df = pl.DataFrame({"symbol": ["A"] * 78, "t": [t.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts],
                       **{c: [1.0] * 78 for c in ids.COLS}})
    assert ids._slot_frame(df).height == 42                # 09:30-12:55


# ---------- L-5 / L-6 ----------

def test_status_close_and_integrity_skip_holiday(halfday, tmp_path):
    from app.services import data_integrity as di
    from app.services import system_status_view as ssv
    assert ssv.market(at(HALF, 11, 0))["close"] == "2026-11-27T13:00-05:00"
    assert THX not in di._candidate_days(HALF, 7)
    assert date(2026, 11, 25) in di._candidate_days(HALF, 7)


# ---------- M-4 TickFlow 执行报价取时间 ----------

def test_tickflow_execution_quote_age_measured_after_response(monkeypatch):
    from app.plugins.tickflow import provider as tp
    t0 = at(date(2026, 10, 26), 10, 30)
    clock = {"t": t0}
    monkeypatch.setattr(us_clock, "now", lambda: clock["t"])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    p = tp.TickFlowProvider.__new__(tp.TickFlowProvider)
    p._record_tick = lambda *a, **k: None
    p._official_opens = lambda syms: {}

    def call(path, params, **kw):
        clock["t"] = t0.replace(second=1)                 # 请求耗时 1 秒, 报价是请求期间 0.5 秒时的成交
        return {"data": [{"symbol": "NVDA.US", "last_price": 180.0, "timestamp": int(t0.timestamp() * 1000) + 500}]}
    p._call = call
    out = p.get_execution_quotes(["NVDA"])
    assert [r["symbol"] for r in out] == ["NVDA"]


# ---------- M-5 定时管道被单飞跳过 ----------

def test_skipped_daily_pipeline_raises_and_retry_takes_over(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline as dp
    from app.services.pipeline_jobs import job_store
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    job_store.clear()
    other, _ = job_store.create(long_running=True)
    try:
        assert dp._run_tracked(lambda on_progress=None: {}, "daily_pipeline") is False
        assert dp._LAST_OUTCOME["daily_pipeline"] == "skipped"
    finally:
        job_store.fail(other, "test")
    thu = date(2026, 10, 22)
    sched = {"hour": 16, "minute": 35}
    skipped = at(thu, 16, 35)
    assert dp.pipeline_retry_due([], at(thu, 16, 45), sched) is None
    assert dp.pipeline_retry_due([], at(thu, 16, 51), sched, skipped_at=skipped) == thu
    assert dp.pipeline_retry_due([], at(thu, 16, 45), sched, skipped_at=skipped) is None     # 退避 15 分钟


def test_scheduled_daily_job_records_skipped_not_ok(monkeypatch, tmp_path):
    import asyncio
    import json
    from unittest.mock import MagicMock

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.jobs import daily_pipeline as dp
    from app.services import ops_watch
    from app.services.pipeline_jobs import job_store
    job_store.clear()
    other, _ = job_store.create(long_running=True)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    try:
        with monkeypatch.context() as m:
            m.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
            sched = dp.start_scheduler(state.repo, MagicMock())
        fn = sched.get_job("daily_pipeline").func

        async def main():
            s = AsyncIOScheduler(timezone="America/New_York")
            ops_watch.attach_listener(s, tmp_path)
            s.add_job(fn, trigger="date", run_date=datetime.now(ET), id="daily_pipeline")
            s.start()
            await asyncio.sleep(1.5)
            s.shutdown(wait=True)
        asyncio.run(main())
    finally:
        job_store.fail(other, "test")
    rows = [json.loads(x) for x in (tmp_path / "ops" / "jobs_run.jsonl").read_text().splitlines() if x.strip()]
    got = [r for r in rows if r["job"] == "daily_pipeline"]
    assert [r["status"] for r in got] == ["skipped"]                       # 不再记 ok
    assert "已有任务在运行" in got[0]["error"]


# ---------- M-6 导入并入分组不重置已有自选 ----------

def test_batch_add_existing_symbol_keeps_added_at_note_and_order(monkeypatch, tmp_path):
    from app.services import watchlist
    monkeypatch.setattr(watchlist, "_entries_path", lambda: tmp_path / "watchlist.parquet", raising=False)
    monkeypatch.setattr(watchlist, "_groups_path", lambda: tmp_path / "groups.json", raising=False)
    rows = [{"symbol": "NVDA", "added_at": "2026-09-01T14:00:00", "note": "核心仓", "group_ids": []},
            {"symbol": "AAPL", "added_at": "2026-09-02T14:00:00", "note": "", "group_ids": []}]
    store = {"rows": rows}
    monkeypatch.setattr(watchlist, "_read_entries", lambda: pl.DataFrame(store["rows"], schema=watchlist._ENTRY_SCHEMA))
    monkeypatch.setattr(watchlist, "_write_entries", lambda df: store.__setitem__("rows", df.to_dicts()))
    monkeypatch.setattr(watchlist, "_read_groups", lambda: [{"id": "g1", "name": "芯片"}])
    out, added = watchlist.add_batch(["AAPL", "NVDA", "TSLA"], group_ids=["g1"])
    assert added == 1
    assert [r["symbol"] for r in out] == ["TSLA", "NVDA", "AAPL"]          # 新票在最前, 旧票顺序不动
    nv = next(r for r in out if r["symbol"] == "NVDA")
    assert nv["added_at"] == "2026-09-01T14:00:00" and nv["note"] == "核心仓" and nv["group_ids"] == ["g1"]


# ---------- 复审修正 ----------

def test_card_volume_phase_and_snapshot_cutoff_follow_half_day(halfday):
    from app.services import card_volume as cv
    from app.services import data_integrity as di
    assert cv.phase(at(HALF, 12, 50))["phase"] == "session"
    assert cv.phase(at(HALF, 13, 30))["phase"] == "after"
    assert di._is_snapshot(HALF, int(at(HALF, 13, 5).timestamp() * 1000)) is False   # 13:00 后 = 定版快照
    assert di._is_snapshot(HALF, int(at(HALF, 12, 5).timestamp() * 1000)) is True


def test_settlement_m30_drops_post_close_bars_on_half_day(halfday, monkeypatch, tmp_path):
    import datetime as _dt

    from app.services import kline_4h
    from app.services import swing_settlement as ss
    ts = [at(HALF, 9, 30) + _dt.timedelta(minutes=30 * k) for k in range(13)]   # 09:30 ... 15:30
    raw = pl.DataFrame({"symbol": ["A"] * 13, "t": [t.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts],
                        "open": [1.0] * 13, "high": [1.0] * 13, "low": [1.0] * 13, "close": [1.0] * 13, "volume": [1.0] * 13})
    monkeypatch.setattr(kline_4h, "load_m30", lambda *a, **k: raw)
    monkeypatch.setattr(kline_4h, "_adj_factors", lambda *a, **k: pl.DataFrame())
    days = ss._m30_days(tmp_path, None, "A", HALF, HALF)
    assert len(days[HALF]) == 7                                                     # 09:30 ... 12:30


def test_skipped_pipeline_is_recorded_as_skipped_with_detail_and_not_alerted_while_active(monkeypatch, tmp_path):
    from app.services import ops_watch
    ev = SimpleNamespace(job_id="daily_pipeline", code=None, scheduled_run_time=at(date(2026, 10, 22), 16, 35))
    from app.jobs.daily_pipeline import PipelineNotRunError
    rows = []
    monkeypatch.setattr(ops_watch, "record_run", lambda d, j, status, **k: rows.append((status, k.get("error"))))
    from apscheduler.events import EVENT_JOB_ERROR
    sched = SimpleNamespace(add_listener=lambda fn, mask: setattr(sched, "fn", fn))
    ops_watch.attach_listener(sched, tmp_path)
    ev.code, ev.exception = EVENT_JOB_ERROR, PipelineNotRunError("skipped: 已有任务在运行 job_id=abc")
    sched.fn(ev)
    assert rows == [("skipped", "PipelineNotRunError: skipped: 已有任务在运行 job_id=abc")]


def test_checklist_skipped_waits_while_active_then_alerts(monkeypatch, tmp_path):
    from app.services import ops_watch
    fire = at(date(2026, 10, 22), 16, 35)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(ops_watch, "due_today", lambda s, now: [("daily_pipeline", fire)])
    monkeypatch.setattr(ops_watch, "is_running", lambda j, now: False)
    ops_watch.record_run(tmp_path, "daily_pipeline", "skipped", scheduled=fire, error="PipelineNotRunError: skipped", now=fire)
    monkeypatch.setattr(ops_watch, "_job_active", lambda: True)
    assert ops_watch.checklist(tmp_path, None, at(date(2026, 10, 22), 18, 0))[0]["state"] == "running"
    monkeypatch.setattr(ops_watch, "_job_active", lambda: False)
    assert ops_watch.checklist(tmp_path, None, at(date(2026, 10, 22), 16, 50))[0]["state"] == "running"   # 30 分钟宽限
    assert ops_watch.checklist(tmp_path, None, at(date(2026, 10, 22), 17, 30))[0]["state"] == "error"
