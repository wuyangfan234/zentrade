"""策略页面板 (只读聚合) 与服务器端策略池。

守的口径:
- 一个策略一行, 多空两面各有各的回放 / 命中 / 机会板 / 模拟成绩;
- 模拟成绩按卡上的 rules 归因, 多规则卡各计一次;
- 区间与背离沿用机会页页头的算法 (出卡日不足 8 个不给区间, 也不判背离);
- 某一段读失败只空那一段;
- 面板不写任何文件;
- 策略池: 从未保存 (None) 与清空 ([]) 要分得开, 固定路径不被 /{strategy_id} 吃掉。
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.api import strategy as strategy_api
from app.services import preferences
from app.services import strategy_panel as sp
from app.services import swing_desk as sd
from app.services import swing_settlement as ss

IDS = ["ibs_mean_reversion", "tv_halftrend", "boll_breakout"]
HIST = {
    "window": ["2023-01-03", "2026-10-30"], "universe": "watchlist 46 (2026-11-09)", "data_end": "2026-11-09",
    "generated": "2026-11-10T17:08:35+08:00",
    "rules": {
        "ibs_mean_reversion": {"n": 1323, "fill": 0.613, "mean_r": 0.3094, "excess": 0.0607, "p": 0.035},
        "tv_halftrend": {"n": 539, "fill": 0.624, "mean_r": 0.0403, "excess": -0.1034, "p": 0.972},
        "tv_halftrend:short": {"n": 164, "fill": 0.648, "mean_r": 0.2482, "excess": 0.2494, "p": 0.0},
        "boll_breakout": {"n": 2, "signals": 3, "raw": 201, "thin": True},
    },
}


def _signal_file(data_dir, day: str, signals: list[tuple[str, str, str]], watchlist: int = 46):
    d = data_dir / "opportunities" / "watchlist"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.json").write_text(json.dumps({
        "as_of": day, "watchlist": [f"S{i}" for i in range(watchlist)],
        "signals": [{"rule": r, "symbol": s, "side": side} for r, s, side in signals],
    }), encoding="utf-8")


def _outcomes(data_dir, rows: list[dict]):
    p = ss.outcomes_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _closed(sym, side, day, rules, r, exit_date, why="stop"):
    return {"card_id": f"c-{sym}-{day}-{side}", "symbol": sym, "side": side, "signal_day": day, "rules": rules,
            "status": "closed", "r": r, "fill_date": day, "exit_date": exit_date, "settled_on": exit_date, "why": why}


def _no_fill(sym, side, day, rules):
    return {"card_id": f"n-{sym}-{day}-{side}", "symbol": sym, "side": side, "signal_day": day, "rules": rules,
            "status": "no_fill", "settled_on": day}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "rule_history", lambda: HIST)
    monkeypatch.setattr(sd, "strategies_from_ruleset", lambda _d: (("ibs_mean_reversion",), ("tv_halftrend",)))
    monkeypatch.setattr(sd, "build_live", lambda *_a, **_k: {
        "long": [{"symbol": "AAPL", "state": "holding", "verdict": "holding", "signal_day": "2026-11-05",
                  "r_now": 1.04, "card_id": "c1", "strategies": [{"id": "ibs_mean_reversion", "name": "x"}]},
                 {"symbol": "AMZN", "state": "pending", "verdict": "skip", "signal_day": "2026-11-09",
                  "r_now": None, "card_id": "c2", "strategies": [{"id": "ibs_mean_reversion", "name": "x"}]}],
        "short": [{"symbol": "AXTI", "state": "in_zone", "blocked": None, "signal_day": date(2026, 11, 5),
                   "r_now": 0.12, "card_id": "c3", "strategies": [{"id": "tv_halftrend", "name": "y"}]}],
    })
    sp._SIG_CACHE.clear()
    return tmp_path


def test_one_row_per_strategy_with_both_sides(env):
    _signal_file(env, "2026-11-09", [("ibs_mean_reversion", "MRNA", "long")])
    _signal_file(env, "2026-11-10", [("ibs_mean_reversion", "MRNA", "long"), ("ibs_mean_reversion", "HIMS", "long"),
                                     ("tv_halftrend", "TSLA", "long"), ("tv_halftrend", "MU", "short"),
                                     ("phantom_4h", "NVDA", "long")])
    out = sp.build(env, strategy_ids=IDS)

    assert out["contract"] == "strategy-panel-v1" and out["errors"] == {}
    assert set(out["strategies"]) == set(IDS)                       # 一个策略一行, 不按方向拆成两行
    assert out["selected"] == {"long": ["ibs_mean_reversion"], "short": ["tv_halftrend"]}
    assert out["signals"] == {"day": "2026-11-10", "requested": None, "days": ["2026-11-09", "2026-11-10"],
                              "universe": 46}

    ibs = out["strategies"]["ibs_mean_reversion"]
    assert ibs["short_capable"] is False and set(ibs["sides"]) == {"long"}
    assert ibs["sides"]["long"]["selected"] is True
    assert ibs["sides"]["long"]["hits"] == ["HIMS", "MRNA"] and ibs["sides"]["long"]["hist"] == [1, 2]
    assert ibs["sides"]["long"]["replay"]["excess"] == 0.0607
    # 机会板现状: 状态原样给出, 不下单单列
    assert [(b["symbol"], b["state"], b["skip"]) for b in ibs["sides"]["long"]["board"]] == [
        ("AAPL", "holding", False), ("AMZN", "pending", True)]

    ht = out["strategies"]["tv_halftrend"]
    assert ht["short_capable"] is True and ht["name_short"] == sd.strategy_name("tv_halftrend", "short")
    assert ht["sides"]["long"]["selected"] is False and ht["sides"]["short"]["selected"] is True
    assert ht["sides"]["long"]["hits"] == ["TSLA"] and ht["sides"]["short"]["hits"] == ["MU"]
    assert ht["sides"]["long"]["replay"]["mean_r"] == 0.0403 and ht["sides"]["short"]["replay"]["mean_r"] == 0.2482
    assert ht["sides"]["short"]["board"][0]["signal_day"] == "2026-11-05"     # date 对象也给成字符串

    # 样本少的回放行原样带出; 没有信号的策略命中为空而不是缺键
    boll = out["strategies"]["boll_breakout"]["sides"]["long"]
    assert boll["replay"]["thin"] is True and boll["hits"] == [] and boll["hist"] == [0, 0] and boll["paper"] is None


def test_paper_attribution_counts_multi_rule_cards_once_per_rule(env):
    _outcomes(env, [
        _closed("AAPL", "long", "2026-10-20", ["ibs_mean_reversion", "tv_halftrend"], 2.0, "2026-10-23", "target"),
        _closed("MSFT", "long", "2026-10-21", ["ibs_mean_reversion"], -1.0, "2026-10-22"),
        _no_fill("NVDA", "long", "2026-10-22", ["ibs_mean_reversion"]),
        _closed("MU", "short", "2026-10-22", ["tv_halftrend"], 0.5, "2026-10-27", "time"),
        {"card_id": "x", "symbol": "ZZ", "side": "long", "signal_day": "2026-10-23", "rules": ["ibs_mean_reversion"],
         "status": "open"},                                           # 未结算的不计
    ])
    out = sp.build(env, strategy_ids=IDS)
    assert out["paper_since"] == "2026-10-20"

    ibs = out["strategies"]["ibs_mean_reversion"]["sides"]["long"]["paper"]
    assert (ibs["cards"], ibs["fills"], ibs["win"], ibs["loss"]) == (3, 2, 1, 1)
    assert ibs["fill_rate"] == 0.667 and ibs["sum_r"] == 1.0 and ibs["mean_r"] == 0.5
    assert ibs["replay_mean_r"] == 0.3094 and ibs["replay_fill"] == 0.613
    assert ibs["ci"] is None and ibs["diverged"] is False             # 出卡日不足 8 个: 不给区间, 不判背离
    assert [(t["symbol"], t["exit_date"], t["why"], t["r"]) for t in ibs["trades"]] == [
        ("MSFT", "2026-10-22", "stop", -1.0), ("AAPL", "2026-10-23", "target", 2.0)]   # 按平仓日升序

    # 同一张 AAPL 卡也记到 HalfTrend 做多名下; 做空那面只有 MU
    ht = out["strategies"]["tv_halftrend"]["sides"]
    assert (ht["long"]["paper"]["cards"], ht["long"]["paper"]["sum_r"]) == (1, 2.0)
    assert (ht["short"]["paper"]["cards"], ht["short"]["paper"]["mean_r"]) == (1, 0.5)
    assert ht["short"]["paper"]["replay_mean_r"] == 0.2482
    per_rule_cards = ibs["cards"] + ht["long"]["paper"]["cards"] + ht["short"]["paper"]["cards"]
    assert per_rule_cards == 5                                        # 4 张已结算的卡, 归因口径下是 5


def test_ci_and_divergence_follow_opp_page_algorithm(env):
    days = [f"2026-10-{d:02d}" for d in range(5, 15)]                 # 10 个出卡日, 每日一笔 -1R
    _outcomes(env, [_closed(f"S{i}", "long", d, ["ibs_mean_reversion"], -1.0, d) for i, d in enumerate(days)])
    p = sp.build(env, strategy_ids=IDS)["strategies"]["ibs_mean_reversion"]["sides"]["long"]["paper"]
    ref = ss.live_vs_replay({d: [-1.0] for d in days}, 0.3094)
    assert p["days"] == 10 and p["ci"] == ref["ci"] == [-1.0, -1.0]
    assert p["diverged"] is True and ref["diverged"] is True          # 区间不含回放值


def test_requested_day_without_signal_file_gives_empty_hits_not_latest(env):
    _signal_file(env, "2026-11-06", [("ibs_mean_reversion", "MRNA", "long")])
    _signal_file(env, "2026-11-10", [("ibs_mean_reversion", "HIMS", "long")])
    exact = sp.build(env, strategy_ids=IDS, day=date(2026, 11, 6))
    assert exact["signals"]["day"] == "2026-11-06" and exact["signals"]["days"] == ["2026-11-06"]
    assert exact["strategies"]["ibs_mean_reversion"]["sides"]["long"]["hits"] == ["MRNA"]
    missing = sp.build(env, strategy_ids=IDS, day=date(2026, 11, 9))
    assert missing["signals"]["day"] is None and missing["signals"]["requested"] == "2026-11-09"
    side = missing["strategies"]["ibs_mean_reversion"]["sides"]["long"]
    assert side["hits"] == [] and side["hist"] == [1]                 # 不拿别的日子冒充, 迷你柱只到所选日


def test_history_window_is_capped_and_cache_follows_file_changes(env):
    for d in range(1, 26):
        _signal_file(env, f"2026-09-{d:02d}", [("ibs_mean_reversion", "MRNA", "long")] * 1)
    out = sp.build(env, strategy_ids=IDS)
    assert len(out["signals"]["days"]) == sp.HIST_DAYS and out["signals"]["days"][-1] == "2026-09-25"
    assert out["strategies"]["ibs_mean_reversion"]["sides"]["long"]["hist"] == [1] * sp.HIST_DAYS
    _signal_file(env, "2026-09-25", [("ibs_mean_reversion", "MRNA", "long"), ("ibs_mean_reversion", "AMD", "long"),
                                     ("ibs_mean_reversion", "ARM", "long")])
    again = sp.build(env, strategy_ids=IDS)
    assert again["strategies"]["ibs_mean_reversion"]["sides"]["long"]["hits"] == ["AMD", "ARM", "MRNA"]


def test_failed_section_only_blanks_itself(env, monkeypatch):
    _signal_file(env, "2026-11-10", [("ibs_mean_reversion", "MRNA", "long")])
    _outcomes(env, [_closed("AAPL", "long", "2026-10-20", ["ibs_mean_reversion"], 2.0, "2026-10-23")])

    def boom(*_a, **_k):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(sd, "build_live", boom)
    out = sp.build(env, strategy_ids=IDS)
    assert out["errors"] == {"board": "RuntimeError"}
    side = out["strategies"]["ibs_mean_reversion"]["sides"]["long"]
    assert side["board"] == [] and side["hits"] == ["MRNA"] and side["paper"]["fills"] == 1


def test_panel_writes_nothing(env):
    _signal_file(env, "2026-11-10", [("ibs_mean_reversion", "MRNA", "long")])
    _outcomes(env, [_closed("AAPL", "long", "2026-10-20", ["ibs_mean_reversion"], 2.0, "2026-10-23")])
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in env.rglob("*") if p.is_file()}
    sp.build(env, strategy_ids=IDS, day=date(2026, 11, 10))
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in env.rglob("*") if p.is_file()}
    assert before == after


# ── 策略池 ────────────────────────────────────────────────────

class _Engine:
    def __init__(self, ids):
        self._ids = ids

    def list_strategies(self, include_research=False):
        return [{"id": i, "research_only": i.startswith("draft_")} for i in self._ids]


def _request(tmp_path, ids=("a", "b", "draft_x", "high_turnover_surge")):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=_Engine(list(ids)))))


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


def test_pool_unsaved_suggests_last_scanned_public_strategies_without_saving(prefs, monkeypatch):
    monkeypatch.setattr("app.services.strategy_cache.read_cache",
                        lambda _d: {"results": {"b": {}, "gone": {}, "draft_x": {}, "a": {}}})
    assert strategy_api.strategy_pool_get(_request(prefs)) == {"pool": ["b", "a"], "saved": False}
    assert preferences.get_strategy_pool() is None                    # 建议不落盘
    assert not (prefs / "preferences.json").exists()


def test_pool_saved_roundtrip_distinguishes_empty_from_unsaved(prefs, monkeypatch):
    monkeypatch.setattr("app.services.strategy_cache.read_cache", lambda _d: {"results": {"a": {}}})
    saved = strategy_api.strategy_pool_put(strategy_api.StrategyPoolIn(pool=["b", "a", "b", "bad id!", ""]))
    assert saved == {"pool": ["b", "a"], "saved": True}               # 去重保序, 非法 id 丢弃
    assert strategy_api.strategy_pool_get(_request(prefs)) == {"pool": ["b", "a"], "saved": True}
    strategy_api.strategy_pool_put(strategy_api.StrategyPoolIn(pool=[]))
    assert strategy_api.strategy_pool_get(_request(prefs)) == {"pool": [], "saved": True}   # 清空 ≠ 从未保存


def test_pool_keeps_ids_the_engine_does_not_know_right_now(prefs):
    # 引擎重载瞬间少报策略时, 保存不能把池里仍有效的 id 洗掉
    assert preferences.set_strategy_pool(["mined_factor_1", "ai_c7037388"]) == ["mined_factor_1", "ai_c7037388"]


def test_pool_save_does_not_clobber_other_preferences(prefs):
    preferences.save({"nav_order": ["/", "/paper"]})
    preferences.set_strategy_pool(["a"])
    assert preferences.load()["nav_order"] == ["/", "/paper"] and preferences.get_strategy_pool() == ["a"]


def test_fixed_paths_are_registered_before_strategy_id_route():
    paths = [(r.path, sorted(r.methods)) for r in strategy_api.router.routes]
    idx = {p: i for i, (p, m) in enumerate(paths) if "GET" in m}
    assert idx["/api/strategies/panel"] < idx["/api/strategies/{strategy_id}"]
    assert idx["/api/strategies/pool"] < idx["/api/strategies/{strategy_id}"]
    assert ("/api/strategies/pool", ["PUT"]) in paths


def test_panel_endpoint_lists_public_strategies_and_rejects_bad_day(env, monkeypatch):
    seen = {}

    def fake_build(data_dir, **kw):
        seen.update(kw)
        return {"contract": sp.CONTRACT, "strategies": {}}
    monkeypatch.setattr(sp, "build", fake_build)
    req = _request(env)
    req.app.state.trading_loop = None
    assert strategy_api.strategy_panel(req, day="2026-11-10")["contract"] == sp.CONTRACT
    assert seen["strategy_ids"] == ["a", "b"] and seen["day"] == date(2026, 11, 10)   # 草稿与历史保留项不列
    with pytest.raises(strategy_api.HTTPException) as err:
        strategy_api.strategy_panel(req, day="11/10")
    assert err.value.status_code == 400
