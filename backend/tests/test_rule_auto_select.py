"""自动选策略: 每周按规则前向成绩评一次, 每侧最多换 1 条; 可关、可撤回; 不碰交易链。"""
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import rule_auto_select as ras
from app.services import rule_forward as rf
from app.services import swing_desk as sd

FRI = date(2026, 12, 4)
THU = date(2026, 12, 3)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _r(rule, side, excess, verdict, *, filled=40, days=25, hits=60, ci=None):
    return {"rule": rule, "side": side, "hits": hits, "filled": filled, "days": days, "fill": 0.6, "mean_r": 0.1, "win": 0.5,
            "excess": excess, "ci": ci or ([excess - 0.1, excess + 0.1] if excess is not None else None), "verdict": verdict}


def _table(rows, gate=(), days=30, as_of=FRI):
    return {"contract": rf.CONTRACT, "days": days, "cells": 2000, "window": ["2026-10-15", "2026-12-03"], "as_of": as_of.isoformat(),
            "scopes": {"all": {"compared": len(rows), "rules": list(rows)}, "gate": {"compared": 0, "rules": list(gate)}}}


CUR = {"long": ["a", "b", "c"], "short": ["x", "y", "z"]}
OK = {"long": {"a", "b", "c", "d", "e"}, "short": {"x", "y", "z", "w"}}
BASE = [_r("a", "long", 0.20, "above"), _r("b", "long", 0.02, "unclear"), _r("c", "long", -0.15, "below"),
        _r("x", "short", 0.10, "unclear"), _r("y", "short", 0.05, "unclear"), _r("z", "short", 0.01, "unclear")]


def _p(rows, **kw):
    kw.setdefault("swaps", [])
    return ras.propose(_table(rows, kw.pop("gate", ()), kw.pop("days", 30)), kw.pop("cur", CUR), kw.pop("ok", OK), kw["swaps"], FRI,
                       kw.get("days_since", lambda d0: 99))


def test_swaps_lowest_incumbent_for_best_candidate_above_zero_one_per_side():
    p = _p([*BASE, _r("d", "long", 0.30, "above"), _r("e", "long", 0.40, "above"), _r("w", "short", 0.25, "above")])
    assert (p["long"]["action"], p["long"]["out"], p["long"]["in"]) == ("swap", "c", "e")
    assert (p["long"]["out_excess"], p["long"]["in_excess"]) == (-0.15, 0.4)
    assert (p["short"]["out"], p["short"]["in"]) == ("z", "w")


def test_candidate_must_be_above_zero_with_enough_days():
    for v in ("early", "unclear", "thin", "below"):
        assert _p([*BASE, _r("d", "long", 0.9, v)])["long"] == {"action": "none", "reason": "no_candidate"}


def test_candidate_outside_catalog_or_negative_in_gate_is_skipped():
    rows = [*BASE, _r("d", "long", 0.30, "above")]
    assert _p(rows, ok={**OK, "long": {"a", "b", "c"}})["long"]["reason"] == "no_candidate"
    assert _p(rows, gate=[_r("d", "long", -0.05, "early", filled=6)])["long"]["reason"] == "no_candidate"
    assert _p(rows, gate=[_r("d", "long", -0.05, "thin", filled=3)])["long"]["action"] == "swap"     # 门内样本太少不否决
    assert _p(rows, ok={**OK, "long": None})["long"] == {"action": "none", "reason": "catalog_unavailable"}


def test_incumbents_that_lead_or_lack_samples_are_not_swapped_out():
    rows = [_r("a", "long", 0.20, "above"), _r("b", "long", 0.25, "above"), _r("c", "long", None, "thin", filled=4, days=3, hits=6),
            _r("d", "long", 0.30, "above")]
    assert _p(rows)["long"] == {"action": "none", "reason": "incumbents_hold", "best": "d"}
    rows[2] = _r("c", "long", 0.35, "unclear")                                         # 入选的比候补还高 -> 不换
    assert _p(rows)["long"]["reason"] == "incumbents_hold"


def test_incumbent_with_zero_hits_counts_as_lowest_only_after_enough_days():
    rows = [_r("a", "long", 0.05, "unclear"), _r("b", "long", 0.02, "unclear"), _r("d", "long", 0.30, "above")]
    p = _p(rows)                                                                       # c 整段 0 命中
    assert (p["long"]["out"], p["long"]["out_excess"], p["long"]["out_hits"]) == ("c", None, 0)
    assert _p(rows, days=12)["long"]["out"] == "b"                                     # 记分还不满 20 日: 不把 0 命中当最低


def test_cooldown_after_auto_swap_and_undone_swap_is_not_repeated():
    rows = [*BASE, _r("d", "long", 0.30, "above"), _r("e", "long", 0.25, "above")]
    sw = [{"id": "2026-11-20-long", "day": "2026-11-20", "side": "long", "out": "q", "in": "a", "undone_at": None}]
    assert _p(rows, swaps=sw, days_since=lambda d0: 9)["long"] == {"action": "none", "reason": "cooldown", "since": "2026-11-20"}
    assert _p(rows, swaps=sw, days_since=lambda d0: 20)["long"]["in"] == "d"
    und = [{"id": "2026-11-20-long", "day": "2026-11-20", "side": "long", "out": "c", "in": "d", "undone_at": "2026-11-21T10:00:00"}]
    p = _p(rows, swaps=und, days_since=lambda d0: 2)                                    # 撤回过的不算冷却, 但不再换回 d
    assert p["long"]["in"] == "e" and _p(rows, swaps=sw, days_since=lambda d0: 9)["short"]["reason"] == "no_candidate"


def test_eval_day_is_last_trading_day_of_week(cal):
    assert ras.is_eval_day(FRI) and not ras.is_eval_day(THU)
    assert ras.next_eval_day(FRI) == date(2026, 12, 11) and ras.next_eval_day(date(2026, 12, 5)) == date(2026, 12, 11)


def _seed(tmp_path, monkeypatch, rows, as_of=FRI):
    sd.save_strategies(tmp_path, CUR["long"], CUR["short"])
    p = rf.summary_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_table(rows, as_of=as_of)))
    monkeypatch.setattr(ras, "eligible", lambda data_dir, engine: OK)
    monkeypatch.setattr(sd, "strategy_name", lambda rule, side="long": f"N-{rule}")
    sent = []
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "enqueue_global", lambda data_dir, key, payload, **kw: sent.append((key, payload, kw)) or True)
    return sent


ROWS = [*BASE, _r("d", "long", 0.30, "above")]


def test_run_weekly_applies_swap_logs_it_and_notifies_once(tmp_path, monkeypatch, cal):
    sent = _seed(tmp_path, monkeypatch, ROWS)
    assert ras.run_weekly(tmp_path, object(), day=THU) is None                          # 不是评定日
    assert sd.strategies_from_ruleset(tmp_path)[0] == ("a", "b", "c") and not ras.log_path(tmp_path).exists()
    rec = ras.run_weekly(tmp_path, object(), day=FRI)
    assert rec["enabled"] is True and rec["sides"]["long"]["applied"] is True and rec["sides"]["short"]["action"] == "none"
    assert sd.strategies_from_ruleset(tmp_path) == (("a", "b", "d"), ("x", "y", "z"))    # 原位替换, 另一侧不动
    log = ras.read_log(tmp_path)
    assert [s["id"] for s in log["swaps"]] == ["2026-12-04-long"] and log["swaps"][0]["in_name"] == "N-d"
    assert len(sent) == 1 and sent[0][0] == "auto-select:2026-12-04-long" and "N-c → N-d" in sent[0][1]["body"]
    assert ras.run_weekly(tmp_path, object(), day=FRI) == rec and len(sent) == 1         # 同一天重跑不重复换、不重复推
    assert len(ras.read_log(tmp_path)["evals"]) == 1


def test_run_weekly_switched_off_records_proposal_without_touching_strategies(tmp_path, monkeypatch, cal):
    sent = _seed(tmp_path, monkeypatch, ROWS)
    assert ras.enabled(tmp_path) is True                                                # 默认开
    ras.set_enabled(tmp_path, False)
    before = sd.swing_config_path(tmp_path).read_bytes()
    rec = ras.run_weekly(tmp_path, object(), day=FRI)
    assert rec["enabled"] is False and rec["sides"]["long"]["action"] == "swap" and rec["sides"]["long"]["applied"] is False
    assert sd.swing_config_path(tmp_path).read_bytes() == before and ras.read_log(tmp_path)["swaps"] == [] and sent == []


def test_run_weekly_without_fresh_scores_does_nothing(tmp_path, monkeypatch, cal):
    _seed(tmp_path, monkeypatch, ROWS, as_of=THU)                                       # 摘要不是今天的
    rec = ras.run_weekly(tmp_path, object(), day=FRI)
    assert {s["reason"] for s in rec["sides"].values()} == {"no_scores"}
    assert sd.strategies_from_ruleset(tmp_path)[0] == ("a", "b", "c")


def test_undo_restores_the_slot_and_refuses_when_changed(tmp_path, monkeypatch, cal):
    _seed(tmp_path, monkeypatch, ROWS)
    ras.run_weekly(tmp_path, object(), day=FRI)
    with pytest.raises(KeyError):
        ras.undo(tmp_path, "nope")
    s = ras.undo(tmp_path, "2026-12-04-long")
    assert s["undone_at"] and sd.strategies_from_ruleset(tmp_path)[0] == ("a", "b", "c")
    with pytest.raises(ValueError, match="already_undone"):
        ras.undo(tmp_path, "2026-12-04-long")
    log = ras.read_log(tmp_path)
    log["swaps"][0]["undone_at"] = None
    ras._write(ras.log_path(tmp_path), log)
    with pytest.raises(ValueError, match="changed_since"):                              # d 已不在入选里
        ras.undo(tmp_path, "2026-12-04-long")


def test_view_and_api_toggle_and_undo(tmp_path, monkeypatch, cal):
    _seed(tmp_path, monkeypatch, ROWS)
    monkeypatch.setattr(us_clock, "today", lambda: date(2026, 12, 7))
    ras.run_weekly(tmp_path, object(), day=FRI)
    from app.api import opportunities as api
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = type("R", (), {"store": type("S", (), {"data_dir": tmp_path})()})()
    c = TestClient(app)
    v = c.get("/api/opportunities/auto-select").json()
    assert v["enabled"] is True and [x["id"] for x in v["picks"]["long"]] == ["a", "b", "d"] and v["picks"]["long"][2]["verdict"] == "above"
    assert v["next_eval"] == "2026-12-11" and v["last_eval"]["day"] == "2026-12-04" and v["scored"] == 7
    assert [x["id"] for x in v["bench"]] == ["c"] and v["swaps"][0]["id"] == "2026-12-04-long"
    assert c.post("/api/opportunities/auto-select", json={"enabled": False}).json()["enabled"] is False
    assert c.post("/api/opportunities/auto-select", json={"enabled": "yes"}).status_code == 422
    assert c.post("/api/opportunities/auto-select/undo", json={"id": "zzz"}).status_code == 404
    r = c.post("/api/opportunities/auto-select/undo", json={"id": "2026-12-04-long"})
    assert r.status_code == 200 and [x["id"] for x in r.json()["picks"]["long"]] == ["a", "b", "c"] and r.json()["swaps"][0]["undone_at"]
    assert c.post("/api/opportunities/auto-select/undo", json={"id": "2026-12-04-long"}).status_code == 409


def test_view_is_safe_without_any_scores(tmp_path, cal):
    v = ras.view(tmp_path, today=date(2026, 12, 7))
    assert v["scored"] == 0 and v["last_eval"] is None and v["swaps"] == [] and len(v["picks"]["long"]) == 3
    assert {x["verdict"] for x in v["picks"]["long"] + v["picks"]["short"]} == {"thin"}


def test_auto_select_stays_out_of_the_trading_chain():
    root = Path(ras.__file__).parents[1]
    for rel in ("trader/trader.py", "services/trading_loop.py", "services/entry_guard.py", "services/watchlist_book.py",
                "services/opportunity_service.py", "services/opp_radar.py", "services/swing_desk.py", "services/rule_forward.py"):
        assert "rule_auto_select" not in (root / rel).read_text(encoding="utf-8"), rel
    src = Path(ras.__file__).read_text(encoding="utf-8")
    for banned in ("save_day", "ledger", "save_ruleset", "entry_guard", "trading_loop"):
        assert banned not in src, banned
