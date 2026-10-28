"""机会页总览 (批次① 2026-10-05): 跨日在窗卡叠加当前许可、账户容量、自选总表。只读。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.services import decision_desk as dd
from app.services import desk_overview as ov
from app.services import strategy_evaluation as se
from app.services.opportunity_service import OpportunityService

TODAY = date(2026, 10, 5)  # 周一


def card(sym, source, created, state=CardState.WATCH, rr=4.9, reason="", decision=None, zone=(85.61, 86.47), inval=81.74):
    setup = source if source in dd.BUILTIN_SETUPS else f"strategy:{source}"
    return OpportunityCard(OpportunityCard.make_id(sym, setup.replace(":", "-"), created, 3), sym, setup, created, zone, inval,
                           (104.99, 107.13), risk_per_share=round((zone[0] + zone[1]) / 2 - inval, 4), reward_risk=rr,
                           state=state, state_reason=reason,
                           evidence={"decision": decision or {"sources": [source], "tier": "proven", "risk_pct": 0.01,
                                                              "tradable_sources": [source]}, "sector": "X"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "today", lambda: TODAY)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=us_clock.tz))
    se.save_evaluation(tmp_path, {"generated_at": "x", "sources": {
        "good": {"n": 1797, "mean": 0.135, "ci_low": 0.007}, "bad": {"n": 10238, "mean": -0.025, "ci_low": -0.056}}})
    reg = dd.SourceRegistry(tmp_path)
    reg.set_status("good", "enabled")
    reg.set_status("bad", "enabled")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_daily=lambda s, a, b: pl.DataFrame({"date": [date(2026, 10, 2)], "close": [30.0]}))
    svc = OpportunityService(repo)
    lg = Ledger(tmp_path / "ledger")
    lg.init_account(Account())
    lg.replay(Account())
    svc.bind_ledger(lg)
    from app.services import watchlist as wl
    monkeypatch.setattr(wl, "list_symbols", lambda: [{"symbol": "TIGO"}, {"symbol": "NVDA"}, {"symbol": "NEW"}])
    return SimpleNamespace(tmp=tmp_path, repo=repo, svc=svc, ledger=lg)


def save(svc, day, cards, watch=()):
    p = svc._cards_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"as_of": day.isoformat(), "gate": "green", "params_revision": 3, "scanned": 2000,
                             "cards": [c.to_dict() for c in cards], "watch": [c.to_dict() for c in watch]}))


def test_cross_day_executable_cards_use_current_permission(env):
    old = card("TIGO", "good", date(2026, 10, 1), reason="组合风险超限")
    neg = card("A", "bad", date(2026, 10, 1), state=CardState.WATCH,
               decision={"sources": ["bad"], "tier": "negative", "risk_pct": 0.0, "tradable_sources": []})
    stale = card("OLD", "good", date(2026, 9, 24))                    # 入场窗已过
    save(env.svc, date(2026, 10, 1), [], [old, neg, stale])
    save(env.svc, date(2026, 10, 2), [], [card("DHR", "bad", date(2026, 10, 2))])
    env.ledger.append("opportunity", {"card_id": neg.id, "symbol": "A", "state": "active", "reason": "观察卡入区且组合容量恢复",
                                      "trade_date": "2026-10-02"}, at="2026-10-02T09:31:00-04:00")
    out = ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)
    syms = [r["symbol"] for r in out["executable"]]
    assert syms == ["TIGO"]                    # 负证据的 active 卡不算可执行, 过窗卡不出现
    assert out["live_other_count"] == 2        # A (旧代码升上来的 active) + DHR
    t = out["executable"][0]
    assert t["window"]["open"] and t["window"]["last_day"] == "2026-10-08"
    assert t["capacity"]["verdict"] == "ok" and t["capacity"]["planned_qty"] > 0
    assert "长下影" not in t["explain"] and "证据为正" in t["explain"] and "入场窗剩" in t["explain"]
    st = out["status"]
    assert [s["id"] for s in st["tradable_sources"]] == ["good"]
    assert st["circuit_breaker"] is None       # 未评估就是未评估, 不是“正常”
    assert st["executable"] == 1 and st["latest_scan"] == "2026-10-02"


def test_capacity_reports_downsize_and_cash_block(env):
    env.ledger.append("fill", dict(symbol="CVE", side="buy", qty=2800, price=30.0, fee=0, stop_price=30.0),
                      at="2026-10-01T10:00:00-04:00")            # 剩 $16,000 现金
    save(env.svc, date(2026, 10, 1), [], [card("TIGO", "good", date(2026, 10, 1))])
    out = ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)
    cap = out["executable"][0]["capacity"]
    assert cap["verdict"] == "downsized" and cap["downsized_qty"] < cap["planned_qty"]
    acc = out["status"]["account"]
    assert acc["cash"] == pytest.approx(16000.0) and acc["positions"] == 1 and "close" in acc["marks_origin"]
    env.ledger.append("fill", dict(symbol="DOCS", side="buy", qty=500, price=30.0, fee=0, stop_price=30.0),
                      at="2026-10-01T10:01:00-04:00")            # 剩 $1,000
    out = ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)
    assert out["executable"][0]["capacity"]["verdict"] == "cash"
    assert any("现金不足" in b for b in out["executable"][0]["blockers"])


def test_watchlist_table_one_row_per_current_member_with_funnel(env):
    save(env.svc, date(2026, 10, 1), [], [card("TIGO", "good", date(2026, 10, 1))])
    runs = env.tmp / "decision_desk" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-10-02.json").write_text(json.dumps({"summary": {"as_of": "2026-10-02", "watchlist_snapshot": ["NVDA", "TIGO", "GONE"]},
        "ranked": [], "funnel": {"NVDA": {"stage": "pool", "reason": "近 30 日均成交额 $1.0M 低于 $20M"},
                                 "TIGO": {"stage": "no_signal", "reason": "当日无来源命中"}}}))
    out = ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)
    wl = out["watchlist"]
    rows = {r["symbol"]: r for r in wl["rows"]}
    assert set(rows) == {"TIGO", "NVDA", "NEW"} and wl["removed_since_scan"] == ["GONE"]
    assert rows["NVDA"]["stage"] == "pool" and "成交额" in rows["NVDA"]["reason"]
    assert rows["NEW"]["stage"] == "pending" and rows["NEW"]["added_after_scan"] is True
    assert rows["TIGO"]["card"]["state"] == "watch" and rows["TIGO"]["card"]["tradable"] is True


def test_watchlist_unrecorded_when_scan_predates_diagnostics(env):
    runs = env.tmp / "decision_desk" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-10-02.json").write_text(json.dumps({"summary": {"as_of": "2026-10-02"}, "ranked": []}))
    rows = {r["symbol"]: r for r in ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)["watchlist"]["rows"]}
    assert rows["NVDA"]["stage"] == "unrecorded" and "未记录" in rows["NVDA"]["reason"]


def test_lane_only_card_removed_from_watchlist_shows_blocker(env):
    c = card("GONE", "good", date(2026, 10, 1), decision={"sources": ["good"], "tier": "proven", "risk_pct": 0.01,
                                                          "tradable_sources": ["good"], "lane_only": True})
    save(env.svc, date(2026, 10, 1), [], [c])
    r = ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)["executable"][0]
    assert any("移出自选" in b for b in r["blockers"])


def test_rr_suspect_flag(env):
    save(env.svc, date(2026, 10, 1), [], [card("CTVA", "good", date(2026, 10, 1), rr=131.7)])
    assert ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)["executable"][0]["rr_suspect"] is True


def test_overview_is_read_only(env):
    save(env.svc, date(2026, 10, 1), [], [card("TIGO", "good", date(2026, 10, 1))])
    before = {p: p.read_bytes() for p in env.tmp.rglob("*") if p.is_file()}
    ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)
    after = {p: p.read_bytes() for p in env.tmp.rglob("*") if p.is_file()}
    assert before == after


def test_api_route(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import opportunities as api
    save(env.svc, date(2026, 10, 1), [], [card("TIGO", "good", date(2026, 10, 1))])
    app = FastAPI()
    app.include_router(api.router)
    app.state.opportunity_service = env.svc
    app.state.repo = env.repo
    r = TestClient(app).get("/api/opportunities/desk/overview")
    assert r.status_code == 200 and r.json()["executable"][0]["symbol"] == "TIGO"


def test_watch_card_blocked_by_sector_occupancy_counts_only_tradable_live_cards(env):
    a = card("AAA", "good", date(2026, 10, 1), state=CardState.ACTIVE)
    b = card("BBB", "good", date(2026, 10, 1), state=CardState.ACTIVE)
    neg = card("NNN", "bad", date(2026, 10, 1), state=CardState.ACTIVE,
               decision={"sources": ["bad"], "tier": "negative", "risk_pct": 0.0, "tradable_sources": []})
    w = card("WWW", "good", date(2026, 10, 1))
    save(env.svc, date(2026, 10, 1), [a, b], [w])
    rows = {r["symbol"]: r for r in ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)["executable"]}
    assert any("行业 X 已有 2 张" in x for x in rows["WWW"]["blockers"])
    save(env.svc, date(2026, 10, 1), [a, neg], [w])   # 负证据 active 卡不占名额 (会被准入守卫打回观察)
    rows = {r["symbol"]: r for r in ov.build(svc=env.svc, repo=env.repo, data_dir=env.tmp)["executable"]}
    assert rows["WWW"]["blockers"] == []
