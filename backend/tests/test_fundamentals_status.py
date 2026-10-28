"""美股财务同步进系统状态页 (部件卡片 + 定时任务表) 与盯盘告警 (缺跑/失败/整轮 0 成功)。"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import fundamentals_sync as fs
from app.services import ops_watch as ow

ET = us_clock.tz


def _client(tmp_path, jobs=()):
    from app.api.trading import router
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = None
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: list(jobs), get_job=lambda j: None)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _comp(tmp_path, **kw):
    body = _client(tmp_path, **kw).get("/api/trading/system-status").json()
    return body, next(c for c in body["components"] if c["id"] == "fundamentals")


def test_component_waiting_before_first_run(tmp_path):
    _, c = _comp(tmp_path)
    assert c["state"] == "idle" and "18:30" in c["summary"]


def test_component_ok_after_success(tmp_path):
    fs.write_status(tmp_path, {"ok": True, "at": "2026-10-12T18:40:00-04:00", "batch": 59, "edgar_ok": 50,
                               "class_shares": ["GOOG"], "finnhub_shares": ["SKHY"], "failed": []})
    _, c = _comp(tmp_path)
    assert c["state"] == "ok" and "59" in c["summary"]


def test_component_error_when_whole_round_failed(tmp_path):
    fs.write_status(tmp_path, {"ok": False, "at": "2026-10-12T18:40:00-04:00", "error": "财务同步本轮 0/59 成功"})
    _, c = _comp(tmp_path)
    assert c["state"] == "error" and "0/59" in c["summary"]


def test_scheduled_records_failure_status_and_reraises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise fs.SyncFailedError("财务同步本轮 0/3 成功")
    monkeypatch.setattr(fs, "run", boom)
    with pytest.raises(fs.SyncFailedError):
        fs.scheduled(tmp_path)
    assert json.loads((tmp_path / "financials" / fs.STATUS_FILE).read_text())["ok"] is False


def test_job_listed_in_jobs_table(tmp_path):
    job = SimpleNamespace(id=fs.JOB_ID, name="f", next_run_time=datetime(2026, 10, 12, 18, 30, tzinfo=ET))
    body, _ = _comp(tmp_path, jobs=[job])
    assert fs.JOB_ID in {j["id"] for j in body["jobs"]}


@pytest.mark.parametrize("state,status", [("missing", None), ("error", "error")])
def test_missing_or_failed_alerts_feishu(tmp_path, state, status):
    sent = []
    item = {"job": fs.JOB_ID, "label": ow.WATCHED[fs.JOB_ID], "state": state, "due": "2026-10-12T18:30:00-04:00",
            "last": {"status": status, "error": "SyncFailedError"} if status else None}
    ow.evaluate_alerts(tmp_path, [], [item], datetime(2026, 10, 12, 19, 0, tzinfo=ET),
                       lambda k, p: sent.append((k, p)) or True)
    assert sent and "美股财务同步" in json.dumps(sent[0][1], ensure_ascii=False)


def test_frontend_job_label_present():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "frontend/src/pages/SystemStatus.tsx").read_text(encoding="utf-8")
    assert "zen_fundamentals_sync:" in src and "zen_top_snapshot:" not in src           # 0.29.1 首选定格退役
