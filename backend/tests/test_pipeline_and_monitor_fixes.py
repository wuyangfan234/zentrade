"""回归测试: 本轮修复的几处高风险行为(并发单飞 / 重任务槽 / sector fail-closed)。

均为纯逻辑, 不触网, 不依赖真实数据源。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import pipeline_jobs, preferences, quote_service
from app.services.pipeline_jobs import JobStore
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

# ── JobStore 单飞 ────────────────────────────────────────────────────────

def test_create_singleflight_dedupes_pending_window(monkeypatch, tmp_path):
    """两次快速 create() 在 pending 窗口内应复用同一 job(is_new=False)。"""
    monkeypatch.setattr(preferences, "load", lambda: {"data_source_job_timeout_s": 3600})
    store = JobStore(store_dir=tmp_path / "jobs")

    jid1, new1 = store.create()
    assert new1 is True
    assert store.get(jid1)["timeout_s"] == 3600

    # 尚未 start(), job 仍是 pending —— 旧实现会在此另起新 job(并发双跑根因)
    jid2, new2 = store.create()
    assert jid2 == jid1
    assert new2 is False

    # start() 后仍复用同一活跃 job
    store.start(jid1)
    jid3, new3 = store.create()
    assert jid3 == jid1
    assert new3 is False


def test_create_new_after_terminal(monkeypatch, tmp_path):
    """job 终态(succeed/fail)后, create() 应给出新 job。"""
    monkeypatch.setattr(preferences, "load", lambda: {"data_source_long_job_timeout_s": 5400})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid1, _ = store.create(long_running=True)
    assert store.get(jid1)["timeout_s"] == 5400
    store.start(jid1)
    store.succeed(jid1, {"ok": True})

    jid2, new2 = store.create()
    assert jid2 != jid1
    assert new2 is True


def test_run_slot_is_exclusive():
    """重任务执行槽同一时刻只允许一个持有者(防僵尸并发)。"""
    assert pipeline_jobs.try_acquire_run_slot() is True
    try:
        # 已被占用, 第二次获取失败
        assert pipeline_jobs.try_acquire_run_slot() is False
    finally:
        pipeline_jobs.release_run_slot()
    # 释放后可再次获取
    assert pipeline_jobs.try_acquire_run_slot() is True
    pipeline_jobs.release_run_slot()
    # 重复释放幂等, 不抛
    pipeline_jobs.release_run_slot()


# ── 监控 sector fail-closed ──────────────────────────────────────────────

def _base_price_rule(scope: str) -> dict:
    return {
        "id": "r_test",
        "name": "t",
        "type": "price",
        "conditions": [{"field": "close", "op": ">", "value": 10}],
        "logic": "and",
        "scope": scope,
    }


def test_validate_rejects_sector_scope():
    with pytest.raises(ValueError):
        monitor_rules.validate(_base_price_rule("sector"))


def test_validate_accepts_symbols_scope():
    rule = _base_price_rule("symbols")
    rule["symbols"] = ["JPM"]
    monitor_rules.validate(rule)  # 不应抛


def test_apply_scope_sector_fails_closed():
    """历史遗留 sector 规则在评估时应返回空(绝不退化为全市场)。"""
    df = pl.DataFrame({"symbol": ["JPM", "BAC"], "close": [10.0, 20.0]})
    out = MonitorRuleEngine._apply_scope(df, {"id": "r_old", "scope": "sector"})
    assert out.is_empty()

    # 对照: scope=all 返回全量, symbols 过滤子集
    assert MonitorRuleEngine._apply_scope(df, {"scope": "all"}).height == 2
    picked = MonitorRuleEngine._apply_scope(
        df, {"scope": "symbols", "symbols": ["JPM"]}
    )
    assert picked.height == 1


def test_monitor_webhook_uses_chinese_title_without_brand(monkeypatch, tmp_path):
    from app.services import notify_opportunity

    class Executor:
        def submit(self, fn, *args):
            return None

    queued = []
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", Executor())
    monkeypatch.setattr(QuoteService, "_push_data_dir", lambda self: tmp_path)
    monkeypatch.setattr(notify_opportunity, "enqueue_global",
                        lambda data_dir, key, payload, **kw: queued.append((key, payload, kw)) or True)
    engine = type("Engine", (), {"rules": {"r_vd": {"webhook_channels": ["all"]}}})()
    QuoteService._maybe_send_webhook(
        object.__new__(QuoteService),
        [{
            "rule_id": "r_vd",
            "source": "volume_delta",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "message": "放量 · 单轮增量 600,000 股",
        }],
        engine,
    )

    assert [payload["title"] for _, payload, _ in queued] == ["ZenTrade · 放量"]
    assert "TickFlow" not in queued[0][1]["title"]
    assert queued[0][2]["event_type"] == "monitor_alert"


def test_monitor_alert_reaches_custom_webhook_and_email_through_the_queue(monkeypatch, tmp_path):
    """Channels come from system settings (never saved = every configured channel)."""
    from app.services import email_adapter, webhook_adapter

    calls = []

    class Executor:
        def submit(self, fn, *args):
            return fn(*args)

    email_config = {
        "host": "smtp.example.com",
        "username": "bot@example.com",
        "from_address": "bot@example.com",
        "to_addresses": ["alerts@example.com"],
    }
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", Executor())
    monkeypatch.setattr(QuoteService, "_push_data_dir", lambda self: tmp_path)
    monkeypatch.setattr("app.services.preferences.get_push_channels", lambda: None)
    monkeypatch.setattr("app.services.feishu_app.is_configured", lambda: False)
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "")
    monkeypatch.setattr("app.services.preferences.get_wecom_webhook_url", lambda: "")
    monkeypatch.setattr("app.services.preferences.get_custom_webhook_url", lambda: "https://example.com/hook")
    monkeypatch.setattr("app.services.preferences.get_email_smtp_config", lambda: email_config)
    monkeypatch.setattr("app.secrets_store.get_custom_webhook_secret", lambda: "hook-secret")
    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "smtp-password")
    monkeypatch.setattr(webhook_adapter, "send_custom",
                        lambda *a, **kw: calls.append(("send_custom", a, kw)) or True)
    monkeypatch.setattr(email_adapter, "send_email", lambda *a: calls.append(("send_email", a, {})) or True)

    engine = type("Engine", (), {"rules": {"r_vd": {"webhook_channels": ["all"]}}})()
    event = {
        "rule_id": "r_vd",
        "source": "volume_delta",
        "symbol": "NVDA",
        "name": "NVIDIA",
        "message": "放量 · 单轮增量 600,000 股",
    }
    QuoteService._maybe_send_webhook(object.__new__(QuoteService), [event], engine)

    assert [name for name, _, _ in calls] == ["send_custom", "send_email"]
    assert calls[0][1][3] == "monitor_alert"
    assert calls[0][1][4]["rule_id"] == "r_vd" and calls[0][1][4]["symbol"] == "NVDA"
    assert calls[0][2] == {"secret": "hook-secret", "subtitle": ""}
    assert calls[1][1][:2] == (email_config, "smtp-password")
