"""Push channels unified in system settings + push log (2026-11-13).

One channel selection drives every push (trades, close, premarket, mood, weekly, news,
ops alerts, open radar, monitor rules). Adding a channel makes it receive all pushes.
Every send is written to a push log, per channel.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.services import notify_opportunity as notify
from app.services import preferences, push_channels, webhook_adapter
from app.services.notification_outbox import NotificationOutbox

TRADE = {"seq": 7, "kind": "fill", "card_id": "c1", "symbol": "AAPL",
         "trade_date": "2026-11-12", "qty": 1, "price": 100, "fee": 0.1}
TRADE_KEY = "trade:7:c1:2026-11-12"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "prefs" / "preferences.json"
    path.parent.mkdir()
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    monkeypatch.delenv("ZENTRADE_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("ZENTRADE_FEISHU_APP_SECRET", raising=False)
    yield path
    preferences._invalidate_cache()


@pytest.fixture
def wire(monkeypatch):
    """Configure feishu (group webhook) + wecom and record every adapter call."""
    preferences.save({"feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
                      "wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=y"})
    calls: list[tuple[str, str]] = []
    result = {"feishu": True, "wecom": True}

    def feishu(url, title, subtitle, body, secret=""):
        calls.append(("feishu", title))
        return result["feishu"]

    def wecom(url, title, body, subtitle=""):
        calls.append(("wecom", title))
        return result["wecom"]

    monkeypatch.setattr(notify, "send_feishu_card", feishu)
    monkeypatch.setattr(webhook_adapter, "send_wecom", wecom)
    return SimpleNamespace(calls=calls, result=result)


# ---- one selection for everything ------------------------------------------------

def test_never_saved_means_every_configured_channel_is_on(wire):
    assert preferences.get_push_channels() is None
    assert push_channels.enabled_channels() == ["feishu", "wecom"]
    assert push_channels.active_channels() == ["feishu", "wecom"]


def test_saved_selection_is_whitelisted_and_ordered():
    assert preferences.set_push_channels(["email", "bogus", "feishu", "email"]) == ["feishu", "email"]
    assert preferences.get_push_channels() == ["feishu", "email"]
    assert preferences.set_push_channels([]) == []
    assert push_channels.enabled_channels() == []


def test_system_push_goes_to_every_selected_channel_under_the_old_key(tmp_path, wire):
    assert notify.enqueue_trade_event(tmp_path, TRADE) is True
    assert notify.flush_notifications(tmp_path)["sent"] == 1
    assert sorted(ch for ch, _ in wire.calls) == ["feishu", "wecom"]
    row = notify._queue(tmp_path).status(TRADE_KEY)
    assert row["status"] == "sent"
    assert sorted(row["delivered"]) == ["feishu", "wecom"]


def test_unticking_feishu_stops_system_pushes_there(tmp_path, wire):
    preferences.set_push_channels(["wecom"])
    notify.enqueue_trade_event(tmp_path, TRADE)
    notify.flush_notifications(tmp_path)
    assert [ch for ch, _ in wire.calls] == ["wecom"]


def test_no_channel_selected_keeps_message_unsent_and_visible(tmp_path, wire):
    preferences.set_push_channels([])
    notify.enqueue_trade_event(tmp_path, TRADE)
    assert notify.flush_notifications(tmp_path)["sent"] == 0
    assert wire.calls == []
    log = notify.push_log(tmp_path)
    assert log["items"][0]["key"] == TRADE_KEY
    assert log["items"][0]["status"] == "unconfigured"


def test_new_channel_configured_later_is_ticked_automatically(tmp_path):
    preferences.save({"feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"})
    preferences.set_push_channels(["feishu"])
    before = push_channels.channel_configured("wecom")
    preferences.save({"wecom_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=y"})
    push_channels.note_configured("wecom", before)
    assert preferences.get_push_channels() == ["feishu", "wecom"]
    # already configured before -> an explicit untick is respected
    preferences.set_push_channels(["feishu"])
    push_channels.note_configured("wecom", True)
    assert preferences.get_push_channels() == ["feishu"]


def test_settings_endpoint_saves_selection_and_preferences_expose_it(wire):
    from app.api.settings import PushChannelsIn, update_push_channels
    out = update_push_channels(PushChannelsIn(channels=["wecom", "nope"]))
    assert out["push_channels"] == ["wecom"]
    assert out["push_channels_active"] == ["wecom"]
    assert preferences.get_push_channels() == ["wecom"]


# ---- no duplicate to channels that already got it ---------------------------------

def test_partial_failure_retries_only_the_failed_channel(tmp_path, wire):
    now = [1000.0]
    queue = NotificationOutbox(tmp_path, sender=notify.send_to_channel, configured=notify._configured,
                               targets=notify._targets, now=lambda: now[0])
    wire.result["wecom"] = False
    queue.enqueue(TRADE_KEY, {"title": "t", "subtitle": "", "body": "b"})
    queue.flush()
    assert sorted(ch for ch, _ in wire.calls) == ["feishu", "wecom"]
    row = queue.status(TRADE_KEY)
    assert row["status"] == "failed" and row["delivered"] == ["feishu"]
    assert "wecom" in row["last_error"]
    wire.result["wecom"] = True
    now[0] = row["next_attempt_at"]
    queue.flush()
    assert [ch for ch, _ in wire.calls].count("feishu") == 1
    assert queue.status(TRADE_KEY)["status"] == "sent"


def test_rows_sent_before_upgrade_are_not_sent_again(tmp_path, wire):
    path = tmp_path / "notifications" / "outbox.sqlite3"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE outbox (key TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0,
        sent_at REAL, last_error TEXT, lease_until REAL NOT NULL DEFAULT 0)""")
    db.execute("INSERT INTO outbox(key,payload,status,attempts,created_at,sent_at) VALUES(?,?,?,?,?,?)",
               (TRADE_KEY, json.dumps({"title": "old", "body": "b"}), "sent", 1, 1.0, 2.0))
    db.commit()
    db.close()
    assert notify.enqueue_trade_event(tmp_path, TRADE) is True
    assert notify.flush_notifications(tmp_path)["sent"] == 0
    assert wire.calls == []
    item = notify.push_log(tmp_path)["items"][0]
    assert item["status"] == "sent"
    assert item["channels"] == [{"channel": "feishu", "label": "飞书", "ok": True, "tries": 1,
                                 "at": 2.0, "error": None, "legacy": True}]


# ---- push log ----------------------------------------------------------------------

def test_push_log_records_each_channel_attempt_and_kind(tmp_path, wire):
    wire.result["wecom"] = False
    notify.enqueue_trade_event(tmp_path, TRADE)
    notify.flush_notifications(tmp_path)
    log = notify.push_log(tmp_path)
    assert log["total"] == 1 and log["failed"] == 1
    item = log["items"][0]
    assert item["kind"] == "trade" and item["kind_label"] == "成交"
    assert item["title"].startswith("ZenTrade")
    by = {c["channel"]: c for c in item["channels"]}
    assert by["feishu"]["ok"] is True and by["wecom"]["ok"] is False
    assert by["wecom"]["error"] == "send_failed"
    assert notify.push_log(tmp_path, only_failed=True)["items"][0]["key"] == TRADE_KEY


def test_push_log_api_reads_the_outbox(tmp_path, wire, monkeypatch):
    from app.api import settings as api
    monkeypatch.setattr(api, "_push_data_dir", lambda: tmp_path)
    notify.enqueue_trade_event(tmp_path, TRADE)
    notify.flush_notifications(tmp_path)
    out = api.get_push_log(limit=50, failed=False)
    assert out["items"][0]["key"] == TRADE_KEY
    assert "body" in out["items"][0]


# ---- open radar + monitor rules use the same path ---------------------------------

def test_open_radar_uses_the_unified_queue(tmp_path, wire):
    assert notify.enqueue_global(tmp_path, "open_radar:2026-11-12", {"title": "ZenTrade · 开盘放量",
                                 "subtitle": "2026-11-12", "body": "NVDA"},
                                 event_type="open_radar", data={"symbols": ["NVDA"]}) is True
    notify.flush_notifications(tmp_path)
    assert sorted(ch for ch, _ in wire.calls) == ["feishu", "wecom"]
    assert notify._queue(tmp_path).status("open_radar:2026-11-12")["status"] == "sent"


def test_monitor_rule_alerts_enter_the_outbox(tmp_path, wire, monkeypatch):
    from app.services import quote_service
    submitted = []
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR",
                        SimpleNamespace(submit=lambda fn, *a: submitted.append((fn, a))))
    service = object.__new__(quote_service.QuoteService)
    monkeypatch.setattr(service, "_push_data_dir", lambda: tmp_path, raising=False)
    engine = SimpleNamespace(rules={"on": {"webhook_channels": ["all"]}, "off": {"webhook_channels": []}})
    service._maybe_send_webhook([
        {"rule_id": "on", "source": "price", "symbol": "NVDA", "name": "英伟达", "message": "突破", "ts": 1,
         "price": 190.0, "change_pct": float("nan")},
        {"rule_id": "off", "source": "price", "symbol": "AMD", "name": "", "message": "x", "ts": 2},
    ], engine)
    keys = [i["key"] for i in notify.push_log(tmp_path)["items"]]
    assert keys == ["monitor:on:NVDA:1"]
    assert len(submitted) == 1 and submitted[0][0] is notify.flush_notifications
    fn, args = submitted[0]
    fn(*args)
    assert sorted(ch for ch, _ in wire.calls) == ["feishu", "wecom"]


def test_monitor_rule_channel_list_becomes_an_on_off_switch():
    from app.strategy import monitor_rules
    base = {"id": "r", "name": "r", "type": "price", "conditions": []}
    assert monitor_rules.normalize({**base, "webhook_channels": ["feishu", "x"]})["webhook_channels"] == ["all"]
    assert monitor_rules.normalize({**base, "webhook_channels": ["unsupported"]})["webhook_channels"] == []
    assert monitor_rules.normalize({**base, "webhook_channels": ["all"]})["webhook_channels"] == ["all"]
    assert monitor_rules.normalize({**base, "webhook_enabled": True})["webhook_channels"] == ["all"]
