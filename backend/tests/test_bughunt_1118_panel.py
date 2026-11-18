"""2026-11-18 清查复现: 策略页面板看不到入选的排序因子 (策略页写入选 5, 机会页入选 6)。"""
import json
from datetime import date

import pytest

from app.services import rank_lab as rl
from app.services import rule_forward as rf
from app.services import strategy_panel as sp
from app.services import swing_desk as sd
from app.services import swing_settlement as ss

RID = "rank_59ae3b44ef"
OTHER = "rank_6355620756"
IDS = ["ibs_mean_reversion", "tv_halftrend"]
FULL = {"n": 1196, "mean_r": 0.3076, "excess": 0.071, "excess_ci": [-0.0096, 0.1535], "p": 0.043975}


def _signal_file(data_dir, day):
    d = data_dir / "opportunities" / "watchlist"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.json").write_text(json.dumps({"as_of": day, "watchlist": ["CBRS", "META"], "signals": []}), encoding="utf-8")


def _cells(data_dir, day, cards):
    p = rf.cells_path(data_dir, date.fromisoformat(day))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"contract": rf.CONTRACT, "day": day, "cards": cards}), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "rule_history", lambda: {"window": ["2023-01-03", "2026-10-30"], "rules": {
        "ibs_mean_reversion": {"n": 1323, "fill": 0.613, "mean_r": 0.3094, "excess": 0.0607, "p": 0.035}}})
    monkeypatch.setattr(sd, "strategies_from_ruleset", lambda _d: (("ibs_mean_reversion", RID), ("tv_halftrend",)))
    monkeypatch.setattr(sd, "build_live", lambda *_a, **_k: {
        "long": [{"symbol": "META", "state": "pending", "verdict": "enter", "signal_day": "2026-11-17", "r_now": None,
                  "card_id": "c1", "strategies": [{"id": RID, "name": "x"}]}], "short": []})
    lab = rl.lab_dir(tmp_path)
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "rankers.json").write_text(json.dumps({"contract": rl.CONTRACT, "rankers": {
        RID: {"id": RID, "name": "波动扩张", "formula": "ts_mean(high/low-1,5)", "direction": "high", "removed_at": None},
        OTHER: {"id": OTHER, "name": "六十日动量", "formula": "ts_delta(close,60)", "direction": "high", "removed_at": None}}}),
        encoding="utf-8")
    (lab / "results.json").write_text(json.dumps({"contract": rl.CONTRACT, "rankers": {RID: {"full": FULL, "error": None}}}),
                                      encoding="utf-8")
    _signal_file(tmp_path, "2026-11-16")
    _signal_file(tmp_path, "2026-11-17")
    _cells(tmp_path, "2026-11-17", [
        {"card_id": "cell-2026-11-17-CBRS-long", "symbol": "CBRS", "side": "long", "rules": [RID, OTHER]},
        {"card_id": "cell-2026-11-17-META-long", "symbol": "META", "side": "long", "rules": [RID]},
        {"card_id": "cell-2026-11-17-META-short", "symbol": "META", "side": "short", "rules": []}])
    p = ss.outcomes_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"card_id": "c-APP", "symbol": "APP", "side": "long", "signal_day": "2026-11-13", "rules": [RID],
                             "status": "closed", "r": 0.4, "fill_date": "2026-11-13", "exit_date": "2026-11-16",
                             "settled_on": "2026-11-16", "why": "time"}) + "\n", encoding="utf-8")
    sp._SIG_CACHE.clear()
    return tmp_path


def test_selected_ranker_gets_a_read_only_entry_with_its_own_numbers(env):
    out = sp.build(env, strategy_ids=IDS)
    assert out["errors"] == {}
    assert set(out["strategies"]) == {*IDS, RID}                      # 没入选的排序因子不列
    e = out["strategies"][RID]
    assert e["ranker"] == {"name": "排序 · 波动扩张", "formula": "ts_mean(high/low-1,5)", "direction": "high"}
    assert e["short_capable"] is False and set(e["sides"]) == {"long"}
    v = e["sides"]["long"]
    assert v["selected"] is True
    assert v["hits"] == ["CBRS", "META"] and v["hist"] == [0, 2]      # 命中读规则前向成绩当天的格子, 信号表里没有排序因子的行
    assert v["replay"] == {"n": 1196, "mean_r": 0.3076, "excess": 0.071, "p": 0.043975}
    assert [b["symbol"] for b in v["board"]] == ["META"]
    assert v["paper"]["fills"] == 1 and v["paper"]["trades"][0]["symbol"] == "APP"
    assert "ranker" not in out["strategies"]["ibs_mean_reversion"]


def test_ranker_without_stored_replay_still_listed(env):
    (rl.lab_dir(env) / "results.json").unlink()
    v = sp.build(env, strategy_ids=IDS)["strategies"][RID]["sides"]["long"]
    assert v["replay"] is None and v["selected"] is True
