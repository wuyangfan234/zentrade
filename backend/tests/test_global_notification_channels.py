from __future__ import annotations

from app import secrets_store
from app.services import email_adapter, webhook_adapter
from app.services import notify_opportunity as notify
from app.services.notification_outbox import NotificationOutbox


def test_enqueue_global_uses_each_selected_channel_with_independent_keys(tmp_path, monkeypatch):
    channels = ["feishu", "wecom", "custom", "email"]
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: channels)
    calls = []
    monkeypatch.setattr(notify, "_enqueue", lambda data_dir, key, payload: calls.append((key, payload)) or True)
    payload = {"title": "ZenTrade · 开盘放量", "subtitle": "2026-10-20", "body": "NVDA"}

    result = notify.enqueue_global(
        tmp_path,
        "open_radar:2026-10-20",
        payload,
        event_type="open_radar",
        data={"symbols": ["NVDA"]},
    )

    assert result == {"selected": channels, "queued": channels, "ok": True}
    assert [key for key, _ in calls] == [f"open_radar:2026-10-20:{channel}" for channel in channels]
    for channel, (_, item) in zip(channels, calls, strict=True):
        assert item == {
            **payload,
            "channel": channel,
            "event_type": "open_radar",
            "data": {"symbols": ["NVDA"]},
        }


def test_enqueue_global_with_no_selected_channels_does_not_enqueue(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: [])
    monkeypatch.setattr(notify, "_enqueue", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected enqueue")))

    result = notify.enqueue_global(tmp_path, "open_radar:2026-10-20", {"title": "t", "body": "b"})

    assert result == {"selected": [], "queued": [], "ok": False}


def test_enqueue_global_retry_only_uses_missing_still_selected_channels(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: ["feishu", "wecom"])
    calls = []
    monkeypatch.setattr(notify, "_enqueue", lambda data_dir, key, payload: calls.append(key) or True)

    result = notify.enqueue_global(
        tmp_path, "open_radar:2026-10-20", {"title": "t", "body": "b"},
        channels=["wecom", "email"],
    )

    assert result == {"selected": ["wecom"], "queued": ["wecom"], "ok": True}
    assert calls == ["open_radar:2026-10-20:wecom"]


def test_global_delivery_uses_existing_channel_adapters(monkeypatch):
    selected = ["wecom", "custom", "email"]
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: selected)
    monkeypatch.setattr(notify.preferences, "get_wecom_webhook_url", lambda: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")
    monkeypatch.setattr(notify.preferences, "get_custom_webhook_url", lambda: "https://example.com/hook")
    email_config = {"host": "smtp.example.com", "from_address": "bot@example.com", "to_addresses": ["to@example.com"]}
    monkeypatch.setattr(notify.preferences, "get_email_smtp_config", lambda: email_config)
    monkeypatch.setattr(secrets_store, "get_custom_webhook_secret", lambda: "custom-secret")
    monkeypatch.setattr(secrets_store, "get_email_smtp_password", lambda: "email-secret")
    calls = []
    monkeypatch.setattr(webhook_adapter, "send_wecom", lambda *args: calls.append(("wecom", args)) or True)
    monkeypatch.setattr(webhook_adapter, "send_custom", lambda *args, **kwargs: calls.append(("custom", args, kwargs)) or True)
    monkeypatch.setattr(email_adapter, "send_email", lambda *args: calls.append(("email", args)) or True)
    payload = {"title": "标题", "body": "正文", "event_type": "open_radar", "data": {"symbols": ["NVDA"]}}

    assert notify._send_payload({**payload, "channel": "wecom"})
    assert notify._send_payload({**payload, "channel": "custom"})
    assert notify._send_payload({**payload, "channel": "email"})

    assert calls[0] == ("wecom", ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "标题", "正文"))
    assert calls[1][0] == "custom"
    assert calls[1][1] == ("https://example.com/hook", "标题", "正文", "open_radar", {"symbols": ["NVDA"]})
    assert calls[1][2] == {"secret": "custom-secret"}
    assert calls[2] == ("email", (email_config, "email-secret", "标题", "正文"))


def test_global_delivery_stops_when_channel_is_deselected(monkeypatch):
    monkeypatch.setattr(notify.preferences, "get_webhook_default_channels", lambda: ["feishu"])
    monkeypatch.setattr(notify.preferences, "get_wecom_webhook_url", lambda: "configured")
    monkeypatch.setattr(webhook_adapter, "send_wecom", lambda *args: (_ for _ in ()).throw(AssertionError("must not send")))

    assert notify._send_payload({"title": "t", "body": "b", "channel": "wecom"}) is False
    assert notify._payload_configured({"channel": "wecom"}) is False


def test_outbox_payload_configuration_allows_ready_channel_and_defers_unready_one(tmp_path):
    now = [100.0]
    ready = {"wecom": False, "email": True}
    sent = []
    queue = NotificationOutbox(
        tmp_path,
        sender=lambda payload: sent.append(payload["channel"]) or True,
        configured=lambda: False,
        configured_for_payload=lambda payload: ready[payload["channel"]],
        now=lambda: now[0],
    )

    assert queue.enqueue("radar:wecom", {"channel": "wecom"})["status"] == "unconfigured"
    assert queue.enqueue("radar:email", {"channel": "email"})["status"] == "pending"
    assert queue.flush()["sent"] == 1
    assert sent == ["email"]
    assert queue.status("radar:wecom")["attempts"] == 0

    ready["wecom"] = True
    now[0] = queue.status("radar:wecom")["next_attempt_at"]
    assert queue.flush()["sent"] == 1
    assert sent == ["email", "wecom"]


def test_outbox_configuration_deferrals_do_not_consume_send_budget(tmp_path):
    now = [100.0]
    sent = []
    queue = NotificationOutbox(
        tmp_path,
        sender=lambda payload: sent.append(payload["channel"]) or True,
        configured=lambda: False,
        configured_for_payload=lambda payload: payload["channel"] == "email",
        now=lambda: now[0],
    )
    for index in range(12):
        assert queue.enqueue(f"blocked:{index}", {"channel": f"blocked-{index}"})["status"] == "unconfigured"
    queue.enqueue("radar:email", {"channel": "email"})

    assert queue.flush(max_items=10)["sent"] == 1
    assert sent == ["email"]
    assert queue.status("radar:email")["status"] == "sent"


def test_outbox_email_success_does_not_mark_feishu_failure_recovered(tmp_path):
    queue = NotificationOutbox(
        tmp_path,
        sender=lambda payload: payload.get("channel") == "email",
        configured=lambda: True,
        configured_for_payload=lambda payload: True,
        max_attempts=1,
    )
    queue.enqueue("legacy:feishu", {"title": "legacy", "body": "failed"})
    queue.enqueue("radar:email", {"channel": "email", "title": "radar", "body": "sent"})
    queue.flush()

    status = queue.status()
    assert queue.status("radar:email")["status"] == "sent"
    assert queue.status("legacy:feishu")["exhausted"] is True
    assert status["status"] == "failed"
    assert status["unresolved_failed"] == 1
