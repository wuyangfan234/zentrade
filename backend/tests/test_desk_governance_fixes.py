"""批次③ 口径修正 (2026-10-05): 薄样本≠损坏、周限幅语义、缺档不重新 seed、先审计后授权。"""
import json
import os

import pytest

from app.services import decision_desk as dd
from app.services import decision_scan as ds


@pytest.mark.parametrize("ev", [{"n": 1, "mean": 0.4, "ci_low": None}, {"n": 10, "mean": 0.2, "ci_low": None},
                                {"n": 29, "mean": -0.1, "ci_low": None}])
def test_thin_sample_without_ci_is_explore_not_corrupt(ev):
    w = dd.source_weight(ev)
    assert w["tier"] == "explore" and w["risk_pct"] == dd.EXPLORE_RISK_PCT
    assert "损坏" not in w["reason"]


@pytest.mark.parametrize("ev", [{"n": 30, "mean": 0.1, "ci_low": None}, {"n": 500, "mean": 0.1},
                                {"n": 5, "mean": None, "ci_low": 0.1}, {"n": 5, "mean": float("nan")}])
def test_judgeable_sample_missing_ci_or_bad_mean_still_fails_closed(ev):
    assert dd.source_weight(ev)["tier"] == "negative"


def test_weekly_weight_holds_between_refreshes_and_caps_on_refresh():
    assert dd.weekly_weight(previous=1.0, target=5.0, due=False) == 1.0   # 未到期: 沿用上周
    assert dd.weekly_weight(previous=1.0, target=5.0, due=True) == 1.5    # 到期: 最多 +50%
    assert dd.weekly_weight(previous=1.0, target=0.2, due=True) == 0.5    # 到期: 最多 −50%
    assert dd.weekly_weight(previous=1.0, target=0.0, due=False) == 0.0   # 负证据立即归零
    assert dd.weekly_weight(previous=None, target=0.3, due=False) == 0.3  # 新转正直接取目标
    assert dd.weekly_weight(previous=0.0, target=0.3, due=True) == 0.3


def test_weights_from_evaluation_uses_due_flag(tmp_path):
    reg = dd.SourceRegistry(tmp_path, builtin={"good"})
    ev = {"sources": {"good": {"n": 1000, "mean": 0.5, "ci_low": 0.5}}}
    target = dd.source_weight(ev["sources"]["good"])["weight"]
    held = ds.weights_from_evaluation(ev, reg, {"good"}, {"good": 1.0}, due=False)["good"]
    moved = ds.weights_from_evaluation(ev, reg, {"good"}, {"good": 1.0}, due=True)["good"]
    assert held["weight"] == 1.0 and held["target_weight"] == target
    assert moved["weight"] == pytest.approx(min(target, 1.5)) and moved["weight"] != 1.0


def test_job_passes_previous_weights_even_when_due(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from types import SimpleNamespace
    from app.opportunity.engine import ScanResult
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation as se
    as_of = date(2026, 10, 9)
    (tmp_path / "decision_desk").mkdir(parents=True)
    (tmp_path / "decision_desk" / "weights.json").write_text(json.dumps(
        {"as_of": (as_of - timedelta(days=7)).isoformat(), "weights": {"good": 0.1}}))
    se.save_evaluation(tmp_path, {"sources": {"good": {"n": 1000, "mean": 0.5, "ci_low": 0.5}}})
    dd.SourceRegistry(tmp_path).set_status("good", "enabled")
    seen = {}
    real_build = ds.build

    def spy(**kw):
        seen.update(previous=kw["previous_weights"], due=kw["weights_due"])
        return real_build(**kw)
    monkeypatch.setattr(job.decision_scan, "build", spy)
    monkeypatch.setattr(job, "strategy_picks", lambda *a, **k: ({"good": []}, {"good": None}, {}))
    monkeypatch.setattr(job, "_phantom_4h_picks", lambda *a, **k: {})
    monkeypatch.setattr(job, "_watchlist", set)
    monkeypatch.setattr(job, "_news_shadow_snapshot", lambda *a: None)
    opp = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), sector_map=dict)
    params = {"revision": 3, "portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2, "max_portfolio_risk_pct": 6.0},
              "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}}
    job.run(opp, as_of, s1s2=ScanResult(as_of, gate="green", params_revision=3), frames={}, params=params,
            excluded=set(), risk_used_pct=0.0)
    assert seen == {"previous": {"good": 0.1}, "due": True}
    saved = json.loads((tmp_path / "decision_desk" / "weights.json").read_text())
    assert saved["weights"]["good"] == pytest.approx(0.15)  # 从 0.1 最多 +50%, 不是直接跳到目标


def test_seed_does_not_reenable_after_registry_loss(tmp_path):
    reg = dd.SourceRegistry(tmp_path)
    assert reg.seed({"a", "b"}) is True
    reg.set_status("a", "observe")
    reg.path.unlink()                          # 登记文件丢失, 审计还在
    assert reg.seed({"a", "b"}) is False
    assert reg.status("a") == "observe" and reg.status("b") == "observe"


def test_first_seed_still_enables_initial_sources(tmp_path):
    reg = dd.SourceRegistry(tmp_path)
    assert reg.seed({"a"}) is True and reg.status("a") == "enabled"


def test_audit_written_before_permission_and_failure_keeps_old_status(tmp_path, monkeypatch):
    reg = dd.SourceRegistry(tmp_path)
    reg.set_status("a", "observe")
    real_open = open

    def broken_open(path, *a, **k):
        if str(path) == str(reg.audit_path):
            raise OSError("disk full")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", broken_open)
    with pytest.raises(OSError):
        reg.set_status("a", "enabled")
    monkeypatch.setattr("builtins.open", real_open)
    assert reg.status("a") == "observe"


def test_replace_failure_is_marked_failed_in_audit(tmp_path, monkeypatch):
    reg = dd.SourceRegistry(tmp_path)
    reg.set_status("a", "observe")
    monkeypatch.setattr(dd.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError):
        reg.set_status("a", "enabled")
    monkeypatch.setattr(dd.os, "replace", os.replace)
    assert reg.status("a") == "observe"
    assert reg.audit()[-1].get("failed") is True


# ── 自选独立选股 + 动态名单 ──

def _bars(sym, n=80, base=100.0, step=0.3, atr=2.0):
    from datetime import date, timedelta
    import polars as pl
    d0 = date(2026, 10, 1) - timedelta(days=n - 1)
    return pl.DataFrame([{"symbol": sym, "date": d0 + timedelta(days=i), "open": base + step * i - 0.2,
                          "high": base + step * i + atr / 2, "low": base + step * i - atr / 2,
                          "close": base + step * i, "volume": 2e6} for i in range(n)])


def _build(tmp_path, picks, lane, hits=None, watchlist=()):
    from datetime import date
    from app.opportunity.engine import ScanResult
    reg = dd.SourceRegistry(tmp_path, builtin={"good", "meh"})
    frames = {s: _bars(s) for ss in picks.values() for s in ss}
    ev = {"sources": {"good": {"n": 1800, "mean": 0.13, "ci_low": 0.02}, "meh": {"n": 900, "mean": 0.0, "ci_low": -0.04}}}
    return ds.build(as_of=date(2026, 10, 1), strategy_picks=picks, s1s2=ScanResult(date(2026, 10, 1), gate="green", params_revision=3),
                    frames=frames, evaluation=ev, registry=reg, watchlist=set(watchlist), stop_losses={k: -0.06 for k in picks},
                    sector_map={s: f"S{i}" for i, s in enumerate(sorted(frames))}, portfolio_risk_used_pct=0.0,
                    active_symbols=set(), params={"portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2,
                    "max_portfolio_risk_pct": 6.0}, "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}, "revision": 3},
                    watchlist_lane=lane, source_hits=hits or {})


def test_watchlist_lane_card_is_marked_lane_only_with_raw_rank(tmp_path):
    hits = {"good": [{"symbol": f"P{i}", "score": 10 - i, "rank": i + 1} for i in range(3)]
            + [{"symbol": "NVDA", "score": 1.5, "rank": 37}]}
    out = _build(tmp_path, {"good": ["P0", "NVDA"]}, {"good": {"NVDA"}}, hits, watchlist={"NVDA"})
    cards = {c.symbol: c for c in out.result.cards}
    d = cards["NVDA"].evidence["decision"]
    assert d["tab"] == "watchlist" and d["lane_only"] is True and d["via_watchlist_lane"] is True
    assert d["source_scores"]["good"] == {"via": "watchlist", "score": 1.5, "rank": 37, "total": 4}
    assert d["risk_pct"] == dd.EXPLORE_RISK_PCT and d["lane_risk_capped"] is True   # 证据外的名次按探索档风险
    assert cards["P0"].evidence["decision"]["lane_only"] is False
    assert cards["P0"].evidence["decision"]["risk_pct"] == dd.PROVEN_RISK_PCT


def test_pool_top_pick_in_watchlist_is_not_lane_only(tmp_path):
    out = _build(tmp_path, {"good": ["NVDA"]}, {}, watchlist={"NVDA"})
    assert out.result.cards[0].evidence["decision"]["lane_only"] is False


def test_removed_from_watchlist_blocks_new_entry_for_lane_only_card(tmp_path, monkeypatch):
    from datetime import date, datetime
    from app.market_clock import us_clock
    from app.opportunity.card import CardState, OpportunityCard
    from app.services import decision_scan_job as job, entry_guard
    from app.services import strategy_evaluation as se
    se.save_evaluation(tmp_path, {"sources": {"good": {"n": 1800, "mean": 0.13, "ci_low": 0.02}}})
    dd.SourceRegistry(tmp_path).set_status("good", "enabled")
    card = OpportunityCard("c", "NVDA", "strategy:good", date(2026, 10, 1), (100., 101.), 95., (112., 114.),
                           risk_per_share=5.5, reward_risk=2.1, state=CardState.ACTIVE,
                           evidence={"decision": {"tier": "proven", "risk_pct": 0.01, "tradable_sources": ["good"],
                                                  "lane_only": True}})
    now = datetime(2026, 10, 2, 11, tzinfo=us_clock.tz)
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA"})
    adm = entry_guard.evaluate(card, data_dir=tmp_path, now=now)
    assert adm.allowed and adm.risk_pct == dd.EXPLORE_RISK_PCT and adm.explore is False   # 不占探索每日额度
    monkeypatch.setattr(job, "_watchlist", lambda: set())
    assert "watchlist_removed" in entry_guard.evaluate(card, data_dir=tmp_path, now=now).blockers
    plain = OpportunityCard("d", "AAA", "strategy:good", date(2026, 10, 1), (100., 101.), 95., (112., 114.),
                            risk_per_share=5.5, reward_risk=2.1, state=CardState.ACTIVE,
                            evidence={"decision": {"tier": "proven", "risk_pct": 0.01, "tradable_sources": ["good"]}})
    assert entry_guard.evaluate(plain, data_dir=tmp_path, now=now).allowed  # 旧卡/全池卡不受自选名单影响


def test_job_builds_watchlist_lane_from_full_hits_and_writes_funnel(tmp_path, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from app.opportunity.engine import ScanResult
    from app.services import decision_scan_job as job
    from app.services import strategy_evaluation as se
    as_of = date(2026, 10, 1)
    se.save_evaluation(tmp_path, {"sources": {"good": {"n": 1800, "mean": 0.13, "ci_low": 0.02}}})
    dd.SourceRegistry(tmp_path).set_status("good", "enabled")
    frames = {s: _bars(s) for s in ("P0", "P1", "NVDA", "MU")}
    hits = {"good": [{"symbol": "P0", "score": 9, "rank": 1}, {"symbol": "P1", "score": 8, "rank": 2},
                     {"symbol": "NVDA", "score": 3, "rank": 3}]}
    monkeypatch.setattr(job, "strategy_picks", lambda *a, **k: ({"good": ["P0"]}, {"good": -0.06},
                                                                 {"strategies": 1, "hits": dict(hits)}))
    monkeypatch.setattr(job, "_phantom_4h_picks", lambda *a, **k: {})
    monkeypatch.setattr(job, "_watchlist", lambda: {"NVDA", "MU", "GONE"})
    monkeypatch.setattr(job, "_news_shadow_snapshot", lambda *a: None)
    opp = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                          sector_map=lambda: {s: f"S{i}" for i, s in enumerate(frames)}, industry_map=dict)
    params = {"revision": 3, "portfolio": {"daily_new_cards_max": 5, "sector_max_active": 2, "max_portfolio_risk_pct": 6.0},
              "market_gate": {"caution_card_max": 2, "caution_risk_pct": 3.0}}
    out = job.run(opp, as_of, s1s2=ScanResult(as_of, gate="green", params_revision=3), frames=frames, params=params,
                  excluded=set(), risk_used_pct=0.0)
    assert {c.symbol for c in out.result.cards} == {"P0", "NVDA"}   # P1 不是自选, 不进通道
    saved = job.latest_run(tmp_path)
    f = saved["funnel"]
    assert set(f) == {"GONE", "MU", "NVDA"}
    assert f["GONE"]["stage"] == "pool" and f["MU"]["stage"] == "no_signal"
    assert f["NVDA"]["stage"] == "card" and f["NVDA"]["hits"][0]["via"] == "watchlist" and f["NVDA"]["hits"][0]["rank"] == 3
    assert saved["summary"]["watchlist_snapshot"] == ["GONE", "MU", "NVDA"] and saved["summary"]["watchlist_lane"] == {"good": 1}


def test_news_shadow_open_at_end_stays_pending_and_retries(tmp_path):
    from datetime import date, timedelta
    import polars as pl
    from app.services import news_shadow as nsh
    as_of = date(2026, 9, 1)
    nsh._save_rows(tmp_path, [{"as_of": as_of.isoformat(), "symbol": "AAA", "entry_zone": [100, 101], "invalidation": 95,
                               "target_zone": [140, 145], "risk": 5.5, "side": "long", "high_bearish": False,
                               "high_bullish": False, "outcome": {"status": "open_at_end", "r": None}}])

    def bars(n):  # 前 4 天在入场区上方不成交, 第 5 天才成交 → 10 根走不完 7 日持有
        return pl.DataFrame([{"symbol": "AAA", "date": as_of + timedelta(days=i + 1),
                              **({"open": 101.5, "high": 102., "low": 101.2, "close": 101.5} if i < 4 else
                                 {"open": 100.5, "high": 101., "low": 100., "close": 100.5})} for i in range(n)])
    repo = type("R", (), {"get_daily_batch": lambda self, *a, **k: bars(10)})()
    assert nsh.resolve_outcomes(tmp_path, repo, today=as_of + timedelta(days=20)) == 0
    assert nsh.evidence(tmp_path)["pending"] == 1
    repo = type("R", (), {"get_daily_batch": lambda self, *a, **k: bars(14)})()
    assert nsh.resolve_outcomes(tmp_path, repo, today=as_of + timedelta(days=30)) == 1
    row = nsh.load_rows(tmp_path)[0]
    assert row["outcome"]["status"] == "closed" and nsh.evidence(tmp_path)["resolved"] == 1


def test_strategy_picks_disables_display_limit_before_pool_filter(tmp_path, monkeypatch):
    """池内强信号排在全市场第 100 名之后也要能进候选: 决策台调用引擎时 display_limit=0。"""
    from datetime import date
    from types import SimpleNamespace
    from app.services import decision_scan_job as job
    from app.strategy.engine import StrategyResult
    seen = {}

    class Engine:
        def list_strategies(self):
            return [{"id": "s1", "asset_types": ["stock"], "timeframes": ["1d"]}]

        def get(self, sid):
            return SimpleNamespace(stop_loss=-0.05)

        def run_all(self, context, overrides_map=None, strategy_ids=None):
            seen.update(overrides_map)
            ov = overrides_map["s1"]
            n = 150 if ov.get("display_limit") == 0 else 100   # 模拟引擎截断
            rows = [{"symbol": f"X{i:03d}", "score": 1000 - i} for i in range(n)]
            return {"s1": StrategyResult(as_of=date(2026, 10, 1), strategy_id="s1", rows=rows, total=n)}

    monkeypatch.setattr(job, "_app_state", lambda: SimpleNamespace(strategy_engine=Engine()))
    import app.services.screener as screener
    monkeypatch.setattr(screener.ScreenerService, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(screener.ScreenerService, "build_strategy_context", lambda self, *a, **k: None)
    from app.strategy import config as strategy_config
    monkeypatch.setattr(strategy_config, "load_override", lambda d, sid: {"stop_loss": -0.04})
    universe = {f"X{i:03d}" for i in range(120, 150)}      # 池内股票全在全市场 100 名之外
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    picks, stops, meta = job.strategy_picks(repo, date(2026, 10, 1), universe)
    assert seen["s1"]["display_limit"] == 0 and seen["s1"]["stop_loss"] == -0.04
    assert picks["s1"] == [f"X{i:03d}" for i in range(120, 130)] and stops["s1"] == -0.04
    assert len(meta["hits"]["s1"]) == 30 and meta["hits"]["s1"][0] == {"symbol": "X120", "score": 880.0, "rank": 1}
