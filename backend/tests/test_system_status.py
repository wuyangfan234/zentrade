"""/api/trading/system-status: one place that shows every component, its state and its evidence (no black box)."""
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.trading import router


def test_system_status_lists_all_components(tmp_path, monkeypatch):
    from datetime import datetime
    from app.market_clock import us_clock
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 12, 10, 0, tzinfo=us_clock.tz))  # 0.7.1: 报价类只在盘中判绿
    app = FastAPI()
    app.include_router(router)
    loop = SimpleNamespace(status=lambda: {"mode": "paper_only", "booted": True, "last_error": None, "last_quote_at": "x",
                                          "execution_source": {"active": "tickflow", "cross_checked": 3, "deviations": []},
                                          "circuit_breaker": {"block_new_entries": False, "risk_multiplier": 1.0},
                                          "entry_blockers": []})
    job = lambda i, n: SimpleNamespace(id=i, name=n, next_run_time=None)  # noqa: E731
    app.state.trading_loop = loop
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [job("zen_paper_tick", "t"), job("zen_hot_quotes", "h")])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    body = TestClient(app).get("/api/trading/system-status").json()
    names = [c["id"] for c in body["components"]]
    for want in ("execution_quotes", "hot_quotes", "realtime_source", "circuit_breaker", "decision_desk", "evidence",
                 "alpaca_mirror", "premarket_gap", "notifications", "earnings", "corporate_actions", "daily_pipeline", "data_freshness"):
        assert want in names, want
    ex = next(c for c in body["components"] if c["id"] == "execution_quotes")
    assert ex["state"] == "ok" and "tickflow" in ex["summary"]
    assert {j["id"] for j in body["jobs"]} == {"zen_paper_tick", "zen_hot_quotes"}
    assert all({"id", "label", "state", "summary"} <= set(c) for c in body["components"])


def test_job_times_are_normalized_to_eastern(tmp_path):
    from datetime import datetime, timedelta, timezone
    app = FastAPI()
    app.include_router(router)
    bj = timezone(timedelta(hours=8))
    app.state.trading_loop = None
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id="zen_hot_quotes", name="h",
                                                                            next_run_time=datetime(2026, 10, 2, 21, 30, tzinfo=bj))])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    body = TestClient(app).get("/api/trading/system-status").json()
    assert body["jobs"][0]["next_run"].startswith("2026-10-02T09:30:00-04:00")


def _status_app(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = None
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _component(body, cid):
    return next(c for c in body["components"] if c["id"] == cid)


def test_evidence_row_in_v4_shows_current_ruleset_and_forward_not_dashes(tmp_path):
    """10-10 review 待确认 2: v4 下「统一口径证据」行写着「窗口 —；生成于 —」且出处指向旧 evaluation.json。
    v4 没有评估窗口这个概念 → 显示现行口径的真实字段: 规则集版本/保存时间、勾选与可下单条数、前向确认样本与是否通过。"""
    import json
    (tmp_path / "decision_desk").mkdir()
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "current.json").write_text(json.dumps({"version": 2}))
    (tmp_path / "rules" / "R2.json").write_text(json.dumps({"version": 2, "enabled": ["breakout", "tv_ut_bot"],
                                                            "params": {}, "created_at": "2026-10-08T18:15:59+08:00"}))
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "evidence")
    assert "窗口 —" not in row["summary"] and "生成于 —" not in row["summary"]
    assert "v4" in row["summary"] and "规则集 R2" in row["summary"] and "2026-10-08 06:15 ET" in row["summary"]
    assert "勾选 2 条" in row["summary"] and "前向确认" in row["summary"]
    assert "evaluation.json" not in row["source"] and "rules/R2.json" in row["source"]


def test_evidence_row_in_v3_points_to_v3_file(tmp_path):
    import json
    (tmp_path / "decision_desk").mkdir()
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v3"}))
    (tmp_path / "decision_desk" / "evaluation_v3.json").write_text(json.dumps({
        "contract": "v3", "window": ["2025-01-01", "2026-09-30"], "generated_at": "2026-10-03T10:00:00", "sources": {}}))
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "evidence")
    assert "2025-01-01 ~ 2026-09-30" in row["summary"] and "2026-10-03T10:00:00" in row["summary"]
    assert row["source"] == "data/decision_desk/evaluation_v3.json"


def test_corporate_actions_row_explains_trading_day_refresh_point(tmp_path, monkeypatch):
    from datetime import datetime
    from app.market_clock import us_clock
    from app.services import corporate_actions as ca
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    svc = ca.CorporateActionService(tmp_path)
    svc.refresh(["AAPL"], datetime(2026, 10, 9, 8, 0, 5, tzinfo=us_clock.tz), http_get=lambda url: {"corporate_actions": {}})
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 10, 12, 0, tzinfo=us_clock.tz))   # 周六
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "corporate_actions")
    assert row["state"] == "ok"
    assert "按交易日 08:00 ET 刷新" in row["summary"] and "10-09 08:00" in row["summary"]


@pytest.mark.real_earnings_calendar
def test_earnings_row_explains_trading_day_refresh_point(tmp_path, monkeypatch):
    from datetime import datetime
    from app.market_clock import us_clock
    from app.services import earnings_calendar as ec
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    ec.EarningsCalendar(tmp_path).refresh(
        datetime(2026, 10, 9, 17, 0, 5, tzinfo=us_clock.tz), use_finnhub=False,
        nasdaq_get=lambda url, headers=None: {"data": {"rows": []}, "status": {"rCode": 200}})
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 11, 12, 0, tzinfo=us_clock.tz))   # 周日
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "earnings")
    assert row["state"] == "ok"
    assert "按交易日 08:30 / 17:00 ET 刷新" in row["summary"] and "10-09 17:00" in row["summary"]
    assert "周末/休市不过期" in row["summary"]


def test_ui_audit_1011_status_text_is_chinese_and_eastern(tmp_path, monkeypatch):
    """ui-audit-1011: 系统状态摘要只改展示 —— 时间统一美东 ET (不再混 UTC/裸 ISO), 状态码译成中文, 不出现「能力矩阵 realtime =」。"""
    from datetime import datetime
    from app.market_clock import us_clock
    from app.services import earnings_calendar, corporate_actions, evaluation_schedule, news_store
    # 冻结时钟到周六 (休市): 原测试用真实时钟, 交易时段内跑会把 10-09 的“最近运行”判成交易环停摆 (0.15.0 发现的定时炸弹)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 10, 12, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(earnings_calendar.EarningsCalendar, "status",
                        lambda self, now=None: {"status": "connected", "source": "nasdaq", "fetched_at": "2026-10-09T21:00:00Z", "due_at": None})
    monkeypatch.setattr(corporate_actions.CorporateActionService, "status",
                        lambda self, now=None: {"status": "stale", "symbols": ["A"], "fetched_at": "2026-10-09T12:00:00Z", "due_at": None})
    monkeypatch.setattr(evaluation_schedule, "last_runs", lambda data_dir, n=1: [{"status": "succeeded"}])
    monkeypatch.setattr(news_store, "status", lambda data_dir: {"last_run_at": "2026-10-11T09:37:19Z", "universe": 400})
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = SimpleNamespace(status=lambda: {"booted": True, "last_error": None,
                                                             "last_run_at": "2026-10-09T15:59:30.123456-04:00"})
    app.state.scheduler = SimpleNamespace(get_jobs=lambda: [])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    body = TestClient(app).get("/api/trading/system-status").json()
    text = " | ".join(c["summary"] for c in body["components"])
    for banned in ("UTC", "succeeded", "connected", "stale", "realtime =", "T15:59"):
        assert banned not in text, banned
    assert "最近 2026-10-11 05:37 ET" in _component(body, "news")["summary"]
    assert _component(body, "earnings")["summary"].startswith("已接入")
    assert _component(body, "corporate_actions")["summary"].startswith("已过期")
    assert "最近运行 2026-10-09 15:59 ET" in _component(body, "paper_loop")["summary"]
    assert "实时行情来源" in _component(body, "realtime_source")["summary"]


def _qveris_row(tmp_path, monkeypatch, auto_at: str):
    import json
    from datetime import datetime

    from app.market_clock import us_clock
    from app.services import qveris_gate
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 15, 2, 30, tzinfo=us_clock.tz))
    monkeypatch.setattr(us_clock, "today", lambda: datetime(2026, 10, 15, 2, 30, tzinfo=us_clock.tz).date())
    monkeypatch.setattr(qveris_gate, "usage", lambda d, **k: {"configured": True, "spent_today": 0, "daily_limit": 150,
                                                              "balance": {"remaining": 9421.23}})
    (tmp_path / "qveris").mkdir(parents=True, exist_ok=True)
    (tmp_path / "qveris" / "auto_state.json").write_text(json.dumps({
        "targets": 141, "fetched": ["A"] * 100, "cached": [], "failed": [{"symbol": "Z", "error": "x"}],
        "blocked": "今日 QVeris 积分已用 100/100, 本次需约 1, 已拒绝", "at": auto_at}), encoding="utf-8")
    monkeypatch.setattr(qveris_gate, "_dir", lambda d: tmp_path / "qveris")
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    body = TestClient(app).get("/api/trading/system-status").json()
    return next(c for c in body["components"] if c["id"] == "qveris")


def test_qveris_row_does_not_repeat_previous_days_quota_rejection(tmp_path, monkeypatch):
    """bughunt-1015 B2: 0.22.5 只把往日 blocked/failed 从 state 里摘掉, summary 仍拼「今日积分已用 100/100…已拒绝」,
    与同一行「今日 0/150」矛盾。往日那次只报结果数字, 不再挂今天的拒绝/失败文案。"""
    row = _qveris_row(tmp_path, monkeypatch, "2026-10-14T12:47:00Z")   # 10-14 08:47 ET (昨天)
    assert row["state"] == "ok"
    assert "已拒绝" not in row["summary"] and "失败" not in row["summary"]
    assert "目标 141" in row["summary"] and "今日 0/150" in row["summary"]


def test_qveris_row_keeps_todays_quota_rejection(tmp_path, monkeypatch):
    row = _qveris_row(tmp_path, monkeypatch, "2026-10-15T06:20:00Z")   # 10-15 02:20 ET (今天)
    assert row["state"] == "warn"
    assert "已拒绝" in row["summary"] and "失败 1" in row["summary"]



def test_open_radar_row_shows_push_switch_and_today_push(tmp_path, monkeypatch):
    """0.35.0: 开盘放量榜一行写出推送开关 / 门槛与今天推了哪些票。"""
    import json
    from datetime import date

    from app.market_clock import us_clock
    from app.services import open_radar
    d0 = date(2026, 10, 19)
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "today", lambda: d0)
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "open_radar")
    assert row["summary"].endswith("\uff1b外部推送开 \u00b7 相对量 \u22653x\uff09")   # external push on, rvol >= 3x
    open_radar.root(tmp_path).mkdir(parents=True)
    open_radar.day_path(tmp_path, d0).write_text(json.dumps({"day": d0.isoformat(), "at": "2026-10-19T09:51:00-04:00", "rows": [
        {"symbol": "NVDA", "rvol": 4.0, "pick": True}], "push": {"symbols": ["NVDA"]}}))
    open_radar.save_push_settings(tmp_path, enabled=False, min_rvol=5)
    row = _component(_status_app(tmp_path).get("/api/trading/system-status").json(), "open_radar")
    assert "NVDA\uff1b\u63a8\u9001 NVDA" in row["summary"]                                  # 点名 NVDA; 推送 NVDA
    assert row["summary"].endswith("外部推送关\uff09")                       # external push off
