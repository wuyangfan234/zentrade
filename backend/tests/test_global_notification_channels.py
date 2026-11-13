from __future__ import annotations

from app import secrets_store
from app.services import email_adapter, webhook_adapter
from app.services import notify_opportunity as notify
from app.services.notification_outbox import NotificationOutbox


def test_enqueue_global_is_one_row_with_event_type_and_data(tmp_path, monkeypatch):
    """2026-11-13: one row per message; channels are resolved at send time from system settings."""
    calls = []
    monkeypatch.setattr(notify, "_enqueue", lambda data_dir, key, payload: calls.append((key, payload)) or True)
    payload = {"title": "ZenTrade · 开盘放量", "subtitle": "2026-10-20", "body": "NVDA"}

    assert notify.enqueue_global(tmp_path, "open_radar:2026-10-20", payload,
                                 event_type="open_radar", data={"symbols": ["NVDA"]}) is True
    assert calls == [("open_radar:2026-10-20",
                      {**payload, "event_type": "open_radar", "data": {"symbols": ["NVDA"]}})]


def test_global_delivery_uses_existing_channel_adapters(monkeypatch):
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

    assert notify.send_to_channel(payload, "wecom")
    assert notify.send_to_channel(payload, "custom")
    assert notify.send_to_channel(payload, "email")

    # every channel gets the "ZenTrade · " title (2026-11-13); email gets plain text + footer
    assert calls[0] == ("wecom", ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "ZenTrade · 标题", "正文", ""))
    assert calls[1][0] == "custom"
    assert calls[1][1] == ("https://example.com/hook", "ZenTrade · 标题", "正文", "open_radar", {"symbols": ["NVDA"]})
    assert calls[1][2] == {"secret": "custom-secret", "subtitle": ""}
    assert calls[2] == ("email", (email_config, "email-secret", "ZenTrade · 标题", "正文" + notify.EMAIL_FOOTER))


def test_targets_follow_ticked_and_configured_channels(monkeypatch):
    monkeypatch.setattr(notify.push_channels, "active_channels", lambda: ["feishu"])
    assert notify._targets({"title": "t", "body": "b"}) == ["feishu"]
    # a row pinned to one channel by the old per-channel queue only goes there, and only if still ticked
    assert notify._targets({"title": "t", "body": "b", "channel": "wecom"}) == []
    assert notify._targets({"title": "t", "body": "b", "channel": "feishu"}) == ["feishu"]


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
