"""2026-11-18 清查复现: 公式挖掘自动起的名字要读得通, 且与公式实际窗口一致 (名字会上屏、进推送)。"""
import re

from app.services import formula_miner as fm


def _many(parents=None):
    out = []
    for i in range(40):
        out += fm.generate(f"seed-{i}", 60, parents)
    return out


def test_generated_names_do_not_stutter():
    names = {x["name"] for x in _many()}
    bad = sorted(n for n in names if "日日" in n or "波动波动" in n)
    assert not bad, bad[:5]


def test_mutated_name_states_the_window_it_actually_uses():
    parent = {"id": "rank_0000000001", "name": "距 20 日低点 低", "formula": "(close/ts_min(low,20)-1)", "direction": "low"}
    kids = [x for x in _many([parent]) if x.get("template") == "mutate"]
    assert kids
    for k in kids:
        new = re.search(r",(\d+)\)", k["formula"]).group(1)
        assert new != "20"
        assert k["name"] == f"距 20 日低点 低 · 窗口 20→{new}", k["name"]


def test_second_generation_mutation_keeps_one_window_note():
    parent = {"id": "rank_0000000002", "name": "距 20 日低点 低 · 窗口 20→10", "formula": "(close/ts_min(low,10)-1)", "direction": "low"}
    kids = [x for x in _many([parent]) if x.get("template") == "mutate"]
    assert kids and all(k["name"].count("窗口") == 1 and k["name"].startswith("距 20 日低点 低 · 窗口 10→") for k in kids)
