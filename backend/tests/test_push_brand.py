"""Every push channel speaks as ZenTrade (2026-11-13): no upstream TickFlow name on the wire.

Captures what actually leaves the process (HTTP body + headers, SMTP message, Feishu card,
OS notification) for the settings-page test buttons, a production push and a monitor alert.
No network: httpx / smtplib / Feishu SDK are stubbed.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.services import (
    email_adapter,
    feishu_app,
    notify_adapter,
    preferences,
    push_channels,
    webhook_adapter,
)
from app.services import notify_opportunity as notify

UPSTREAM = ("tickflow", "stock panel")


@pytest.fixture
def wire(tmp_path, monkeypatch):
    prefs = tmp_path / "preferences.json"
    prefs.write_text(json.dumps({
        "feishu_app_chat_id": "oc_test",
        "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        "feishu_webhook_secret": "s",
        "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-000000000000",
        "custom_webhook_url": "https://example.com/hook",
        "email_smtp_config": {"host": "smtp.example.com", "port": 465, "security": "ssl", "username": "bot@example.com",
                              "from_address": "bot@example.com", "to_addresses": ["me@example.com"]},
        "system_notify_enabled": True,
    }))
    monkeypatch.setattr(preferences, "_path", lambda: prefs)
    preferences._invalidate_cache()
    monkeypatch.setattr(secrets_store, "get_custom_webhook_secret", lambda: "hook-secret")
    monkeypatch.setattr(secrets_store, "get_email_smtp_password", lambda: "pw")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_ID", "id")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_SECRET", "secret")
    calls: list[dict] = []

    class Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"code": 0, "errcode": 0}

    def fake_post(url, json=None, content=None, headers=None, timeout=None):
        body = json if json is not None else __import__("json").loads(content)
        host = url.split("/")[2]
        channel = {"open.feishu.cn": "feishu_webhook", "qyapi.weixin.qq.com": "wecom"}.get(host, "custom")
        calls.append({"channel": channel, "headers": dict(headers or {}), "payload": body})
        return Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            calls.append({"channel": "email", "headers": {"Subject": msg["Subject"], "From": msg["From"]},
                          "payload": {"text": msg.get_content()}})

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(email_adapter.smtplib, "SMTP_SSL", FakeSMTP)

    class Channel:
        async def send(self, target, payload, options):
            calls.append({"channel": "feishu_app", "headers": {}, "payload": payload["card"]})
            return SimpleNamespace(success=True)

        def stop(self, **kw):
            pass

    monkeypatch.setattr(feishu_app, "_channel", lambda *a: Channel())
    monkeypatch.setattr(feishu_app, "_cleanup_failed", False)
    monkeypatch.setattr(notify_adapter, "notify",
                        lambda title, message, icon=None: calls.append(
                            {"channel": "system_notify", "headers": {}, "payload": {"title": title, "body": message}}) or True)
    return calls


def _no_upstream(calls):
    blob = json.dumps(calls, ensure_ascii=False).lower()
    assert not [w for w in UPSTREAM if w in blob], blob


def _title(rec):
    p, ch = rec["payload"], rec["channel"]
    if ch == "feishu_app":
        return p["header"]["title"]["content"]
    if ch == "feishu_webhook":
        return p["card"]["header"]["title"]["content"]
    if ch == "wecom":
        return p["text"]["content"].split("\n", 1)[0]
    if ch == "custom":
        return p["title"]
    if ch == "email":
        return rec["headers"]["Subject"]
    return p["title"]


@pytest.mark.parametrize("channel,wire_channel", [
    ("feishu_app", "feishu_app"), ("feishu", "feishu_webhook"), ("wecom", "wecom"), ("custom", "custom"), ("email", "email"),
])
def test_settings_test_button_is_zentrade_on_every_channel(wire, channel, wire_channel):
    from app.api.settings import WebhookTestIn, test_webhook
    assert test_webhook(WebhookTestIn(channel=channel))["ok"] is True
    assert [c["channel"] for c in wire] == [wire_channel]
    assert _title(wire[0]).startswith("ZenTrade · 推送测试")
    _no_upstream(wire)


def test_feishu_webhook_test_uses_the_production_card_once(wire, monkeypatch):
    """The group-webhook test sends the same card format production uses, single attempt."""
    from app.api.settings import WebhookTestIn, test_webhook
    seen = {}
    real = notify.send_feishu_card

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(notify, "send_feishu_card", spy)
    test_webhook(WebhookTestIn(channel="feishu"))
    assert seen.get("max_attempts") == 1
    assert wire[0]["payload"]["msg_type"] == "interactive"


def _close_payload(lines=60):
    body = "\n".join(["**2026-11-12 收盘 · 机会板**"] + [f"NVDA 多 挂单 18{i % 10}.10 止损 17{i % 10}.20" for i in range(lines)]
                     + ["**纸面日报**", "> 净值 $100,512.33 · 现金 $61,000.00"])
    return {"title": "ZenTrade · 收盘", "subtitle": "2026-11-12", "body": body}


@pytest.mark.parametrize("channel", ["feishu", "wecom", "custom", "email"])
def test_production_push_keeps_title_and_date_on_every_channel(wire, channel):
    assert notify.send_to_channel(_close_payload(lines=3), channel) is True
    rec = wire[-1]
    assert _title(rec).startswith("ZenTrade · 收盘")
    assert "2026-11-12" in json.dumps(rec, ensure_ascii=False)
    _no_upstream(wire)


def test_custom_webhook_headers_and_envelope_are_zentrade(wire):
    notify.send_to_channel({**_close_payload(lines=1), "event_type": "notification", "data": {"k": 1}}, "custom")
    rec = wire[-1]
    assert rec["headers"]["User-Agent"].startswith("ZenTrade-Webhook/")
    assert rec["headers"]["X-ZenTrade-Signature"].startswith("sha256=")
    assert rec["headers"]["X-ZenTrade-Timestamp"].isdigit()
    assert rec["payload"]["subtitle"] == "2026-11-12"
    assert not [h for h in rec["headers"] if "tickflow" in h.lower()]


def test_email_from_name_subject_and_plain_body(wire):
    notify.send_to_channel(_close_payload(lines=2), "email")
    rec = wire[-1]
    assert rec["headers"]["From"] == "ZenTrade <bot@example.com>"
    assert rec["headers"]["Subject"] == "ZenTrade · 收盘 · 2026-11-12"
    text = rec["payload"]["text"]
    assert "**" not in text and "\n> " not in text
    assert "ZenTrade" in text.splitlines()[-1]


def test_wecom_long_push_fits_the_byte_limit_and_points_to_push_log(wire):
    notify.send_to_channel(_close_payload(lines=120), "wecom")
    text = wire[-1]["payload"]["text"]["content"]
    assert wire[-1]["payload"]["msgtype"] == "text"  # text renders in personal WeChat, markdown does not
    assert len(text.encode("utf-8")) <= 2048
    assert text.startswith("ZenTrade · 收盘\n2026-11-12")
    assert "**" not in text
    assert text.endswith(webhook_adapter.WECOM_TRUNCATED_HINT)
    assert "推送日志" in webhook_adapter.WECOM_TRUNCATED_HINT


def test_wecom_short_push_is_sent_whole(wire):
    notify.send_to_channel(_close_payload(lines=2), "wecom")
    text = wire[-1]["payload"]["text"]["content"]
    assert webhook_adapter.WECOM_TRUNCATED_HINT not in text
    assert "净值 $100,512.33" in text


def test_monitor_alert_and_system_notification_carry_the_brand(wire, tmp_path, monkeypatch):
    from app.services import quote_service
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", SimpleNamespace(submit=lambda fn, *a: fn(*a)))
    monkeypatch.setattr(push_channels, "active_channels", lambda: ["feishu"])
    svc = object.__new__(quote_service.QuoteService)
    monkeypatch.setattr(quote_service.QuoteService, "_push_data_dir", lambda self: tmp_path)
    ev = {"rule_id": "r1", "source": "volume_delta", "symbol": "NVDA", "name": "英伟达", "message": "放量", "ts": 1}
    svc._maybe_send_webhook([ev], SimpleNamespace(rules={"r1": {"webhook_channels": ["all"]}}))
    svc._maybe_send_system_notifications([ev])
    titles = [_title(c) for c in wire]
    assert titles == ["ZenTrade · 放量", "ZenTrade · 放量"]
    _no_upstream(wire)


def test_brand_title_does_not_double_prefix():
    assert push_channels.brand_title("放量") == "ZenTrade · 放量"
    assert push_channels.brand_title("ZenTrade · 收盘") == "ZenTrade · 收盘"
    assert push_channels.brand_title("") == "ZenTrade · 通知"


def test_push_sources_carry_no_upstream_name():
    """Guard the push modules and the push settings page (the TickFlow *data source* lives elsewhere)."""
    root = Path(__file__).resolve().parents[1]
    files = [root / "app/services" / n for n in ("webhook_adapter.py", "email_adapter.py", "notify_opportunity.py",
                                                  "push_channels.py", "feishu_app.py", "notification_outbox.py")]
    files.append(root.parent / "frontend/src/pages/settings/PushSettings.tsx")
    hits = [f.name for f in files if any(w in f.read_text(encoding="utf-8").lower() for w in UPSTREAM)]
    assert hits == []
