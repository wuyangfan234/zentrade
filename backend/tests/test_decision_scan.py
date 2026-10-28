"""Daily decision-desk scan: strategy picks → unified cards → weights/resonance/explore quota → portfolio
constraints → saved as the day's cards next to S1/S2 (same account, same risk controls, same lifecycle)."""
from datetime import date, timedelta
import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.opportunity.card import CardState, OpportunityCard
from app.opportunity.engine import ScanResult
from app.services import decision_desk as dd
from app.services import decision_scan as ds

AS_OF = date(2026, 10, 1)


def bars(sym, n=80, base=100.0, step=0.3, atr=2.0):
    d0 = AS_OF - timedelta(days=n - 1)
    return pl.DataFrame([{"symbol": sym, "date": d0 + timedelta(days=i), "open": base + step * i - 0.2,
                          "high": base + step * i + atr / 2, "low": base + step * i - atr / 2,
                          "close": base + step * i, "volume": 2e6} for i in range(n)])


EVAL = {"sources": {
    "breakout": {"n": 1395, "mean": -0.077, "ci_low": -0.152},
    "pullback": {"n": 1446, "mean": -0.013, "ci_low": -0.106},
    "good": {"n": 1800, "mean": 0.13, "ci_low": 0.02},
    "meh": {"n": 900, "mean": 0.01, "ci_low": -0.04},
    "thin": {"n": 12, "mean": 0.3, "ci_low": -0.5},
}}


def run(tmp_path, picks, s1s2=None, watchlist=(), monkeypatch=None):
    frames = {s: bars(s) for ss in picks.values() for s in ss}
    for c in (s1s2.cards if s1s2 else []):
        frames.setdefault(c.symbol, bars(c.symbol))
    reg = dd.SourceRegistry(tmp_path, builtin={"breakout", "pullback", "good", "meh", "thin"})
    return ds.build(as_of=AS_OF, strategy_picks=picks, s1s2=s1s2 or ScanResult(AS_OF, gate="green", params_revision=3),
                    frames=frames, evaluation=EVAL, registry=reg, watchlist=set(watchlist),
                    stop_losses={k: -0.06 for k in picks}, sector_map={s: f"S{i % 4}" for i, s in enumerate(sorted(frames))},
                    portfolio_risk_used_pct=0.0, active_symbols=set(), params={"portfolio": {"daily_new_cards_max": 5,
                    "sector_max_active": 2, "max_portfolio_risk_pct": 6.0}, "market_gate": {"caution_card_max": 2,
                    "caution_risk_pct": 3.0}, "revision": 3})


def test_positive_source_cards_open_negative_sources_only_watch(tmp_path):
    s1 = OpportunityCard(OpportunityCard.make_id("BRK1", "breakout", AS_OF, 3), "BRK1", "breakout", AS_OF, (100., 101.), 95.,
                         (112., 114.), risk_per_share=5.5, reward_risk=2.1, state=CardState.OPEN)
    out = run(tmp_path, {"good": ["AAA", "BBB"], "meh": ["CCC"]},
              s1s2=ScanResult(AS_OF, cards=[s1], gate="green", params_revision=3))
    opened = {c.symbol: c for c in out.result.cards}
    watched = {c.symbol: c for c in out.result.watch}
    assert set(opened) == {"AAA", "BBB"}
    assert "CCC" in watched and "置信下界" in watched["CCC"].state_reason
    assert "BRK1" in watched and "置信下界" in watched["BRK1"].state_reason  # S1 negative evidence → weight 0
    a = opened["AAA"]
    assert a.setup_type == "strategy:good" and a.max_holding_days == 7
    assert a.evidence["decision"]["tier"] == "proven" and a.evidence["decision"]["risk_pct"] == pytest.approx(0.01)


def test_resonance_ranks_first_and_lists_all_sources(tmp_path):
    out = run(tmp_path, {"good": ["AAA", "BBB"], "thin": ["BBB"]})
    first = out.result.cards[0]
    assert first.symbol == "BBB" and sorted(first.evidence["decision"]["sources"]) == ["good", "thin"]
    assert first.evidence["decision"]["resonance"] == 2


def test_explore_source_limited_to_one_card_at_quarter_risk(tmp_path):
    out = run(tmp_path, {"thin": ["T1", "T2", "T3"]})
    explore = [c for c in out.result.cards if c.evidence["decision"]["tier"] == "explore"]
    assert len(explore) == 1 and explore[0].evidence["decision"]["risk_pct"] == pytest.approx(0.0025)
    assert sum(1 for c in out.result.watch if "探索额度" in c.state_reason) == 2


def test_watchlist_tab_marked_and_same_rules(tmp_path):
    out = run(tmp_path, {"good": ["AAA", "NVDA"]}, watchlist={"NVDA"})
    tabs = {c.symbol: c.evidence["decision"]["tab"] for c in out.result.cards}
    assert tabs == {"AAA": "pool", "NVDA": "watchlist"}


def test_disabled_source_never_opens(tmp_path):
    reg = dd.SourceRegistry(tmp_path, builtin={"good"})
    reg.set_status("good", "disabled", note="test")
    out = ds.build(as_of=AS_OF, strategy_picks={"good": ["AAA"]}, s1s2=ScanResult(AS_OF, gate="green", params_revision=3),
                   frames={"AAA": bars("AAA")}, evaluation=EVAL, registry=reg, watchlist=set(), stop_losses={"good": -0.06},
                   sector_map={"AAA": "S1"}, portfolio_risk_used_pct=0.0, active_symbols=set(),
                   params={"portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2, "max_portfolio_risk_pct": 6.0},
                           "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}, "revision": 3})
    assert not out.result.cards and "停用" in out.result.watch[0].state_reason


def test_yellow_gate_and_daily_cap_still_apply(tmp_path):
    out = run(tmp_path, {"good": [f"S{i}" for i in range(9)]})
    assert len(out.result.cards) <= 5
    assert sum(1 for c in out.result.watch if "每日新卡上限" in c.state_reason or "行业" in c.state_reason) >= 4


def test_summary_counts_by_source_for_push(tmp_path):
    out = run(tmp_path, {"good": ["AAA"], "meh": ["CCC"]})
    assert out.summary["by_source"]["good"]["opened"] == 1 and out.summary["by_source"]["meh"]["watched"] == 1


def test_job_wires_engine_picks_and_writes_run_summary(tmp_path, monkeypatch):
    """decision_scan_job.run: picks from StrategyEngine rows (universe-filtered, top 10), evidence file, summary on disk."""
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation as se
    se.save_evaluation(tmp_path, EVAL)
    frames = {s: bars(s) for s in ("AAA", "BBB", "OUT")}
    monkeypatch.setattr(job, "strategy_picks", lambda repo, as_of, universe: (
        {"good": [s for s in ["AAA", "OUT", "BBB"] if s in universe]}, {"good": -0.06}, {"strategies": 1}))
    monkeypatch.setattr(job, "_watchlist", lambda: {"BBB"})
    opp = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                          sector_map=lambda: {"AAA": "Tech", "BBB": "Health"})
    params = {"portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2, "max_portfolio_risk_pct": 6.0},
              "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}, "revision": 3}
    out = job.run(opp, AS_OF, s1s2=ScanResult(AS_OF, gate="green", params_revision=3), frames=frames, params=params,
                  excluded=set(), risk_used_pct=0.0)
    assert {c.symbol for c in out.result.cards} == {"AAA", "BBB"}
    saved = job.latest_run(tmp_path)
    assert saved["summary"]["opened"] == 2 and saved["summary"]["weights_refreshed"] is True
    assert (tmp_path / "decision_desk" / "weights.json").exists()


# ── trading loop honours the decision desk ──

from datetime import datetime  # noqa: E402

from app.ledger.core import Account  # noqa: E402
from app.market_clock import us_clock  # noqa: E402
from app.services.trading_loop import TradingLoop  # noqa: E402


@pytest.fixture
def loop_rig(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 2, 11, tzinfo=us_clock.tz)]
    monkeypatch.setattr(us_clock, "now", lambda: clock[0])
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["AAA", "OLD"], "sector": ["Tech", "Health"]}),
                           get_daily=lambda sym, start, end: pl.DataFrame({"date": [end], "close": [100.], "raw_close": [100.]}))
    loop = TradingLoop(repo, Account())
    loop.boot()
    monkeypatch.setattr(loop.earnings, "usable", lambda *a: True)
    monkeypatch.setattr(loop.earnings, "event_for", lambda *a: None)
    monkeypatch.setattr(loop.corp_actions, "usable", lambda *a: True)
    monkeypatch.setattr(loop.corp_actions, "covers", lambda *a: True)
    monkeypatch.setattr(loop.opp_svc, "revalidate_unfilled", lambda now: None)
    return loop, clock, tmp_path


def _q(sym, at, price=100.0):
    return {sym: {"symbol": sym, "price": price, "timestamp": int(at.timestamp() * 1000), "feed": "tickflow",
                  "open": 100.0, "open_timestamp": int(at.replace(hour=9, minute=30, second=0).timestamp() * 1000)}}


def _save(loop, card):
    loop.opp_svc.save_scan(ScanResult(card.created_date, cards=[card], gate="green", params_revision=3))


def test_old_card_from_zero_weight_source_cannot_enter(loop_rig):
    loop, clock, tmp = loop_rig
    from app.services import decision_scan_job as job
    (tmp / "decision_desk" / "runs").mkdir(parents=True)
    (tmp / "decision_desk" / "runs" / "2026-10-01.json").write_text(json.dumps({"summary": {"weights": {
        "breakout": {"weight": 0.0, "tier": "negative", "status": "enabled"}}}}))
    old = OpportunityCard("old-OLD", "OLD", "breakout", date(2026, 9, 30), (99., 102.), 94., (110., 120.), risk_per_share=6.,
                          reward_risk=2., evidence={"sector": "Health"}, state=CardState.OPEN)
    _save(loop, old)
    for _ in range(3):
        loop.intraday_fill_check(_q("OLD", clock[0]))
        clock[0] += timedelta(seconds=15)
    assert not loop.ledger.open_positions
    assert _has_blocker(loop.entry_blockers, "OLD", "source_not_tradable:breakout")  # 0.26.0: 逐卡拦截另带 card_id
    assert job.source_tradable(tmp, "breakout") is False


def test_explore_card_sizes_at_quarter_percent_risk(loop_rig):
    loop, clock, tmp = loop_rig
    # Explicit current permission/evidence: absence is no longer an implicit grant.
    from app.services import strategy_evaluation
    dd.SourceRegistry(tmp).set_status("thin", "enabled")
    strategy_evaluation.save_evaluation(tmp, {"sources": {"thin": {"n": 0}}})
    card = OpportunityCard("exp-AAA", "AAA", "strategy:thin", date(2026, 10, 1), (99., 102.), 94., (110., 120.),
                           risk_per_share=6., reward_risk=2., state=CardState.OPEN,
                           evidence={"sector": "Tech", "decision": {"tier": "explore", "risk_pct": 0.0025}})
    _save(loop, card)
    for _ in range(3):
        loop.intraday_fill_check(_q("AAA", clock[0]))
        clock[0] += timedelta(seconds=15)
    pos = loop.ledger.position("AAA")
    assert pos is not None and pos.qty == 41  # 0.25% of ~$100k / $6 = 41 shares (vs 166 at 1%)


def test_rescan_endpoint_runs_in_process_after_close_only(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.opportunities import router
    calls = []
    loop = SimpleNamespace(
        opp_svc=SimpleNamespace(list_card_days=lambda: ["2026-10-01"],
                                run_daily_scan=lambda day: calls.append(day) or ScanResult(day, gate="green", params_revision=3),
                                desk_summary={"opened": 0}),
        _sync_card_book=lambda: calls.append("sync"))
    app = FastAPI()
    app.include_router(router)
    app.state.trading_loop = loop
    app.state.quote_service = SimpleNamespace(is_official_close=lambda d: True)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(us_clock, "in_session", lambda *a: True)
    assert TestClient(app).post("/api/opportunities/desk/rescan").status_code == 409
    monkeypatch.setattr(us_clock, "in_session", lambda *a: False)
    r = TestClient(app).post("/api/opportunities/desk/rescan")
    assert r.status_code == 200 and calls == [date(2026, 10, 1), "sync"]


def _has_blocker(blockers, symbol, reason):
    """逐卡拦截 (0.26.0 起带 card_id, 漏斗按它归属): 按 symbol+reason 匹配, 且逐卡拦截必须带 card_id。"""
    hit = [b for b in blockers if b.get("symbol") == symbol and b.get("reason") == reason]
    return bool(hit) and all(b.get("card_id") for b in hit if symbol != "*")
