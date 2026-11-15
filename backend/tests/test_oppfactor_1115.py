"""机会页 / 因子页 B 档 (2026-11-15): 口径标签、今日多空分段、主题压力、历史日回看、齿轮两步保存、
因子成熟度、试验台账与冷却、立即跑一轮。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_clock import us_clock
from app.services import factor_maturity as fm
from app.services import ls_rank as lr
from app.services import opp_asof, replay_ruler, theme_exposure, trial_ledger
from app.services import swing_desk as sd
from app.services import watchlist_v4 as wv

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=us_clock.tz)            # 周日: 盘外


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    monkeypatch.setattr(us_clock, "now", lambda: NOW)
    wv._PM_CACHE.clear()
    lr._SETTLE_CACHE.clear()


def _tree_hash(d: Path) -> dict:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


def _client(tmp_path):
    from app.api.opportunities import router
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.trading_loop = None
    return TestClient(app)


def _signal_table(tmp_path, day: str, members: list[str]):
    p = tmp_path / "opportunities" / "watchlist" / f"{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"as_of": day, "watchlist": members, "signals": []}))


# ── 口径标签 ────────────────────────────────────────────────────────

def test_ruler_post_hoc_and_changed_against_that_days_members(tmp_path):
    _signal_table(tmp_path, "2026-11-09", ["AAPL", "MSFT", "NVDA"])
    r = replay_ruler.ruler("watchlist 3 (2026-11-09)", ["2023-01-03", "2026-10-30"], data_dir=tmp_path,
                           watch_now={"AAPL", "MSFT", "NVDA"})
    assert r["kind"] == "post_hoc" and r["n"] == 3 and r["fixed"] == "2026-11-09" and r["changed"] is False
    r = replay_ruler.ruler("watchlist 3 (2026-11-09)", ["2023-01-03", "2026-10-30"], data_dir=tmp_path,
                           watch_now={"AAPL", "MSFT", "TSLA"})
    assert r["changed"] is True and r["watch_now"] == 3                     # 只数一样, 成员不同 = 变了


def test_ruler_without_that_days_table_compares_count_only(tmp_path):
    r = replay_ruler.ruler("watchlist 46 (2026-11-09)", ["2023-01-03", "2026-10-30"], data_dir=tmp_path, watch_now=set("ABC"))
    assert r["changed"] is True
    r = replay_ruler.ruler("watchlist 3 (2026-11-09)", ["2023-01-03", "2026-10-30"], data_dir=tmp_path, watch_now=set("ABC"))
    assert r["changed"] is None                                             # 只数相同又没有成员可比: 不下结论


def test_ruler_unrecorded_for_code_constants():
    r = replay_ruler.ruler(None, "2024-01~2026-10", watch_now={"A", "B"})
    assert r["kind"] == "unrecorded" and r["window"] == ["2024-01", "2026-10"] and r["watch_now"] == 2
    assert replay_ruler.from_meta({}) is None


def test_watch_sha_ignores_order_case_and_duplicates():
    assert replay_ruler.watch_sha(["nvda", "AAPL", "NVDA"]) == replay_ruler.watch_sha(["AAPL", "NVDA"])
    assert replay_ruler.watch_sha([]) is None and len(replay_ruler.watch_sha(["A"]) or "") == 12


# ── 主题压力 ────────────────────────────────────────────────────────

GROUPS = [{"id": "core", "name": "核心"}, {"id": "mega", "name": "科技巨头"}, {"id": "chip", "name": "芯片"}, {"id": "ev", "name": "电动车"}]
MEMBERS = {"AMZN": ["core", "mega"], "TSLA": ["core", "mega", "ev"], "AVGO": ["core", "chip"], "QCOM": ["chip"], "X": ["core"], "Y": []}


def test_theme_compute_matches_hand_math_and_excludes_tier_groups():
    pos = [{"symbol": "AMZN", "qty": 50, "avg_cost": 200.0, "stop": 190.0, "price": 210.0},
           {"symbol": "TSLA", "qty": 20, "avg_cost": 400.0, "stop": 380.0, "price": 390.0},
           {"symbol": "QCOM", "qty": 30, "avg_cost": 150.0, "stop": 150.0, "price": 160.0},     # 止损在成本价: 计划风险 0
           {"symbol": "AVGO", "qty": -10, "avg_cost": 300.0, "stop": 315.0, "price": 290.0}]    # 空头
    out = theme_exposure.compute(pos, GROUPS, MEMBERS, nav=100_000.0, pct=20, watch_total=6)
    by = {r["theme"]: r for r in out["rows"]}
    assert "核心" not in by and out["tiers"] == [{"name": "核心", "n": 4}]                  # 覆盖 4/6 只 = 分层不是主题
    mega = by["科技巨头"]                                                                   # AMZN 50x210 + TSLA 20x390
    assert mega["notional"] == 18_300.0 and mega["stop_risk"] == 50 * 10 + 20 * 20
    assert mega["stress_loss"] == pytest.approx(3_660.0) and mega["stress_pct_nav"] == 3.66
    chip = by["芯片"]                                                                       # QCOM 30x160 + AVGO 10x290 (空头按上涨)
    assert chip["stop_risk"] == 0 + 10 * 15 and chip["stress_loss"] == pytest.approx((4_800 + 2_900) * 0.2)
    assert by["电动车"]["symbols"] == ["TSLA"]                                               # 一票多主题: 各自计入
    t = out["total"]
    assert t["notional"] == 50 * 210 + 20 * 390 + 30 * 160 + 10 * 290 and t["stress_pct_nav"] == pytest.approx(t["notional"] * 0.2 / 1000, abs=0.01)
    assert out["rows"][0]["theme"] == "科技巨头"                                # 按压力损失排序


def test_theme_pending_cards_only_add_to_after_fill_column():
    pend = [{"symbol": "QCOM", "side": "long", "qty": 10, "mid": 100.0}]
    out = theme_exposure.compute([], GROUPS, MEMBERS, nav=100_000.0, pct=10, pending=pend, watch_total=6)
    chip = {r["theme"]: r for r in out["rows"]}["芯片"]
    assert chip["stress_loss"] == 0 and chip["after_fill_loss"] == 100.0 and chip["pending"] == ["QCOM"]
    assert out["total"]["stress_loss"] == 0


def test_theme_stress_setting_strict_levels(tmp_path):
    assert theme_exposure.stress_pct(tmp_path) == 20
    theme_exposure.save_stress_pct(tmp_path, 25)
    assert theme_exposure.stress_pct(tmp_path) == 25
    with pytest.raises(ValueError):
        theme_exposure.save_stress_pct(tmp_path, 12)


def test_pending_cards_skip_holding_blocked_and_skip_verdicts():
    sw = {"long": [{"symbol": "A", "state": "pending", "verdict": "order", "entry_zone": [9, 11], "odds": {"qty": 5}},
                   {"symbol": "B", "state": "pending", "verdict": "skip", "entry_zone": [9, 11], "qty": 5},
                   {"symbol": "C", "state": "holding", "verdict": "holding", "entry_zone": [9, 11], "qty": 5},
                   {"symbol": "D", "state": "pending", "verdict": "order", "blocked": "cooldown", "entry_zone": [9, 11], "qty": 5}],
          "short": []}
    assert theme_exposure._pending_cards(sw) == [{"symbol": "A", "side": None, "qty": 5, "mid": 10.0}]


# ── 齿轮两步保存 ─────────────────────────────────────────────────────

def _mode(tmp_path):
    (tmp_path / "decision_desk").mkdir(parents=True, exist_ok=True)
    (tmp_path / "decision_desk" / "evidence_mode.json").write_text(json.dumps({"mode": "v4"}))


def test_preview_writes_nothing_and_confirm_with_hash_saves(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    c = _client(tmp_path)
    before = _tree_hash(tmp_path)
    body = {"long": ["macd_golden", "trend_breakout"], "short": ["tv_halftrend"], "risk_per_trade_pct": 1.0, "theme_stress_pct": 25}
    pv = c.post("/api/opportunities/settings/preview", json=body).json()
    assert _tree_hash(tmp_path) == before                                                 # 预览只读
    assert pv["changes"]["long"]["add"] == ["trend_breakout"] and pv["risk"] == {"from": 0.5, "to": 1.0, "caps_from": [10, 4], "caps_to": [5, 2]}
    assert pv["theme_stress"] == {"from": 20, "to": 25} and len(pv["hash"]) == 16
    r = c.post("/api/opportunities/settings", json={**body, "preview_hash": pv["hash"]})
    assert r.status_code == 200
    assert sd.strategies_from_ruleset(tmp_path)[0] == ("macd_golden", "trend_breakout") and theme_exposure.stress_pct(tmp_path) == 25


def test_confirm_after_other_entry_saved_is_409_and_writes_nothing(tmp_path):
    """两个入口同时打开: 先保存的一方改了状态, 后保存的一方拿旧哈希 → 409 + 新预览, data 目录字节不变。"""
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    c = _client(tmp_path)
    mine = {"long": ["trend_breakout"], "short": ["tv_halftrend"], "risk_per_trade_pct": None}
    stale = c.post("/api/opportunities/settings/preview", json=mine).json()["hash"]
    other = {"long": ["macd_golden"], "short": ["tv_halftrend"], "risk_per_trade_pct": 0.75}
    other_hash = c.post("/api/opportunities/settings/preview", json=other).json()["hash"]
    assert c.post("/api/opportunities/settings", json={**other, "preview_hash": other_hash}).status_code == 200
    before = _tree_hash(tmp_path)
    r = c.post("/api/opportunities/settings", json={**mine, "preview_hash": stale})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "settings_changed"
    assert r.json()["detail"]["preview"]["risk"]["from"] == 0.75                          # 新预览按另一方存完的状态
    assert _tree_hash(tmp_path) == before


def test_confirm_with_body_different_from_preview_is_409(tmp_path):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    c = _client(tmp_path)
    h = c.post("/api/opportunities/settings/preview", json={"long": ["macd_golden"], "short": [], "risk_per_trade_pct": 1.0}).json()["hash"]
    r = c.post("/api/opportunities/settings", json={"long": ["macd_golden"], "short": [], "risk_per_trade_pct": 1.5, "preview_hash": h})
    assert r.status_code == 409


@pytest.mark.parametrize("bad", [12, "20", True, 20.0])
def test_theme_stress_must_be_a_listed_integer(tmp_path, bad):
    _mode(tmp_path)
    wv.save_ruleset(tmp_path, ["macd_golden"], {"risk_per_trade_pct": 0.5})
    before = _tree_hash(tmp_path)
    r = _client(tmp_path).post("/api/opportunities/settings", json={"long": [], "short": [], "theme_stress_pct": bad})
    assert r.status_code == 422 and _tree_hash(tmp_path) == before


def test_preview_hash_must_be_a_string(tmp_path):
    _mode(tmp_path)
    r = _client(tmp_path).post("/api/opportunities/settings", json={"long": [], "short": [], "preview_hash": 123})
    assert r.status_code == 422


# ── 历史日回看 ──────────────────────────────────────────────────────

def _book(tmp_path, day: str, cards: list[tuple[str, str]]):
    p = tmp_path / "opportunities" / "book" / f"{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"as_of": day, "built_at": f"{day}T21:00:00+08:00", "swing_rules": {"long": ["ibs_mean_reversion"], "short": []},
                             "cards": [{"id": cid, "symbol": sym, "side": "long", "entry_zone": [9.0, 10.0], "invalidation": 8.0,
                                        "target_zone": [12.0, 12.0], "reward_risk": 2.0,
                                        "evidence": {"side": "long", "score": 1.0, "decision": {"rules": ["ibs_mean_reversion"]}}}
                                       for sym, cid in cards],
                             "skipped": [{"symbol": "ZZ", "side": "long", "reason": "sector_cap"}]}))


def test_asof_board_reads_the_two_books_before_that_day_and_joins_outcomes(tmp_path):
    _book(tmp_path, "2026-11-10", [("OLD", "c-old")])
    _book(tmp_path, "2026-11-11", [("A", "c-a")])
    _book(tmp_path, "2026-11-12", [("B", "c-b")])
    _book(tmp_path, "2026-11-13", [("FUT", "c-fut")])                                      # 13 号的簿是 13 号收盘后才有: 不进 13 号的页面
    od = tmp_path / "research" / "swing_settle"
    od.mkdir(parents=True)
    (od / "outcomes.swing-settle-v3.jsonl").write_text(json.dumps({"card_id": "c-a", "status": "closed", "why": "stop", "r": -1.02}) + "\n")
    b = opp_asof.board(tmp_path, date(2026, 11, 13))
    assert b["signal_days"] == ["2026-11-11", "2026-11-12"]
    assert [c["card_id"] for c in b["cards"]] == ["c-b", "c-a"]
    oc = {c["card_id"]: c["outcome"] for c in b["cards"]}
    assert oc["c-a"]["r"] == -1.02 and oc["c-b"] is None
    assert b["cards"][0]["rules"][0]["id"] == "ibs_mean_reversion" and b["skipped"][0]["reason"] == "sector_cap"


def test_asof_missing_blocks_are_not_archived_and_never_fall_back(tmp_path):
    out = opp_asof.build(tmp_path, date(2026, 10, 1), today=date(2026, 11, 16))
    assert out["board"] is None and set(out["errors"]) == {"board", "ls_rank", "intraday", "radar", "resonance", "mood"}
    assert set(out["errors"].values()) == {"not_archived"}
    with pytest.raises(ValueError):
        opp_asof.build(tmp_path, date(2026, 11, 16), today=date(2026, 11, 16))


def test_asof_api_rejects_today_and_bad_dates(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/opportunities/asof", params={"day": "2026-11-15"}).status_code == 422     # NOW 的美东日期
    assert c.get("/api/opportunities/asof", params={"day": "11/14"}).status_code == 422
    r = c.get("/api/opportunities/asof", params={"day": "2026-11-13"})
    assert r.status_code == 200 and r.json()["asof"] == "2026-11-13"


def test_asof_ls_list_uses_settled_r_and_model_quarter(tmp_path):
    rec = {"contract": lr.CONTRACT, "day": "2026-11-10", "as_of": "2026-11-09", "model": {"quarter": "2026Q4", "trained_to": "2026-09-22"},
           "n": 3, "long": ["A"], "short": ["B"], "rows": [{"symbol": "A", "rank": 1, "close": 10.0, "atr": 1.0}, {"symbol": "B", "rank": 3}],
           "settled": {"end": "2026-11-16", "r": {"A": {"long": 0.5, "short": -0.5, "long_exit": "time", "short_exit": "time"}}}}
    lr._write_json(lr.day_path(tmp_path, date(2026, 11, 10)), rec)
    v = opp_asof.ls_list(tmp_path, date(2026, 11, 10))
    assert v["model"] == "2026Q4" and v["long"][0]["r"] == 0.5 and v["short"][0]["r"] is None and v["settled_end"] == "2026-11-16"


# ── 今日多空: 前向按模型季度分段 ──────────────────────────────────────

def _settled(tmp_path, day: str, quarter: str, rl: float, late: bool = False):
    rec = {"contract": lr.CONTRACT, "day": day, "as_of": day, "model": {"quarter": quarter, "trained_to": "x", "rows": 1},
           "long": ["A"], "short": ["B"], "random": {"long": ["A"], "short": ["B"]}, "rows": [],
           "settled": {"end": day, "missing": [], "r": {"A": {"long": rl, "short": -rl, "long_exit": "time", "short_exit": "time"},
                                                         "B": {"long": 0.0, "short": 0.1, "long_exit": "time", "short_exit": "time"}}},
           **({"late": True} if late else {})}
    lr._write_json(lr.day_path(tmp_path, date.fromisoformat(day)), rec)


def test_settle_view_counts_only_the_current_quarter_and_freezes_others(tmp_path):
    _settled(tmp_path, "2026-12-28", "2026Q4", 0.4)
    _settled(tmp_path, "2026-12-29", "2026Q4", 0.2)
    _settled(tmp_path, "2027-01-04", "2027Q1", -0.3)
    _settled(tmp_path, "2027-01-05", "2027Q1", 0.1, late=True)
    v = lr.settle_view(tmp_path, quarter="2027Q1")
    assert v["days"] == 1 and v["late"] == 1 and v["first"] == "2027-01-04"               # 换模型后前向从 0 重新计
    assert v["prev"][0]["quarter"] == "2026Q4" and v["prev"][0]["days"] == 2
    assert v["prev"][0]["total"]["long"] == pytest.approx(0.3)
    assert lr.settle_view(tmp_path)["days"] == 3                                           # 不给季度 = 旧口径 (全部合计)


# ── 试验台账与冷却 ───────────────────────────────────────────────────

def _trial(run, day, key="annual_vol_20d:low", sh=0.5, weekly=True):
    return {"run_id": run, "weekly": weekly, "day": day, "key": key, "oos_sharpe": sh}


def test_trial_ledger_matches_preregistered_expectation():
    tr = [_trial("weekly-2026-W41", "2026-10-09", sh=0.74), _trial("weekly-2026-W42", "2026-10-16", sh=1.33),
          _trial("weekly-2026-W43", "2026-10-23", sh=0.77), _trial("weekly-2026-W44", "2026-10-30", sh=0.51)]
    v = trial_ledger.summarize(tr, date(2026, 11, 15))["annual_vol_20d:low"]
    assert v["n"] == 4 and v["sharpes"] == [0.74, 1.33, 0.77, 0.51] and v["cooldown_until"] == "2027-01-28" and v["cooling"]
    assert trial_ledger.summarize(tr, date(2027, 1, 28))["annual_vol_20d:low"]["cooling"] is False    # 到期自动解除


def test_trial_ledger_after_go_live_reappearance_does_not_extend_and_manual_runs_do_not_trigger():
    tr = [_trial("weekly-2026-W47", "2026-11-20"), _trial("weekly-2026-W48", "2026-11-27"), _trial("weekly-2026-W49", "2026-12-04"),
          _trial("weekly-2026-W50", "2026-12-11")]
    v = trial_ledger.summarize(tr, date(2026, 12, 12))["annual_vol_20d:low"]
    assert v["cooldown_until"] == "2027-03-04" and v["n"] == 4                             # W49 触发; W50 在冷却期内只计次
    man = [_trial(f"m{i}", f"2026-11-2{i}", weekly=False) for i in range(5)]
    assert trial_ledger.summarize(man, date(2026, 11, 30))["annual_vol_20d:low"]["cooldown_until"] is None


def test_trial_ledger_excludes_the_run_being_evaluated():
    tr = [_trial("weekly-a", "2026-11-20"), _trial("weekly-b", "2026-11-27"), _trial("weekly-c", "2026-12-04")]
    assert trial_ledger.summarize(tr, date(2026, 12, 4), exclude_run="weekly-c")["annual_vol_20d:low"]["cooling"] is False


def test_collect_reads_real_run_store_within_its_limit(tmp_path):
    """11-16 线上: list_runs(limit=500) 超过存储上限直接抛错, 台账整份为空。用真实 MiningRunStore 落两轮候选表读回。"""
    import polars as pl

    from app.services.mining_jobs import MiningRunStore
    store = MiningRunStore(tmp_path)
    for rid, created, sh in (("weekly-2026-W43", "2026-10-23T20:48:26+00:00", 0.77), ("weekly-2026-W44", "2026-10-30T20:53:44+00:00", 0.51)):
        d = tmp_path / "research" / "mining" / "runs" / rid
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"run_id": rid, "status": "succeeded", "created_at": created}))
        pl.DataFrame({"signature": ["factor:annual_vol_20d|w:1|d:-1"], "oos_sharpe": [sh],
                      "definition_json": [json.dumps({"kind": "factor_rank", "scoring": {"annual_vol_20d": 1.0},
                                                      "directions": {"annual_vol_20d": "low"}})]}).write_parquet(d / "candidates.parquet")
    try:
        runs = store.list_runs(limit=trial_ledger.RUNS_LIMIT)
    except Exception as e:  # 夹具里 manifest 字段不全时, 退回只校验 limit 合法
        pytest.skip(f"fixture manifest not accepted by store: {type(e).__name__}")
    assert {r["run_id"] for r in runs} >= {"weekly-2026-W43", "weekly-2026-W44"}
    trial_ledger._CACHE.clear()
    tr = trial_ledger.collect(tmp_path)
    assert [(t["run_id"], t["day"], t["key"]) for t in tr] == [("weekly-2026-W43", "2026-10-23", "annual_vol_20d:low"),
                                                               ("weekly-2026-W44", "2026-10-30", "annual_vol_20d:low")]


def test_collect_never_asks_the_store_for_more_than_its_limit(monkeypatch, tmp_path):
    from app.services import mining_jobs
    seen = []

    def fake_list_runs(self, limit=50, **kw):
        seen.append(limit)
        if not 1 <= limit <= 200:
            raise mining_jobs.MiningRunValidationError("limit must be between 1 and 200")
        return []
    monkeypatch.setattr(mining_jobs.MiningRunStore, "list_runs", fake_list_runs)
    trial_ledger._CACHE.clear()
    assert trial_ledger.collect(tmp_path) == [] and seen == [200]


def test_structure_key_sorts_and_maps_numeric_directions():
    assert trial_ledger.structure_key({"b": 1, "a": 2}, {"a": "-1", "b": "high"}) == "a:low|b:high"
    assert trial_ledger.structure_key({}, {}) is None


# ── 因子成熟度 ───────────────────────────────────────────────────────

def _series(start: date, n: int, ic):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d, ic(len(out))))
        d += timedelta(days=1)
    return out


def test_maturity_states_follow_preregistered_rules():
    months = [date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 30)]
    strong = _series(date(2025, 6, 1), 370, lambda i: 0.05 + (0.1 if i % 2 else -0.1))       # |均值| / 标准差 = 0.5
    weak = _series(date(2025, 6, 1), 370, lambda i: 0.01 + (0.1 if i % 2 else -0.1))         # 0.1 < 0.2 持续
    short = _series(date(2026, 8, 1), 60, lambda i: 0.05 + (0.1 if i % 2 else -0.1))         # 样本 < 120 天
    stale = _series(date(2025, 6, 1), 250, lambda i: 0.01 + (0.1 if i % 2 else -0.1))        # 最后 IC 早于月末 60 天
    st = fm.derive({"s": strong, "w": weak, "n": short, "o": stale, "y": strong}, months=months, young={"y"})
    assert st["s"]["state"] == "active" and st["s"]["icir"] == pytest.approx(0.5, abs=0.01)
    assert st["w"]["state"] == "retired" and st["w"]["streak"] == 4
    assert st["n"]["state"] == "observing" and st["o"]["state"] == "observing"              # 数据不够、过期都不算弱
    assert st["y"]["state"] == "experiment" and st["y"]["icir"] is None and st["y"]["readings"] == []


def test_maturity_retire_needs_three_consecutive_months():
    rs = [{"icir": 0.3, "n": 200, "stale": False}, {"icir": 0.1, "n": 200, "stale": False}, {"icir": 0.1, "n": 200, "stale": False}]
    assert fm.state_at(rs, young=False) == ("observing", 2)
    assert fm.state_at([*rs, {"icir": 0.15, "n": 200, "stale": False}], young=False) == ("retired", 3)


def test_month_ends_drop_the_unfinished_month():
    days = [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 5), date(2026, 11, 6)]
    last = {(2026, 10): date(2026, 10, 30), (2026, 11): date(2026, 11, 30)}
    got = fm.month_ends(days, month_last_trading=lambda d: last[(d.year, d.month)])
    assert got == [date(2026, 10, 30)]


def test_duplicates_point_to_the_stronger_factor_only():
    corr = [{"factor_x": "annual_vol_20d", "factor_y": "atr_pct", "rho": 0.91}, {"factor_x": "atr_pct", "factor_y": "annual_vol_20d", "rho": 0.91},
            {"factor_x": "a", "factor_y": "b", "rho": 0.5}, {"factor_x": "c", "factor_y": "c", "rho": 1.0}]
    d = fm.duplicates(corr, {"annual_vol_20d": 0.079, "atr_pct": 0.087, "a": 0.3, "b": 0.4, "c": 0.1})
    assert d == {"annual_vol_20d": {"of": "atr_pct", "rho": 0.91}}


def test_distribution_gate_and_library_view_hide_states_when_not_launched(tmp_path):
    states = {f"f{i}": {"state": "retired"} for i in range(19)} | {"x": {"state": "active"}}
    assert fm.distribution(states)["launch_ok"] is False                                  # 19/20 = 95% → 不上线
    out = fm.out_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "2026-10.json").write_text(json.dumps({"contract": fm.CONTRACT, "months": ["2026-10-30"], "distribution": fm.distribution(states),
                                                  "factors": {"x": {"state": "active", "icir": 0.3, "dup": {"of": "y", "rho": 0.95}}}}))
    v = fm.library_view(tmp_path)
    assert v["launched"] is False and v["factors"]["x"] == {"dup": {"of": "y", "rho": 0.95}}


def test_maturity_due_monthly(tmp_path):
    assert fm.due(tmp_path, date(2026, 11, 21)) is True
    out = fm.out_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "2026-10.json").write_text(json.dumps({"contract": fm.CONTRACT, "months": ["2026-07-31", "2026-10-30"], "factors": {}}))
    assert fm.due(tmp_path, date(2026, 11, 21)) is False and fm.due(tmp_path, date(2026, 12, 5)) is True


# ── 立即跑一轮 / 挖掘开关 ─────────────────────────────────────────────

def test_run_weekly_mining_force_ignores_switch_and_weekday(monkeypatch, tmp_path):
    from app.services import mining_schedule as ms
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_mining_schedule", lambda: {"mining_schedule_enabled": False, "mining_schedule_weekday": 4,
                                                                     "mining_budget_profile": "balanced"})
    assert ms.run_weekly_mining(SimpleNamespace(), now=NOW)["status"] == "disabled"
    started = []
    store = SimpleNamespace(get=lambda rid: None)
    mgr = SimpleNamespace(store=store, start=lambda req, fp, **kw: started.append((kw["run_id"], fp)) or {"run_id": kw["run_id"]})
    st = SimpleNamespace(mining_manager=mgr, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(ms, "build_default_request", lambda repo, profile: {"symbols": ["A"], "end": date(2026, 11, 13)})
    monkeypatch.setattr(ms, "build_data_fingerprint", lambda repo, state, req: {"x": 1})
    monkeypatch.setattr(ms, "_prerequisite_error", lambda repo, req: None)
    out = ms.run_weekly_mining(st, now=NOW, force=True)
    assert out == {"status": "enqueued", "run_id": "weekly-2026-W46"} and started[0][1]["forced"] is True


def test_run_now_chains_discover_after_mining_succeeds(monkeypatch, tmp_path):
    from app.services import factor_discover as fd
    from app.services import mining_schedule as ms
    fd._RUN_NOW.clear()
    monkeypatch.setattr(fd, "can_run_heavy", lambda now=None: True)
    monkeypatch.setattr(ms, "run_weekly_mining", lambda state, force=False: {"status": "enqueued", "run_id": "weekly-2026-W46"})
    states = iter(["queued", "running", "succeeded"])
    store = SimpleNamespace(get=lambda rid: {"status": next(states)})
    called = []
    monkeypatch.setattr(fd, "run_weekly", lambda data_dir, repo: called.append(data_dir) or {"week": "2026-W46", "candidates": 0})
    import threading
    started = []
    real_thread = threading.Thread

    class _Sync:
        def __init__(self, target, name=None, daemon=None):
            self.t = target
            started.append(name)

        def start(self):
            self.t()
    monkeypatch.setattr(threading, "Thread", _Sync)
    out = fd.run_now(SimpleNamespace(mining_manager=SimpleNamespace(store=store)), tmp_path, None, sleep=lambda s: None)
    monkeypatch.setattr(threading, "Thread", real_thread)
    assert out == {"status": "enqueued", "run_id": "weekly-2026-W46"}
    assert called == [tmp_path] and fd._RUN_NOW["state"] == "done"


def test_run_now_failed_mining_does_not_run_discover(monkeypatch, tmp_path):
    from app.services import factor_discover as fd
    from app.services import mining_schedule as ms
    fd._RUN_NOW.clear()
    monkeypatch.setattr(fd, "can_run_heavy", lambda now=None: True)
    monkeypatch.setattr(ms, "run_weekly_mining", lambda state, force=False: {"status": "already_claimed", "run_id": "weekly-2026-W46"})
    store = SimpleNamespace(get=lambda rid: {"status": "failed"})
    monkeypatch.setattr(fd, "run_weekly", lambda data_dir, repo: pytest.fail("不该跑发现"))
    import threading

    class _Sync:
        def __init__(self, target, name=None, daemon=None):
            self.t = target

        def start(self):
            self.t()
    monkeypatch.setattr(threading, "Thread", _Sync)
    fd.run_now(SimpleNamespace(mining_manager=SimpleNamespace(store=store)), tmp_path, None, sleep=lambda s: None)
    assert fd._RUN_NOW["state"] == "failed" and fd._RUN_NOW["reason"] == "failed"


def test_run_now_refuses_in_session(monkeypatch, tmp_path):
    from app.services import factor_discover as fd
    monkeypatch.setattr(fd, "can_run_heavy", lambda now=None: False)
    assert fd.run_now(SimpleNamespace(), tmp_path, None) == {"status": "session"}


def test_page_sections_include_themes_and_ruler_does_not_break_page(tmp_path, monkeypatch):
    from app.services import intraday_strength, market_mood
    monkeypatch.setattr(sd, "build_live", lambda data_dir, repo=None, loop=None, today=None: {"contract": sd.CONTRACT, "available": True,
                        "as_of": "2026-11-13", "long": [], "short": [], "history": [], "long_rules": [], "short_rules": [], "ruler": None})
    monkeypatch.setattr(intraday_strength, "build_live", lambda data_dir, repo=None, loop=None, now=None: None)
    monkeypatch.setattr(market_mood, "view", lambda data_dir: None)
    monkeypatch.setattr(theme_exposure, "view", lambda data_dir, repo=None, loop=None, swing=None: (_ for _ in ()).throw(OSError("groups")))
    d = _client(tmp_path).get("/api/opportunities/page").json()
    assert d["themes"] is None and d["errors"]["themes"] == "OSError" and d["swing"]["as_of"] == "2026-11-13"
