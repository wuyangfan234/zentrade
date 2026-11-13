"""Server酱 (Turbo sctapi.ftqq.com / Server酱³ *.push.ft07.com) behind the third-party webhook.

Server酱 only reads ``title`` (<= 32 chars, one line) and ``desp`` (Markdown); success is
``code == 0`` in the JSON reply, not HTTP 200 (quota exhaustion comes back as HTTP 200).
"""
from __future__ import annotations

import pytest

from app.services import webhook_adapter

TURBO = "https://sctapi.ftqq.com/SCTtestkey000.send"
SC3 = "https://123.push.ft07.com/send/sctp123tkey.send"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"code": 0, "message": "", "data": {}}
        self.text = str(self._body)

    def json(self):
        return self._body


class Calls(list):
    reply: dict


@pytest.fixture
def posts(monkeypatch):
    calls = Calls()
    reply = {"resp": Resp()}

    def fake_post(url, data=None, json=None, content=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "json": json, "content": content, "headers": headers or {}})
        return reply["resp"]

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda s: None)
    calls.reply = reply
    return calls


@pytest.mark.parametrize("url,kind", [
    (TURBO, "turbo"), ("https://sc.ftqq.com/SCU1.send", "turbo"), (SC3, "sc3"),
    ("https://example.com/hook", None), ("https://ftqq.com.evil.example/x.send", None),
])
def test_serverchan_is_detected_by_host(url, kind):
    assert webhook_adapter.serverchan_kind(url) == kind


@pytest.mark.parametrize("url", [TURBO, SC3])
def test_custom_webhook_to_serverchan_sends_title_and_desp(posts, url):
    body = "**NVDA** · 回踩\n> 120 股 @ $188.50\n\n- 止损 $181.20"
    assert webhook_adapter.send_custom(url, "ZenTrade · 纸面买入", body, "notification", {"k": 1},
                                       subtitle="2026-11-12")
    sent = posts[0]
    assert sent["url"] == url
    assert sent["data"]["title"] == "ZenTrade · 纸面买入 · 2026-11-12"
    desp = sent["data"]["desp"]
    assert "**NVDA** · 回踩" in desp and "120 股 @ $188.50" in desp and "- 止损 $181.20" in desp
    assert "\n\n" in desp and "\n\n\n" not in desp  # one paragraph per line, Markdown keeps line breaks
    assert sent["headers"]["User-Agent"].startswith("ZenTrade-Webhook/")
    assert not [h for h in sent["headers"] if "tickflow" in h.lower()]


def test_serverchan_title_is_one_line_and_at_most_32_chars(posts):
    long_title = "ZenTrade · " + "持仓新闻提醒" * 8
    webhook_adapter.send_custom(TURBO, long_title + "\nsecond line", "正文", "notification", subtitle="2026-11-12")
    title = posts[0]["data"]["title"]
    assert len(title) <= 32 and "\n" not in title
    assert "2026-11-12" in posts[0]["data"]["desp"]  # date did not fit the title -> kept in the body


def test_serverchan_business_error_is_a_failure_even_with_http_200(posts):
    posts.reply["resp"] = Resp(200, {"code": 40001, "message": "超过当天的发送次数限制"})
    assert webhook_adapter.send_custom(TURBO, "ZenTrade · 收盘", "正文", "notification") is False
    assert len(posts) == 1  # quota / key errors are not retried (failed calls also count against quota)


def test_serverchan_server_error_is_retried(posts):
    posts.reply["resp"] = Resp(502, {})
    assert webhook_adapter.send_custom(TURBO, "ZenTrade · 收盘", "正文", "notification", max_attempts=3) is False
    assert len(posts) == 3


def test_generic_json_webhook_is_unchanged_for_other_hosts(posts):
    posts.reply["resp"] = Resp(204, {})
    assert webhook_adapter.send_custom("https://example.com/hook", "ZenTrade · 收盘", "正文", "notification")
    assert posts[0]["data"] is None and posts[0]["content"] is not None


def test_settings_test_button_names_serverchan(posts, monkeypatch):
    from app.api.settings import WebhookTestIn, test_webhook
    monkeypatch.setattr("app.services.preferences.get_custom_webhook_url", lambda: TURBO)
    monkeypatch.setattr("app.secrets_store.get_custom_webhook_secret", lambda: "")
    assert test_webhook(WebhookTestIn(channel="custom"))["ok"] is True
    assert posts[0]["data"]["title"] == "ZenTrade · 推送测试 · Server酱"
    assert "「Server酱」推送配置正确" in posts[0]["data"]["desp"]
