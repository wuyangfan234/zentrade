"""0.7.1 系统状态页只读聚合: 5 种场景 + 只读约束。"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.trading import router
from app.market_clock import us_clock
from app.services import ops_watch

ET = us_clock.tz


@pytest.fixture(autouse=True)
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _client(tmp_path, monkeypatch, now, loop_st=None, sched_jobs=(), ops=None):
    monkeypatch.setattr(us_clock, "now", lambda: now)
    if ops is not None:
        monkeypatch.setattr(ops_watch, "status", lambda d, s, lp, now=None: ops)
    app = FastAPI()
    app.include_router(router)
    st = loop_st if loop_st is not None else {"booted": True, "last_error": None, "last_run_at": None,
                                              "execution_source": {"active": "tickflow"}}
    app.state.trading_loop = SimpleNamespace(status=lambda: st)
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: list(sched_jobs), get_job=lambda j: None)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _c(body, cid):
    return next(c for c in body["components"] if c["id"] == cid)


def _snapshot(tmp_path):
    return {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}


def test_premarket_nothing_red_and_waiting(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 7, 30, tzinfo=ET)
    body = _client(tmp_path, monkeypatch, now, ops={"checklist": [], "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json()
    assert body["market"]["phase"] == "premarket" and body["market"]["trading_day"] is True
    ops = _c(body, "ops_watch")
    assert ops["state"] == "idle" and ops["idle_kind"] == "waiting" and "等待" in ops["summary"]   # 不再"应跑 0 项"显绿
    assert _c(body, "premarket_gap")["idle_kind"] == "waiting"
    assert _c(body, "paper_loop")["state"] == "idle"
    for cid in ("paper_loop", "execution_quotes", "hot_quotes", "ops_watch", "premarket_gap"):
        assert _c(body, cid)["state"] != "error", cid


def test_session_ok_and_pending_running_visible(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 10, 0, tzinfo=ET)
    (tmp_path / "premarket_gap").mkdir()
    (tmp_path / "premarket_gap" / "2026-10-12.json").write_text(json.dumps({"day": "2026-10-12", "checked": 3}))
    ck = [{"job": "zen_market_mood", "label": "盘前大盘情绪", "due": "2026-10-12T08:35-04:00", "state": "ok",
           "last": {"at": "2026-10-12T08:36:00-04:00"}},
          {"job": "zen_qveris_estimates", "label": "QVeris 财报预期", "due": "2026-10-12T09:50-04:00", "state": "running", "last": None}]
    jobs = [SimpleNamespace(id="zen_market_mood", next_run_time=None), SimpleNamespace(id="zen_qveris_estimates", next_run_time=None)]
    st = {"booted": True, "last_error": None, "last_run_at": (now - timedelta(seconds=20)).isoformat(),
          "execution_source": {"active": "tickflow"}}
    body = _client(tmp_path, monkeypatch, now, st, jobs, {"checklist": ck, "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json()
    assert body["market"]["phase"] == "session"
    assert _c(body, "paper_loop")["state"] == "ok" and _c(body, "paper_loop")["since"]
    ops = _c(body, "ops_watch")
    assert ops["state"] == "ok" and "进行中 1" in ops["summary"]
    j = {x["id"]: x for x in body["jobs"]}
    assert j["zen_qveris_estimates"]["state"] == "running" and j["zen_market_mood"]["last_at"].startswith("2026-10-12T08:36")


def test_session_loop_stalled_is_red_with_impact_action(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 10, 42, tzinfo=ET)
    st = {"booted": True, "last_error": "HTTPError https://x.io/q?apikey=SEKRIT", "last_run_at": (now - timedelta(minutes=6)).isoformat(),
          "execution_source": {"active": "tickflow"}}
    body = _client(tmp_path, monkeypatch, now, st, ops={"checklist": [], "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json()
    pl = _c(body, "paper_loop")
    assert pl["state"] == "error" and pl["impact"] and pl["action"] and pl["since"]
    assert "重启" not in pl["action"].replace("不要重启", "")              # 下一步只写查看/排查
    assert "SEKRIT" not in json.dumps(body)
    assert body["components"][0]["state"] == "error"


def test_session_loop_stalled_by_staleness_only(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 10, 42, tzinfo=ET)
    st = {"booted": True, "last_error": None, "last_run_at": (now - timedelta(minutes=4)).isoformat(),
          "execution_source": {"active": "tickflow"}}
    pl = _c(_client(tmp_path, monkeypatch, now, st, ops={"checklist": [], "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json(), "paper_loop")
    assert pl["state"] == "error" and pl["stale"] and "4 分钟" in pl["summary"]


def test_postmarket_data_quality_error(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 18, 0, tzinfo=ET)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "data_quality.json").write_text(json.dumps({
        "day": "2026-10-12", "at": "2026-10-12T17:10:00-04:00", "ok": False, "integrity": [], "numeric": [],
        "cross": {"status": "deviation", "deviations": [{"symbol": "AAPL"}], "max_dev_pct": 1.2, "provider": "t"}}))
    body = _client(tmp_path, monkeypatch, now, ops={"checklist": [], "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json()
    assert body["market"]["phase"] == "postmarket"
    dq = _c(body, "data_quality")
    assert dq["state"] == "error" and dq["since"].startswith("2026-10-12T17:10") and dq["impact"] and dq["action"]
    assert _c(body, "paper_loop")["idle_kind"] == "closed"


def test_postmarket_data_quality_old_report_is_warn(tmp_path, monkeypatch):
    now = datetime(2026, 10, 12, 18, 0, tzinfo=ET)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "data_quality.json").write_text(json.dumps({
        "day": "2026-10-08", "at": "x", "ok": True, "integrity": [], "numeric": [],
        "cross": {"status": "ok", "compared": 3, "max_dev_pct": 0.1}}))
    dq = _c(_client(tmp_path, monkeypatch, now, ops={"checklist": [], "problems": [], "trading_day": True}).get(
        "/api/trading/system-status").json(), "data_quality")
    assert dq["state"] == "warn" and dq["stale"]


def test_closed_day_all_quiet_and_readonly(tmp_path, monkeypatch):
    now = datetime(2026, 10, 11, 12, 0, tzinfo=ET)        # 周日
    calls = []
    monkeypatch.setattr("app.services.notify_opportunity._enqueue", lambda *a, **k: calls.append(a) or True)
    (tmp_path / "keep.txt").write_text("x")
    before = _snapshot(tmp_path)
    body = _client(tmp_path, monkeypatch, now, ops={"checklist": [], "problems": [], "trading_day": False}).get(
        "/api/trading/system-status").json()
    assert body["market"]["phase"] == "closed" and body["market"]["trading_day"] is False
    assert body["market"]["next_open"].startswith("2026-10-12T09:30")
    assert _c(body, "ops_watch")["idle_kind"] == "closed"
    for cid in ("paper_loop", "execution_quotes", "hot_quotes", "ops_watch"):
        assert _c(body, cid)["state"] in ("idle",), cid
    assert calls == []
    after = {k: v for k, v in _snapshot(tmp_path).items() if k in before}
    assert after == before
