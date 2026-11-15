"""回测排队提示 (11-16): 重任务额度被占时, 事件流先发 queued 告诉页面在等谁。

线上 11-16 实测: 发布挖掘策略后后台“回放验收”独占额度约 4 分钟, 期间连点三次回测, 页面一直写
“正在加载回测数据…”, 最后手动取消 —— 看起来就是“回测跑不出来”。
"""
from __future__ import annotations

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.backtest import worker
from app.services import heavy_job_limiter as limiter_mod
from app.services.heavy_job_limiter import HeavyJobLimiter


def test_holder_labels_track_named_slots_only():
    limiter = HeavyJobLimiter(capacity=2)
    with limiter.slot("normal", label="回测"):
        with limiter.slot("normal", label="ignored-nested"):  # 复用外层额度, 不另记
            assert limiter.holder_labels() == ["回测"]
        with limiter.slot("normal"):  # 匿名
            pass
        assert limiter.holder_labels() == ["回测"]
    assert limiter.holder_labels() == []
    assert limiter.available == 2


def test_stream_emits_queued_then_clears_when_slot_frees(monkeypatch):
    limiter = HeavyJobLimiter(capacity=2)
    monkeypatch.setattr(limiter_mod, "shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(worker, "make_worker_task", lambda *a, **k: {})

    def slow_run(task, *a, **k):  # 拿到额度后真回测要几秒: 期间页面应清掉排队提示
        time.sleep(1.2)
        return {"run_id": "r", "error": None}

    monkeypatch.setattr(worker, "run_worker_task", slow_run)

    held = threading.Event()
    release = threading.Event()

    def hold():
        with limiter.slot("mining", label="回放验收"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.5, release.set).start()

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "queue_probe", "start": "2024-01-02", "end": "2024-03-01",
    })
    holder.join(5)

    text = resp.text
    assert resp.status_code == 200
    first = text.index('event: queued\ndata: {"waiting_for": ["回放验收"]}')
    cleared = text.index('event: queued\ndata: {"waiting_for": []}')
    done = text.index("event: done")
    assert first < cleared < done


def test_stream_without_contention_sends_no_queued_event(monkeypatch):
    limiter = HeavyJobLimiter(capacity=2)
    monkeypatch.setattr(limiter_mod, "shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(worker, "make_worker_task", lambda *a, **k: {})
    monkeypatch.setattr(worker, "run_worker_task", lambda task, *a, **k: {"run_id": "r", "error": None})

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/api/backtest/strategy/stream", params={
        "strategy_id": "queue_probe_free", "start": "2024-01-02", "end": "2024-03-01",
    })

    assert "event: done" in resp.text
    assert "event: queued" not in resp.text
