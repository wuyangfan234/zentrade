"""审计 2026-10-26 L-9: 偏好接口不再明文回显 Webhook token / 签名密钥 / 机器人 Secret; 保存时掩码回传 = 保持原值。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/FAKE-HOOK-TOKEN-0001"
WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=FAKE-WECOM-KEY-12345678"
CUSTOM = "https://hooks.example.com/ingest/FAKE-CUSTOM-PATH-TOKEN"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.api import settings as settings_api
    from app.services import preferences
    store: dict = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(store))
    monkeypatch.setattr(preferences, "save", lambda d: store.update(d))
    preferences.save({"feishu_webhook_url": FEISHU, "feishu_webhook_secret": "FAKE_FEISHU_SIGN_SECRET",
                      "wecom_webhook_url": WECOM, "custom_webhook_url": CUSTOM,
                      "wecom_bot_id": "botid", "wecom_bot_secret": "FAKE_WECOM_BOT_SECRET"})
    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app), store


def test_get_preferences_never_echoes_secrets(client):
    c, _ = client
    body = c.get("/api/settings/preferences").json()
    raw = json.dumps(body)
    for s in ("FAKE_FEISHU_SIGN_SECRET", "FAKE_WECOM_BOT_SECRET", "FAKE-HOOK-TOKEN", "FAKE-WECOM-KEY", "FAKE-CUSTOM-PATH"):
        assert s not in raw
    assert body["feishu_webhook_url"].startswith("https://open.feishu.cn/open-apis/bot/v2/hook/")
    assert body["feishu_webhook_secret_set"] is True and body["feishu_webhook_secret"] == ""
    assert body["wecom_bot_secret_set"] is True and body["wecom_bot_secret"] == ""
    assert body["wecom_webhook_url"] and body["custom_webhook_url"]             # 仍可判断「已配置」


def test_masked_roundtrip_keeps_values_and_secret(client):
    c, store = client
    body = c.get("/api/settings/preferences").json()
    r = c.put("/api/settings/preferences/feishu-webhook", json={"url": body["feishu_webhook_url"]})
    assert r.status_code == 200 and "FAKE" not in r.text
    assert store["feishu_webhook_url"] == FEISHU and store["feishu_webhook_secret"] == "FAKE_FEISHU_SIGN_SECRET"
    assert c.put("/api/settings/preferences/wecom-webhook", json={"url": body["wecom_webhook_url"]}).status_code == 200
    assert store["wecom_webhook_url"] == WECOM
    assert c.put("/api/settings/preferences/custom-webhook", json={"url": body["custom_webhook_url"]}).status_code == 200
    assert store["custom_webhook_url"] == CUSTOM


def test_new_url_and_explicit_secret_update(client):
    c, store = client
    new = "https://open.feishu.cn/open-apis/bot/v2/hook/NEW-TOKEN-9999"
    c.put("/api/settings/preferences/feishu-webhook", json={"url": new, "secret": "S2"})
    assert store["feishu_webhook_url"] == new and store["feishu_webhook_secret"] == "S2"
    c.put("/api/settings/preferences/feishu-webhook", json={"url": new, "secret": ""})
    assert store["feishu_webhook_secret"] == ""                                  # 显式空串 = 清除
    c.put("/api/settings/preferences/feishu-webhook", json={"url": ""})
    assert store["feishu_webhook_url"] == ""


def test_tampered_mask_is_rejected(client):
    c, store = client
    r = c.put("/api/settings/preferences/feishu-webhook",
              json={"url": "https://open.feishu.cn/open-apis/bot/v2/hook/\u2022\u2022\u2022\u2022XXXX"})
    assert r.status_code == 400 and store["feishu_webhook_url"] == FEISHU


def test_wecom_bot_secret_kept_when_omitted(client):
    c, store = client
    r = c.put("/api/settings/preferences/wecom-bot", json={"bot_id": "botid", "enabled": False})
    assert r.status_code == 200 and "FAKE_WECOM_BOT_SECRET" not in r.text
    assert store["wecom_bot_secret"] == "FAKE_WECOM_BOT_SECRET"
    c.put("/api/settings/preferences/wecom-bot", json={"bot_id": "", "enabled": False})
    assert store["wecom_bot_secret"] == ""
