"""审计 2026-10-26 H-2: 美东零点后的开机补跑 / 失败重试必须给目标交易日定版, 不能把「今天」当收盘日。"""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.jobs import daily_pipeline as dp
from app.market_clock import us_clock

ET = us_clock.tz
SCHED = {"hour": 16, "minute": 35}


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


class _QS:
    def __init__(self):
        self.marked = []

    def mark_official_close(self, day):
        self.marked.append(day)
        return True

    def is_official_close(self, day):
        return day in self.marked


class _Loop:
    def __init__(self):
        self.calls = []
        self.opp_svc = SimpleNamespace(run_daily_scan=lambda d: SimpleNamespace(cards=[], watch=[], gate={}))
        self.ledger = SimpleNamespace(data_dir=Path("/nonexistent"), open_positions=[], read_events=lambda: [])

    def post_close_manage(self, day, confirmed_official=False):
        self.calls.append(day)
        return {"events": 0, "nav": {"nav": 1.0}, "missing_symbols": []}


def _stub_pipeline(monkeypatch, tmp_path, now, written_day):
    monkeypatch.setattr(us_clock, "now", lambda: now)
    monkeypatch.setattr(dp.instrument_sync, "sync_instruments", lambda d: 0)
    monkeypatch.setattr(dp, "_invalidate", lambda t=None: None)
    monkeypatch.setattr(dp, "_refresh_views", lambda repo: None)
    monkeypatch.setattr(dp, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(dp, "warn_if_enriched_too_thin", lambda d: 1460)
    monkeypatch.setattr(dp, "_resolve_universe", lambda capset, repo=None: ["AAPL", "SPY"])

    def fake_sync(universe, repo, capset, *, start_date, end_date, on_chunk_done=None, written_dates_out=None):
        if written_dates_out is not None:
            written_dates_out.add(written_day.isoformat())
        return 8000
    monkeypatch.setattr(dp.kline_sync, "sync_and_persist_daily_batch", fake_sync)
    monkeypatch.setattr(dp._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(dp._prefs, "get_adj_factor_provider", lambda: "")
    import app.services.data_integrity as di
    import app.services.index_sync as isync
    import app.services.notify_opportunity as no
    import app.services.preferences as prefs
    monkeypatch.setattr(prefs, "get_minute_sync_enabled", lambda: False)
    monkeypatch.setattr(di, "scan_recent_integrity", lambda *a, **k: [])
    monkeypatch.setattr(isync, "sync_core_index_from_stock", lambda repo, days, now=None: [])
    monkeypatch.setattr(no, "enqueue_daily_opportunities", lambda *a, **k: None)
    qs, loop = _QS(), _Loop()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: date(2026, 10, 21),
                           symbols_lagging=lambda d, min_gap_days=3: [], earliest_daily_date=lambda: date(2021, 1, 4))
    monkeypatch.setattr(dp, "_app_state_ref", SimpleNamespace(quote_service=qs, trading_loop=loop, repo=repo))
    return repo, qs, loop


class _Capset:
    def has(self, cap):
        return False


def test_retry_after_midnight_finalizes_target_day(tmp_path, monkeypatch):
    # 10-22 16:35 管道失败, 10-23 00:30 ET 重试 (美东已跨日)
    repo, qs, loop = _stub_pipeline(monkeypatch, tmp_path, datetime(2026, 10, 23, 0, 30, tzinfo=ET), date(2026, 10, 22))
    res = dp.run_now(repo, _Capset(), session_day=date(2026, 10, 22))
    assert res["zen_trade"]["status"] == "completed"
    assert qs.marked == [date(2026, 10, 22)] and loop.calls == [date(2026, 10, 22)]


def test_weekend_boot_finalizes_friday(tmp_path, monkeypatch):
    repo, _qs, loop = _stub_pipeline(monkeypatch, tmp_path, datetime(2026, 10, 24, 7, 58, tzinfo=ET), date(2026, 10, 23))
    res = dp.run_now(repo, _Capset(), session_day=date(2026, 10, 23))
    assert res["zen_trade"]["status"] == "completed" and loop.calls == [date(2026, 10, 23)]


def test_manual_run_without_session_day_keeps_today(tmp_path, monkeypatch):
    # 手动 / 16:35 正常路径不传 session_day: 仍按今天 (盘中手动跑不得偷跑昨日定版)
    repo, _qs, loop = _stub_pipeline(monkeypatch, tmp_path, datetime(2026, 10, 23, 0, 30, tzinfo=ET), date(2026, 10, 22))
    res = dp.run_now(repo, _Capset())
    assert res["zen_trade"]["status"] == "skipped" and loop.calls == []


def _failed(started="2026-10-22T20:35:00Z", finished="2026-10-22T20:35:40Z"):
    return {"id": "f", "status": "failed", "stage": "sync_daily", "started_at": started, "finished_at": finished,
            "result": None, "error": "scheduled daily_pipeline failed: URLError"}


def _sched(monkeypatch, tmp_path, jobs):
    from app.services import pipeline_jobs
    monkeypatch.setattr(dp.AsyncIOScheduler, "start", lambda self: None)
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None))
    monkeypatch.setattr(dp, "_app_state_ref", state)
    monkeypatch.setattr(dp._prefs, "get_pipeline_schedule", lambda: dict(SCHED))
    monkeypatch.setattr(pipeline_jobs.job_store, "list_recent", lambda n=20: list(jobs))
    monkeypatch.setattr(pipeline_jobs.job_store, "active_id", lambda: None)
    import app.services.preferences as prefs
    monkeypatch.setattr(prefs, "get_pipeline_schedule", lambda: dict(SCHED))
    seen = []

    def fake_run_now(repo, capset, on_progress=None, override_start_date=None, session_day=None):
        seen.append(session_day)
        return {"zen_trade": {"status": "completed"}}
    monkeypatch.setattr(dp, "run_now", fake_run_now)
    monkeypatch.setattr(dp, "_run_tracked", lambda fn, label: fn() is not None)
    monkeypatch.setattr(dp, "_run_data_quality", lambda s: None)
    monkeypatch.setattr(dp, "record_pipeline_catchup", lambda *a, **k: False)
    import app.services.mining_schedule as ms
    monkeypatch.setattr(ms, "run_weekly_mining", lambda s: None)
    return dp.start_scheduler(state.repo, MagicMock()), seen


def test_retry_wrapper_passes_target_day(monkeypatch, tmp_path):
    sched, seen = _sched(monkeypatch, tmp_path, [_failed()])
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 23, 0, 30, tzinfo=ET))
    assert sched.get_job("daily_pipeline_retry").func() is True
    assert seen == [date(2026, 10, 22)]


def test_boot_catchup_passes_target_day(monkeypatch, tmp_path):
    sched, seen = _sched(monkeypatch, tmp_path, [_failed("2026-10-23T20:35:00Z", "2026-10-23T20:35:40Z")])
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 24, 7, 58, tzinfo=ET))
    sched.get_job("daily_pipeline_catchup").func()
    assert seen == [date(2026, 10, 23)]


def test_boot_catchup_of_past_day_not_started_after_0830(monkeypatch, tmp_path):
    """复审: 开机补跑昨日在交易日 08:30 ET 起不再发起 (与失败重试同一条线), 免得定版拖进开盘。"""
    sched, seen = _sched(monkeypatch, tmp_path, [_failed()])
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 23, 8, 45, tzinfo=ET))
    sched.get_job("daily_pipeline_catchup").func()
    assert seen == []
    assert dp.catchup_too_close_to_open(datetime(2026, 10, 23, 8, 29, tzinfo=ET), date(2026, 10, 22)) is False
    assert dp.catchup_too_close_to_open(datetime(2026, 10, 24, 10, 0, tzinfo=ET), date(2026, 10, 23)) is False  # 周六不拦


def test_ops_ledger_note_uses_target_day(tmp_path, monkeypatch):
    """复审: 跨零点补跑成功后 catchup_ok 记在目标交易日, 盯盘清单该日不再显示失败。"""
    from app.services import ops_watch
    monkeypatch.setattr(dp._prefs, "get_pipeline_schedule", lambda: dict(SCHED))
    (tmp_path / "ops").mkdir(exist_ok=True)
    tue_2am = datetime(2026, 10, 27, 2, 0, tzinfo=ET)
    assert dp.record_pipeline_catchup(tmp_path, tue_2am) is False                       # 旧口径: 今天未到调度
    assert dp.record_pipeline_catchup(tmp_path, tue_2am, session_day=date(2026, 10, 26)) is True
    runs = ops_watch.load_runs(tmp_path, date(2026, 10, 26))
    assert [r["status"] for r in runs if r["job"] == "daily_pipeline"] == ["catchup_ok"]
    assert dp.record_pipeline_catchup(tmp_path, tue_2am, session_day=date(2026, 10, 26)) is False  # 只记一次
