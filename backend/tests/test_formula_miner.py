"""公式挖掘: 生成 -> 评估查重 -> 候补席 -> 退席; 回测成绩不参与排序; 不碰交易链。"""
import json
import random
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_rank_lab import _days, _write_pool

from app.market_clock import us_clock
from app.services import formula_miner as fm
from app.services import rank_lab as rl
from app.services import rule_forward as rf

DAY = date(2026, 12, 7)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    from app.factors import registry
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    data_dir, _, _ = _write_pool(tmp_path)
    return data_dir


def _forward(data_dir: Path, rows: list[dict]) -> None:
    p = rf.summary_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"contract": rf.CONTRACT, "days": 30, "cells": 100, "as_of": DAY.isoformat(),
                             "scopes": {"all": {"compared": len(rows), "rules": rows}, "gate": {"compared": 0, "rules": []}}}),
                 encoding="utf-8")


def _fw(rule: str, verdict: str, excess: float = 0.0) -> dict:
    return {"rule": rule, "side": "long", "hits": 60, "filled": 40, "days": 25, "excess": excess,
            "ci": [excess - 0.1, excess + 0.1], "verdict": verdict}


# ── 生成 ─────────────────────────────────────────────────────────────

def test_generate_is_reproducible_and_unique():
    a, b = fm.generate("s1", 60), fm.generate("s1", 60)
    assert a == b and len(a) == 60
    assert len({(x["formula"], x["direction"]) for x in a}) == 60
    assert fm.generate("s2", 60) != a
    assert {x["direction"] for x in a} == {"high", "low"}


def test_every_template_compiles_as_a_ranker():
    """模板写出的公式必须过排序因子的全部限制 (基础列 / 预热 / 复权无关), 否则一轮预算全浪费在报错上。"""
    seen = set()
    for seed in range(12):
        for it in fm.generate(f"t{seed}", 80):
            seen.add(it["template"])
            rl.check_formula(it["formula"])
    assert seen >= {"smooth", "std", "ratio", "level", "corr", "cond_share", "cond_count", "spread", "rank_sum", "ts_rank",
                    "rsi", "decay", "zscore"}


def test_cond_count_never_pairs_conditions_that_cannot_hold_on_the_same_day():
    """「A 且 B 占比」: 互斥的两个条件 (上涨/下跌、高开/低开、放量/缩量) 同一天不会同时成立, 公式恒为 0,
    当天前 5 只剩股票代码顺序。固定种子大量生成 (旧写法约五分之一抽到互斥的一对), 每条在面板上取值都不能全为 0。
    面板自造: test_rank_lab 的夹具开盘价 = 收盘价, 高开 / 低开与涨跌分不开, 合法的「上涨日且低开日」在那里也全为 0。"""
    rng = np.random.default_rng(1)
    rows = []
    for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        close = 50.0
        for d in _days(300):
            o = close * float(np.exp(rng.normal(0, 0.01)))
            close = o * float(np.exp(rng.normal(0, 0.02)))
            rows.append({"symbol": s, "date": d, "open": o, "high": max(o, close) * 1.01, "low": min(o, close) * 0.99,
                         "close": close, "volume": float(rng.integers(100_000, 1_000_000)), "amount": close * 1e5})
    panel = rl.prepare_panel(pl.DataFrame(rows))
    made = {}
    for seed in range(10):
        for it in fm.generate(f"cc{seed}", 120):
            if it["template"] == "cond_count":
                made.setdefault(it["formula"], it["name"])
    assert len(made) >= 30
    flat = []
    for f, name in made.items():
        v = rl.factor_frame(panel, f, "high")["value"].drop_nulls()
        if v.len() == 0 or bool((v == 0).all()):
            flat.append(name)
    assert flat == []


def test_mutate_moves_one_window_one_step():
    rng = random.Random(3)
    f = "ts_mean((close/prev_close-1),20)/max(ts_mean(abs((close/prev_close-1)),60),0.0001)"
    for _ in range(20):
        g = fm.mutate(f, rng)
        assert g != f and rl.check_formula(g)
        diff = [(x, y) for x, y in zip(fm._WIN_RE.findall(f), fm._WIN_RE.findall(g), strict=True) if x != y]
        assert len(diff) == 1
        i, j = fm.WINDOWS.index(int(diff[0][0])), fm.WINDOWS.index(int(diff[0][1]))
        assert abs(i - j) == 1
    assert fm.mutate("close/open-1", rng) is None


def test_generate_mutates_parents_and_keeps_their_direction():
    parents = [{"id": "rank_aaaaaaaaaa", "name": "母", "formula": "ts_mean((close/open-1),20)", "direction": "low"}]
    kids = [x for x in fm.generate("p", 80, parents) if x["template"] == "mutate"]
    assert kids and all(k["parent"] == "rank_aaaaaaaaaa" and k["direction"] == "low" for k in kids)
    assert {k["formula"] for k in kids} == {"ts_mean((close/open-1),14)", "ts_mean((close/open-1),40)"}


def test_overlap():
    a = {1: {"A", "B"}, 2: {"C"}, 3: {"D"}}
    assert fm.overlap(a, a) == 1.0
    assert fm.overlap(a, {1: {"A", "X"}, 2: {"Y"}}) == 0.25
    assert fm.overlap(a, {9: {"A"}}) == 0.0


# ── 评估 / 查重 / 选席 ───────────────────────────────────────────────

def test_evaluate_reports_mining_segment_only(lab):
    pool = rl.load_pool(lab)
    cut = rl._segments(pool)["mining"][1]
    rec, sets = fm.evaluate(pool, {"formula": "(close/ts_delay(close,5)-1)", "direction": "high", "name": "五日涨幅 高"})
    full = rl.evaluate_formula(pool, "(close/ts_delay(close,5)-1)", "high")
    assert rec["id"] == rl.ranker_id("(close/ts_delay(close,5)-1)", "high")
    assert rec["mining"] == {k: full["mining"][k] for k in ("n", "mean_r", "excess", "excess_ci")}   # 与导入后的复核逐位相同
    assert "holdout" not in rec and "full" not in rec
    assert sets and max(sets) < cut and all(len(s) <= rl.PER_SIDE for s in sets.values())
    bad, none = fm.evaluate(pool, {"formula": "rsi_14 + 1", "direction": "high", "name": "x"})
    assert bad["error"] == "R003" and none is None


def test_search_admits_by_novelty_not_by_backtest(lab):
    res = fm.search(lab, slots=2, seed="run-a", budget=120)
    f = res["funnel"]
    assert f["generated"] == 120 and f["new"] == f["evaluated"] == 120 and f["admitted"] == len(res["chosen"]) == 2
    assert f["errors"] + f["thin"] + f["negative"] + f["same_as_alive"] + f["same_as_retired"] + f["eligible"] == f["evaluated"]
    assert f["negative"] > 0
    trials = fm.read_trials(lab)
    assert len(trials) == 120 and {t["run"] for t in trials} == {"run-a"}
    elig = [t for t in trials if t["outcome"] in ("eligible", "admitted")]
    # 选中的两条里没有“回测超额最高”这个条件: 至少存在一条没入席、超额却更高的
    worst_admitted = min(c["mining"]["excess"] for c in res["chosen"])
    assert any(t["outcome"] == "eligible" and t["mining"]["excess"] > worst_admitted for t in elig)
    # 回放超额为负的不入席, 也不算可用
    assert all(c["mining"]["excess"] >= 0 for c in res["chosen"])
    assert all(t["mining"]["excess"] >= 0 for t in elig)
    assert all(t["mining"]["excess"] < 0 for t in trials if t["outcome"] == "negative")
    # 再跑同一个种子: 全部试过, 不再评估
    again = fm.search(lab, slots=2, seed="run-a", budget=120)
    assert again["funnel"]["new"] == 0 and again["chosen"] == [] and len(fm.read_trials(lab)) == 120


def test_search_rejects_what_alive_rankers_already_pick(lab):
    first = fm.search(lab, slots=1, seed="run-b", budget=30)["chosen"][0]
    rl.import_ranker(lab, name=first["name"], formula=first["formula"], direction=first["direction"])
    twin = {"formula": first["formula"].replace(" ", ""), "direction": first["direction"], "name": "同一条"}
    pool = rl.load_pool(lab)
    _rec, sets = fm.evaluate(pool, twin)
    alive = fm._pick_sets(rl.picks(pool, rl.value_matrix(pool, rl.factor_frame(pool.panel, first["formula"], first["direction"])))[1],
                          rl._segments(pool)["mining"][1])
    assert fm.overlap(sets, alive) == 1.0
    res = fm.search(lab, slots=3, seed="run-c", budget=40)
    assert all(c["overlap"]["alive"] < fm.OVERLAP_ALIVE_MAX for c in res["chosen"])
    assert first["id"] not in {t["id"] for t in fm.read_trials(lab) if t["run"] == "run-c"}   # 已登记的不再试


def test_search_without_pool(tmp_path):
    assert fm.search(tmp_path, slots=2, seed="x") == {"skipped": "no_pool"}


# ── 一轮: 退席 -> 生成 -> 入席 ───────────────────────────────────────

def _stub(lab):
    return lambda data_dir, cfg: fm.search(data_dir, slots=cfg["slots"], seed=cfg["seed"], budget=120)


def test_run_admits_challengers_as_rankers(lab, cal, monkeypatch):
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    rec = fm.run(lab, object(), today=DAY, searcher=_stub(lab))
    assert rec["run_id"] == f"fm-{DAY.isoformat()}-1" and len(rec["admitted"]) == fm.ADMIT_PER_RUN
    rankers = rl.read_rankers(lab)
    for a in rec["admitted"]:
        rk = rankers[a["id"]]
        assert rk["source"]["system"] == fm.SYSTEM and rk["source"]["status"] == "challenger"
        assert rk["parity"]["checked"] and rk["parity"]["match"]          # 入席前后两次评估逐位相同
    v = fm.view(lab, today=DAY)
    assert [x["id"] for x in v["bench"]] and len(v["bench"]) == 2 and v["selected"] == [] and v["retired"] == []
    assert v["totals"]["tried"] == 120 and v["totals"]["admitted"] == 2 and v["bench"][0]["forward"]["verdict"] == "thin"
    assert v["bench"][0]["replay"]["mining"]["n"] >= fm.MIN_MINING_N and v["last_run"]["run_id"] == rec["run_id"]


def test_run_respects_switch_and_bench_cap(lab, cal, monkeypatch):
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    fm.set_enabled(lab, False)
    assert fm.run(lab, object(), today=DAY, searcher=_stub(lab)) == {"skipped": "disabled"}
    assert fm.read_trials(lab) == [] and rl.read_rankers(lab) == {}
    assert len(fm.run(lab, object(), today=DAY, force=True, searcher=_stub(lab))["admitted"]) == 2      # 手动跑不看开关
    fm.set_enabled(lab, True)
    monkeypatch.setattr(fm, "BENCH_CAP", 3)
    assert len(fm.run(lab, object(), today=DAY, searcher=_stub(lab))["admitted"]) == 1                   # 只剩 1 个空席
    called = []
    full = fm.run(lab, object(), today=DAY, searcher=lambda d, c: called.append(c) or {})
    assert full["skipped"] == "bench_full" and called == []                                              # 满员不生成
    assert len(rl.read_rankers(lab)) == 3


def test_retire_on_forward_verdict_timeout_and_keep_selected(lab, cal, monkeypatch):
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    monkeypatch.setattr(fm, "BENCH_CAP", 4)
    monkeypatch.setattr(fm, "ADMIT_PER_RUN", 4)
    ids = [a["id"] for a in fm.run(lab, object(), today=date(2026, 9, 1), searcher=_stub(lab))["admitted"]]
    assert len(ids) == 4
    below, sel, slow, good = ids
    _forward(lab, [_fw(below, "below", -0.3), _fw(sel, "unclear"), _fw(slow, "unclear"), _fw(good, "above", 0.4)])
    monkeypatch.setattr(fm, "_long_ids", lambda d: [sel])
    # 坐了 10 个交易日: 只有垫底的退
    out = fm.retire(lab, today=date(2026, 9, 15))
    assert [x["id"] for x in out] == [below] and out[0]["reason"] == "below"
    assert below not in rl.read_rankers(lab) and below in rl.read_rankers(lab, include_removed=True)
    # 坐满期限: 没过线的退, 过线的留, 入选中的不动
    out = fm.retire(lab, today=DAY)
    assert [(x["id"], x["reason"]) for x in out] == [(slow, "timeout")]
    v = fm.view(lab, today=DAY)
    assert [x["id"] for x in v["selected"]] == [sel] and [x["id"] for x in v["bench"]] == [good]
    assert {x["id"]: x["reason"] for x in v["retired"]} == {below: "below", slow: "timeout"}
    assert next(x for x in v["retired"] if x["id"] == below)["forward"]["excess"] == -0.3            # 退席时的成绩留档
    assert v["totals"]["above"] == 1
    # 人工移除的记 removed; 被换出入选的回到候补
    monkeypatch.setattr(fm, "_long_ids", lambda d: [])
    rl.remove_ranker(lab, good, enabled_long=[])
    assert [(x["id"], x["reason"]) for x in fm.retire(lab, today=date(2026, 9, 2))] == [(good, "removed")]
    assert fm.read_state(lab)["members"][sel]["status"] == "bench"


def test_formula_taken_over_by_external_system_is_released_not_retired(lab, cal, monkeypatch):
    """同一条公式之后被外部研究系统导入 (登记出处被改写): 让出席位, 之后垫底 / 到期都不由这里移除。"""
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    a, b = (x["id"] for x in fm.run(lab, object(), today=date(2026, 9, 1), searcher=_stub(lab))["admitted"])
    rk = rl.read_rankers(lab)[a]
    rl.import_ranker(lab, name="外部导入", formula=rk["formula"], direction=rk["direction"], source={"system": "external-miner"})
    _forward(lab, [_fw(a, "below", -0.3), _fw(b, "unclear")])
    assert fm.retire(lab, today=DAY) == [{"id": b, "name": fm.read_state(lab)["members"][b]["name"], "reason": "timeout"}]
    m = fm.read_state(lab)["members"][a]
    assert m["status"] == "released" and m["released_to"] == "external-miner" and a in rl.read_rankers(lab)
    assert fm.bench_count(fm.read_state(lab)) == 0
    v = fm.view(lab, today=DAY)
    assert a not in {x["id"] for x in v["bench"] + v["selected"] + v["retired"]} and v["totals"]["released"] == 1
    assert fm.retire(lab, today=DAY) == []                                    # 之后不再碰它


def test_admit_skips_a_formula_another_system_imported_during_the_search(lab, cal, monkeypatch):
    """搜索在子进程里跑 (最长 15 分钟), 这期间外部研究系统导入了同一条公式: 入席时不改写它的登记出处、不记成候补
    (否则之后垫底 / 到期会把这条外部导入移除), 本轮记录列进 taken。判断与登记在 rank_lab 的同一把锁里。"""
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    seen = {}

    def searcher(data_dir, cfg):
        res = fm.search(data_dir, slots=cfg["slots"], seed=cfg["seed"], budget=120)
        c = seen["c"] = res["chosen"][0]
        rl.import_ranker(data_dir, name="外部导入", formula=c["formula"], direction=c["direction"],
                         source={"system": "external-miner"})
        return res
    rec = fm.run(lab, object(), today=DAY, searcher=searcher)
    taken = seen["c"]["id"]
    assert rl.read_rankers(lab)[taken]["source"]["system"] == "external-miner"
    assert taken not in fm.read_state(lab)["members"]
    assert len(rec["admitted"]) == fm.ADMIT_PER_RUN - 1 and taken not in {a["id"] for a in rec["admitted"]}
    assert rec["taken"] == [{"id": taken, "name": seen["c"]["name"], "system": "external-miner"}]
    _forward(lab, [_fw(taken, "below", -0.3)])
    assert fm.retire(lab, today=DAY) == [] and taken in rl.read_rankers(lab)


def test_retired_formula_is_never_tried_again(lab, cal, monkeypatch):
    monkeypatch.setattr(rl, "backfill_latest", lambda d, r: {"added": []})
    rid = fm.run(lab, object(), today=date(2026, 9, 1), searcher=_stub(lab))["admitted"][0]["id"]
    _forward(lab, [_fw(rid, "below", -0.2)])
    gone = rl.read_rankers(lab, include_removed=True)[rid]
    again = [{"formula": gone["formula"], "direction": gone["direction"], "name": "又来", "template": "level"}]
    monkeypatch.setattr(fm, "generate", lambda seed, n, parents=None: again)
    rec = fm.run(lab, object(), today=date(2026, 9, 15), searcher=_stub(lab))
    assert rec["retired"][0]["id"] == rid and rec["funnel"]["generated"] == 1 and rec["funnel"]["new"] == 0
    assert rec["admitted"] == [] and rid not in rl.read_rankers(lab)


# ── 接口与边界 ───────────────────────────────────────────────────────

def test_api_view_switch_and_run_guard(lab, cal, monkeypatch):
    from app.api import factor_lab as api
    from app.services import swing_candidates as sc
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = type("R", (), {"store": type("S", (), {"data_dir": lab})()})()
    c = TestClient(app)
    v = c.get("/api/factor-lab/miner").json()
    assert v["enabled"] is True and v["pool_ready"] is True and v["bench"] == [] and v["caps"]["bench"] == fm.BENCH_CAP
    assert c.post("/api/factor-lab/miner", json={"enabled": False}).json()["enabled"] is False
    assert c.post("/api/factor-lab/miner", json={"enabled": "no"}).status_code == 422
    monkeypatch.setattr(sc, "can_run_now", lambda now=None: False)
    r = c.post("/api/factor-lab/miner/run")
    assert r.status_code == 409 and "立即挖掘失败" in r.json()["detail"]


def test_worker_kind_is_wired_both_ways():
    from app.backtest import worker
    task = worker.make_worker_task("formula_miner", Path("/tmp/x"), {"slots": 1, "seed": "s"})
    assert task["kind"] == "formula_miner"
    with pytest.raises(TypeError):
        worker.make_worker_task("formula_miner", Path("/tmp/x"), "bad")
    src = Path(worker.__file__).read_text(encoding="utf-8")
    assert src.count('kind == "formula_miner"') == 2


def test_miner_stays_out_of_the_trading_chain():
    root = Path(fm.__file__).parents[1]
    for rel in ("trader/trader.py", "services/trading_loop.py", "services/entry_guard.py", "services/watchlist_book.py",
                "services/opportunity_service.py", "services/swing_desk.py", "services/rule_forward.py",
                "services/rule_auto_select.py"):
        assert "formula_miner" not in (root / rel).read_text(encoding="utf-8"), rel
    src = Path(fm.__file__).read_text(encoding="utf-8")
    for banned in ("save_day", "ledger", "save_ruleset", "save_strategies", "entry_guard", "trading_loop"):
        assert banned not in src, banned
