"""纸面通知 outbox：零网络测试、持久化重启、退避、并发隔离。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event

from app.services import notify_opportunity as notify


def make_queue(tmp_path, **kwargs):
    assert hasattr(notify, "NotificationOutbox"), "需要持久化 outbox 公共接口"
    return notify.NotificationOutbox(tmp_path, **kwargs)


def event(**extra):
    return {"seq": 7, "kind": "fill", "card_id": "c1", "symbol": "AAPL",
            "trade_date": "2026-09-25", "qty": 1, "price": 100, "fee": 0.1, **extra}


def test_enqueue_is_durable_deduplicated_and_does_not_send(tmp_path):
    sent = []
    queue = make_queue(tmp_path, sender=lambda payload: sent.append(payload) or True,
                       configured=lambda: True, now=lambda: 100)
    first = queue.enqueue("trade:7:c1:2026-09-25", {"body": "example"})
    second = queue.enqueue("trade:7:c1:2026-09-25", {"body": "cannot replace frozen event"})
    assert first["status"] == second["status"] == "pending"
    assert sent == []
    restarted = make_queue(tmp_path, sender=lambda payload: sent.append(payload) or True,
                           configured=lambda: True, now=lambda: 100)
    assert restarted.flush()["sent"] == 1
    assert sent == [{"body": "example"}]
    assert restarted.status(first["key"])["status"] == "sent"
    assert restarted.flush()["sent"] == 0
    assert restarted.status()["counts"]["sent"] == 1


def test_missing_webhook_keeps_event_and_can_send_after_configuration(tmp_path):
    configured = [False]
    sent = []
    queue = make_queue(tmp_path, sender=lambda payload: sent.append(payload) or True,
                       configured=lambda: configured[0], now=lambda: 100)
    result = queue.enqueue("daily:2026-09-25", {"body": "example"})
    assert result["status"] == "unconfigured"
    assert queue.flush()["sent"] == 0
    assert queue.status(result["key"])["attempts"] == 0
    assert sent == []
    configured[0] = True
    assert queue.flush()["sent"] == 1
    assert queue.status(result["key"])["status"] == "sent"


def test_failure_is_retained_with_bounded_retry_and_no_tight_loop(tmp_path):
    now, attempts = [100.0], []
    queue = make_queue(tmp_path, sender=lambda payload: attempts.append(payload) or False,
                       configured=lambda: True, now=lambda: now[0], max_attempts=2)
    queue.enqueue("weekly:2026-09-27", {"body": "week"})
    assert queue.flush()["failed"] == 1
    row = queue.status("weekly:2026-09-27")
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > now[0]
    for _ in range(5):
        queue.flush()
    assert len(attempts) == 1
    now[0] = row["next_attempt_at"]
    queue.flush()
    now[0] += 100000
    queue.flush()
    assert len(attempts) == 2
    assert queue.status("weekly:2026-09-27")["exhausted"] is True


def test_sending_does_not_hold_enqueue_lock_and_only_one_flush_runs(tmp_path):
    entered, release = Event(), Event()
    sent = []

    def sender(payload):
        entered.set()
        assert release.wait(3)
        sent.append(payload)
        return True

    queue = make_queue(tmp_path, sender=sender, configured=lambda: True, now=lambda: 100)
    queue.enqueue("one", {"body": "one"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(queue.flush, max_items=1)
        try:
            assert entered.wait(2)
            other = pool.submit(queue.enqueue, "two", {"body": "two"})
            assert other.result(timeout=1)["status"] == "pending"
            assert queue.flush()["busy"] is True
        finally:
            release.set()
        assert pending.result(timeout=2)["sent"] == 1
    assert len(sent) == 1


def test_sender_exception_is_redacted_and_does_not_lose_event(tmp_path):
    def sender(payload):
        raise RuntimeError("secret webhook url must not enter status")

    queue = make_queue(tmp_path, sender=sender, configured=lambda: True, now=lambda: 100)
    queue.enqueue("one", {"body": "one"})
    queue.flush()
    row = queue.status("one")
    assert row["status"] == "failed"
    assert "secret webhook" not in str(row)
    assert row["last_error"] == "RuntimeError"


def test_trade_public_enqueue_and_status_never_call_webhook(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_url", lambda: "")
    monkeypatch.setattr(notify, "send_feishu_card", lambda *a: (_ for _ in ()).throw(AssertionError("network")))
    assert hasattr(notify, "enqueue_trade_event")
    assert notify.enqueue_trade_event(tmp_path, event()) is True
    assert notify.enqueue_trade_event(tmp_path, event()) is True
    status = notify.notification_status(tmp_path)
    assert status["status"] == "unconfigured"
    assert status["counts"]["unconfigured"] == 1
    assert notify.enqueue_trade_event(tmp_path, event(kind="stop_move")) is False


def test_weekly_scheduler_entry_actually_calls_push_api_and_deduplicates(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_url", lambda: "mock-configured")
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_secret", lambda: "")
    monkeypatch.setattr(notify, "send_feishu_card", lambda *a: calls.append(a) or True)
    assert hasattr(notify, "push_weekly_report")
    result = notify.push_weekly_report(tmp_path, date(2026, 9, 27))
    assert result["status"] == "sent"
    assert len(calls) == 1
    assert "周报" in calls[0][1]
    assert "台账未初始化" in calls[0][3]
    assert "0" in calls[0][3]
    notify.push_weekly_report(tmp_path, date(2026, 9, 27))
    assert len(calls) == 1


def test_daily_and_weekly_enqueue_match_parent_api(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_url", lambda: "")
    result = SimpleNamespace(as_of=date(2026, 9, 25), gate="green", scanned=42,
                             cards=[], watch=[], params_revision=1)
    assert hasattr(notify, "enqueue_daily_opportunities")
    assert notify.enqueue_daily_opportunities(tmp_path, result) is True
    assert notify.enqueue_daily_opportunities(tmp_path, result) is True
    report = {"week_end": "2026-09-27", "trips": 0, "by_setup": []}
    assert notify.enqueue_weekly_review(tmp_path, report) is True
    assert notify.enqueue_weekly_review(tmp_path, report) is True
    assert notify.notification_status(tmp_path)["counts"]["unconfigured"] == 2
    assert notify.flush_notifications(tmp_path, limit=1)["sent"] == 0


def test_empty_configured_queue_is_idle_not_success(tmp_path):
    queue = make_queue(tmp_path, sender=lambda payload: True, configured=lambda: True)
    assert queue.status()["status"] == "idle"


def test_weekly_without_webhook_does_not_claim_success(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.preferences, "get_feishu_webhook_url", lambda: "")
    assert hasattr(notify, "push_weekly_report")
    result = notify.push_weekly_report(tmp_path, date(2026, 9, 27))
    assert result["status"] == "unconfigured"
