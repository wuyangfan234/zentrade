"""飞书应用机器人推送 (Channel SDK) — 取代群自定义机器人 Webhook。

用户 2026-10-01: 群 Webhook 不稳定, 改用飞书 Agent 集成能力的应用机器人方式发送。
这里只覆盖「凭据/目标解析、消息形态、失败语义」; SDK 本身用注入的假实现, 不触网。
"""
from __future__ import annotations

import pytest

from app.services import feishu_app


class FakeResult:
    def __init__(self, success=True, message_id="om_1", error=None):
        self.success, self.message_id, self.error = success, message_id, error


class FakeChannel:
    def __init__(self, result=None, raises=None):
        self.calls, self.result, self.raises = [], result or FakeResult(), raises

    async def send(self, to, message, opts=None):
        self.calls.append((to, message, opts))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_SECRET", "secret-test")
    monkeypatch.setattr(feishu_app, "_saved_chat_id", lambda: "oc_target")
    fake = FakeChannel()
    monkeypatch.setattr(feishu_app, "_channel", lambda app_id, secret: fake)
    return fake


def test_card_goes_to_the_saved_chat_as_a_schema_2_card(configured):
    assert feishu_app.send_card("ZenTrade · 每日机会", "2026-10-01", "**NVDA** · 突破") is True
    to, message, opts = configured.calls[0]
    assert to == "oc_target" and opts == {"receive_id_type": "chat_id"}
    card = message["card"]
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "ZenTrade · 每日机会"
    texts = [e.get("content") for e in card["body"]["elements"] if e.get("tag") == "markdown"]
    assert "**2026-10-01**" in texts and "**NVDA** · 突破" in texts


def test_not_configured_without_app_credentials_or_chat(monkeypatch):
    monkeypatch.delenv("ZENTRADE_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("ZENTRADE_FEISHU_APP_SECRET", raising=False)
    monkeypatch.setattr(feishu_app, "_saved_chat_id", lambda: "oc_target")
    assert feishu_app.is_configured() is False
    assert feishu_app.send_card("t", "", "b") is False

    monkeypatch.setenv("ZENTRADE_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_SECRET", "secret-test")
    monkeypatch.setattr(feishu_app, "_saved_chat_id", lambda: "")
    assert feishu_app.is_configured() is False


def test_upstream_failure_and_exceptions_return_false_without_leaking_the_secret(monkeypatch, caplog):
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_SECRET", "secret-test")
    monkeypatch.setattr(feishu_app, "_saved_chat_id", lambda: "oc_target")
    monkeypatch.setattr(feishu_app, "_channel", lambda a, s: FakeChannel(FakeResult(False, None, "permission_denied")))
    assert feishu_app.send_card("t", "", "b") is False
    monkeypatch.setattr(feishu_app, "_channel", lambda a, s: FakeChannel(raises=RuntimeError("boom secret-test")))
    assert feishu_app.send_card("t", "", "b") is False
    assert "secret-test" not in caplog.text


def test_long_bodies_are_truncated_below_the_card_limit(configured):
    feishu_app.send_card("t", "", "x" * 100_000)
    body = configured.calls[0][1]["card"]["body"]["elements"][-1]["content"]
    assert len(body) < 28_100


def test_outbox_uses_the_app_bot_when_configured(configured, tmp_path):
    from app.services import notify_opportunity

    notify_opportunity._enqueue(tmp_path, "daily:2026-10-01", {"title": "ZenTrade · 每日机会", "subtitle": "d", "body": "b"})
    assert notify_opportunity.notification_status(tmp_path)["configured"] is True
    result = notify_opportunity.flush_notifications(tmp_path)
    assert result["sent"] == 1
    assert configured.calls and configured.calls[0][0] == "oc_target"


def test_settings_save_rejects_non_chat_ids_and_reports_readiness(monkeypatch, tmp_path):
    from fastapi import HTTPException

    from app.api import settings as api
    from app.services import preferences

    store: dict = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(store))
    monkeypatch.setattr(preferences, "save", lambda updates: store.update(updates))
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("ZENTRADE_FEISHU_APP_SECRET", "secret-test")
    with pytest.raises(HTTPException):
        api.update_feishu_app(api.FeishuAppPrefsIn(chat_id="https://open.feishu.cn/open-apis/bot/v2/hook/x"))
    out = api.update_feishu_app(api.FeishuAppPrefsIn(chat_id="oc_abc"))
    assert out == {"feishu_app_chat_id": "oc_abc", "feishu_app_configured": True}


def test_settings_test_send_uses_the_app_bot_and_never_returns_credentials(configured):
    from app.api import settings as api

    out = api.test_webhook(api.WebhookTestIn(channel="feishu_app"))
    assert out["ok"] is True
    assert "secret-test" not in str(out) and "cli_test" not in str(out)
    assert configured.calls[0][0] == "oc_target"


def test_chats_endpoint_without_credentials_is_explicit_not_an_error(monkeypatch):
    from app.api import settings as api

    monkeypatch.delenv("ZENTRADE_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("ZENTRADE_FEISHU_APP_SECRET", raising=False)
    out = api.list_feishu_app_chats()
    assert out["credentials_set"] is False and out["chats"] == []
