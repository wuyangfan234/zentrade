"""2026-11-18 清查复现: 自动选策略撤回 x 公式挖掘退席的交叉路径。"""
import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import rank_lab as rl
from app.services import rule_auto_select as ras
from app.services import rule_forward as rf
from app.services import swing_desk as sd

FRI = date(2026, 12, 4)
RID = "rank_aaaaaaaaaa"


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


def _r(rule, side, excess, verdict):
    return {"rule": rule, "side": side, "hits": 60, "filled": 40, "days": 25, "fill": 0.6, "mean_r": 0.1, "win": 0.5,
            "excess": excess, "ci": [excess - 0.1, excess + 0.1], "verdict": verdict}


def _seed(tmp_path, monkeypatch):
    """入选做多 = a / b / 一条公式挖掘的排序因子 (前向落后); 候补 d 领先 -> 评定日把排序因子换出。"""
    sd.save_strategies(tmp_path, ["a", "b", RID], ["x", "y", "z"])
    p = rl.lab_dir(tmp_path) / "rankers.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"contract": rl.CONTRACT, "rankers": {RID: {
        "id": RID, "name": "测试公式", "formula": "(close/prev_close-1)", "direction": "low",
        "source": {"system": "zentrade-miner"}, "removed_at": None}}}))
    rows = [_r("a", "long", 0.20, "above"), _r("b", "long", 0.02, "unclear"), _r(RID, "long", -0.15, "below"),
            _r("d", "long", 0.30, "above")]
    s = rf.summary_path(tmp_path)
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(json.dumps({"contract": rf.CONTRACT, "days": 30, "cells": 2000, "window": ["2026-10-15", "2026-12-03"],
                             "as_of": FRI.isoformat(),
                             "scopes": {"all": {"compared": 4, "rules": rows}, "gate": {"compared": 0, "rules": []}}}))
    monkeypatch.setattr(ras, "eligible", lambda data_dir, engine: {"long": {"a", "b", "d", RID}, "short": {"x", "y", "z"}})
    from app.services import notify_opportunity as no
    monkeypatch.setattr(no, "enqueue_global", lambda *a, **k: True)
    rec = ras.run_weekly(tmp_path, object(), day=FRI)
    assert rec["sides"]["long"]["out"] == RID and rec["sides"]["long"]["applied"] is True
    assert sd.strategies_from_ruleset(tmp_path)[0] == ("a", "b", "d")


def test_undo_refuses_to_bring_back_a_ranker_that_was_retired_meanwhile(tmp_path, monkeypatch, cal):
    """换出的排序因子当晚被公式挖掘退席 (登记标记移除) 之后再撤回: 不能把一条已不存在的排序因子写回入选策略。
    写回去的后果: 信号表不再给它算值, 入选做多 3 条里有 1 条永远 0 命中。"""
    _seed(tmp_path, monkeypatch)
    rl.remove_ranker(tmp_path, RID, enabled_long=list(sd.strategies_from_ruleset(tmp_path)[0]))
    assert RID not in rl.read_rankers(tmp_path)
    before = sd.swing_config_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="out_removed"):
        ras.undo(tmp_path, f"{FRI.isoformat()}-long")
    assert sd.swing_config_path(tmp_path).read_bytes() == before
    assert ras.read_log(tmp_path)["swaps"][0]["undone_at"] is None


def test_undo_still_restores_a_ranker_that_is_alive(tmp_path, monkeypatch, cal):
    _seed(tmp_path, monkeypatch)
    s = ras.undo(tmp_path, f"{FRI.isoformat()}-long")
    assert s["undone_at"] and sd.strategies_from_ruleset(tmp_path)[0] == ("a", "b", RID)


def test_undo_api_says_why_when_the_ranker_is_gone(tmp_path, monkeypatch, cal):
    from app.api import opportunities as opp
    _seed(tmp_path, monkeypatch)
    rl.remove_ranker(tmp_path, RID, enabled_long=list(sd.strategies_from_ruleset(tmp_path)[0]))
    app = FastAPI()
    app.include_router(opp.router)

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()
    app.state.repo = _Repo()
    prefix = opp.router.prefix
    r = TestClient(app).post(f"{prefix}/auto-select/undo", json={"id": f"{FRI.isoformat()}-long"})
    assert r.status_code == 409 and "已退席" in r.json()["detail"]
