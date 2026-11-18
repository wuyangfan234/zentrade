"""规则前向成绩 (全格子记分): 每个信号日 x 自选 x 多空都记格子卡, 只结算不下单; 每条规则和同日同向其余格子比。"""
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from test_swing_book_tight import SIG_DAY, _repo, _row, _setup, _snapshot

from app.market_clock import us_clock
from app.services import rule_forward as rf
from app.services import swing_desk as sd
from app.services import swing_pm_check as pmc
from app.services import swing_settlement as ss

D1 = date(2026, 10, 9)
D2 = date(2026, 10, 12)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    pmc._LOAD_CACHE.clear()
    pmc._LAST_TRY.clear()


def _flat(px, n=13):
    return [(px, px, px, px)] * n


def _day(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    rows = [_row("macd_golden", "AAA"), _row("ibs_mean_reversion", "AAA"), _row("tv_halftrend", "BBB", "short"),
            _row("phantom_4h", "AAA"), _row("phantom_4h_short", "CCC", "short")]
    watch = {"AAA", "BBB", "CCC", "NOATR"}
    _snapshot(tmp_path, SIG_DAY, rows, watch, {s: 100.0 for s in watch}, {"AAA": 2.0, "BBB": 2.0, "CCC": 2.0},
              h4={"BBB": "long"})
    return _repo(tmp_path)


def test_build_cells_covers_every_watch_symbol_both_sides_with_board_geometry(tmp_path, monkeypatch, cal):
    _day(tmp_path, monkeypatch)
    cards = rf.build_cells(sd.load_watchlist_snapshot(tmp_path, SIG_DAY), SIG_DAY)
    by = {(c["symbol"], c["side"]): c for c in cards}
    assert set(by) == {(s, side) for s in ("AAA", "BBB", "CCC") for side in ("long", "short")}   # 没有 ATR 的票不出格子
    g = sd.geometry("long", 100.0, 2.0)
    assert by[("AAA", "long")]["zone"] == g["entry_zone"] and by[("AAA", "long")]["stop"] == g["stop"]
    assert by[("AAA", "long")]["rules"] == ["ibs_mean_reversion", "macd_golden"]                 # 4h 来源是门, 不算规则
    assert by[("AAA", "long")]["gate"] == "event" and by[("CCC", "short")]["gate"] == "event"
    assert by[("BBB", "long")]["gate"] == "state" and by[("BBB", "short")]["gate"] == "opp"
    assert by[("BBB", "short")]["rules"] == ["tv_halftrend"] and by[("CCC", "long")]["gate"] == "none"
    assert by[("AAA", "long")]["card_id"] == "cell-2026-10-08-AAA-long"


def test_rank_factor_hits_are_recorded_without_being_ticked(tmp_path, monkeypatch, cal):
    _day(tmp_path, monkeypatch)
    snap = sd.load_watchlist_snapshot(tmp_path, SIG_DAY)
    snap["rank_values"] = {"rank_abc": {"values": {"AAA": 1.0, "BBB": 3.0, "CCC": 9.0}}}
    by = {(c["symbol"], c["side"]): c for c in rf.build_cells(snap, SIG_DAY)}
    assert "rank_abc" in by[("AAA", "long")]["rules"] and "rank_abc" in by[("BBB", "long")]["rules"]
    assert "rank_abc" not in by[("CCC", "long")]["rules"]                                        # 4h 门外不算命中


def test_record_once_then_settle_with_board_replay_and_append_only(tmp_path, monkeypatch, cal):
    repo = _day(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 8, 17, 0, tzinfo=us_clock.tz))
    rec = rf.record_cells(tmp_path, SIG_DAY)
    assert rec["contract"] == "rule-forward-v1" and rec["late"] is False and len(rec["cards"]) == 6
    before = rf.cells_path(tmp_path, SIG_DAY).read_text()
    assert rf.record_cells(tmp_path, SIG_DAY) is None and rf.cells_path(tmp_path, SIG_DAY).read_text() == before
    # 做空挂单区 100.5-101: 第 1 天 10:00 那根碰到; 做多区 99-99.5 两天都没碰到
    hit = [(100.0, 100.0, 100.0, 100.0), (100.6, 100.8, 100.4, 100.6), *_flat(100.6, 11)]
    bars = {D1: hit, D2: _flat(100.6)}
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e, **kw: {k: v for k, v in bars.items() if s <= k <= e})
    r = rf.settle_cells(tmp_path, repo, as_of_close=D2)
    assert r == {"recorded": 0, "written": 3, "pending": 3}                                      # 3 张做多未回区已定; 3 张做空在持有
    rows = rf.read_outcomes(tmp_path)
    assert {x["status"] for x in rows} == {"no_fill"} and {x["side"] for x in rows} == {"long"}
    assert {x["contract"] for x in rows} == {ss.CONTRACT} and all("gate" in x for x in rows)
    assert rf.settle_cells(tmp_path, repo, as_of_close=D2)["written"] == 0                       # 重跑不重复写
    assert ss.read_outcomes(tmp_path) == [] and ss.read_shadow_outcomes(tmp_path) == []          # 不混进板卡 / 影子结局


def test_record_marks_late_backfill_and_skips_days_without_atr(tmp_path, monkeypatch, cal):
    _day(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 20, 17, 0, tzinfo=us_clock.tz))
    assert rf.record_cells(tmp_path, SIG_DAY)["late"] is True
    _snapshot(tmp_path, D1, [_row("macd_golden", "AAA")], {"AAA"}, {"AAA": 100.0}, {})
    assert rf.record_cells(tmp_path, D1) is None and not rf.cells_path(tmp_path, D1).exists()


def _rows(n_days=10, good=0.5):
    """每天 6 张做多成交格子: 规则 good 命中 2 张 (比其余高 good), 规则 flat 命中 2 张 (与其余相同), 另 2 张无规则。
    每天整体水平大幅摆动 (-2 .. +2), 用来验证相对成绩不受当天涨跌影响。"""
    rows = []
    for i in range(n_days):
        day = f"2026-10-{i + 1:02d}"
        base = -2.0 + 4.0 * (i % 2)
        for j in range(6):
            rules = ["good"] if j < 2 else (["flat"] if j < 4 else [])
            jitter = 0.1 * ((i + j) % 3 - 1)
            rows.append({"card_id": f"cell-{day}-S{j}-long", "symbol": f"S{j}", "side": "long", "signal_day": day,
                         "rules": rules, "gate": "state" if j % 2 == 0 else "none", "status": "closed",
                         "r": round(base + (good if j < 2 else 0.0) + jitter, 4)})
        rows.append({"card_id": f"cell-{day}-S9-long", "symbol": "S9", "side": "long", "signal_day": day,
                     "rules": ["good"], "gate": "none", "status": "no_fill"})
    return rows


def test_table_scores_rules_relative_to_same_day_same_side_peers():
    t = rf.table(Path("/nonexistent"), _rows(n_days=24))
    assert t["days"] == 24 and t["cells"] == 168 and t["window"] == ["2026-10-01", "2026-10-24"]
    by = rf.by_rule(t, "all")
    g, f = by["good"], by["flat"]
    assert g["hits"] == 72 and g["filled"] == 48 and g["fill"] == round(48 / 72, 3)
    assert abs(g["mean_r"] - 0.5) < 0.11                                                         # 原始每笔 R 被涨跌日抵消后约等于 good
    # 对照 = 同日其余 5 张 (含另一张 good): 0.5 - 0.5 / 5 = 0.4 左右
    assert 0.3 < g["excess"] < 0.5 and g["ci"][0] > 0 and g["verdict"] == "above"
    assert f["verdict"] in ("below", "unclear") and f["excess"] < 0.0                            # flat 的对照里含 good, 略低于 0
    assert (g["ci"][1] - g["ci"][0]) < 0.3                                                       # 当天水平摆动 4R, 相对成绩的区间仍很窄
    assert t["scopes"]["all"]["compared"] == 2
    gate = rf.by_rule(t, "gate")                                                                 # 门内只剩 j 为偶数的 3 张 / 天
    assert gate["good"]["hits"] == 24 and gate["good"]["filled"] == 24 and gate["good"]["verdict"] == "above"


def test_few_signal_days_show_interval_but_no_verdict():
    g = rf.by_rule(rf.table(Path("/nonexistent"), _rows(n_days=10)))["good"]
    assert g["ci"] is not None and g["ci"][0] > 0 and g["verdict"] == "early"                    # 10 个信号日: 只给区间


def test_table_thin_rules_get_no_interval_and_empty_input_is_safe():
    t = rf.table(Path("/nonexistent"), _rows(n_days=5))
    assert rf.by_rule(t)["good"]["verdict"] == "thin" and rf.by_rule(t)["good"]["ci"] is None    # 信号日不足 8 个
    e = rf.table(Path("/nonexistent"), [])
    assert e["window"] is None and e["scopes"]["all"]["rules"] == [] and e["scopes"]["all"]["compared"] == 0


def test_more_rules_compared_means_stricter_bar():
    base = _rows(n_days=12, good=0.12)
    assert rf.by_rule(rf.table(Path("/x"), base))["good"]["ci"] is not None
    many = [dict(r, rules=(r["rules"] + [f"noise{k}" for k in range(40)]) if r["rules"] == ["flat"] else r["rules"]) for r in base]
    t1, t2 = rf.table(Path("/x"), base), rf.table(Path("/x"), many)
    assert t2["scopes"]["all"]["compared"] > t1["scopes"]["all"]["compared"]
    assert rf.by_rule(t1)["good"]["ci"] == rf.by_rule(t2)["good"]["ci"]                          # 展示区间不变, 只有过线门槛变严


def test_run_daily_writes_summary_and_swing_run_daily_calls_it(tmp_path, monkeypatch, cal):
    repo = _day(tmp_path, monkeypatch)
    monkeypatch.setattr(us_clock, "now", lambda: datetime(2026, 10, 12, 17, 0, tzinfo=us_clock.tz))
    monkeypatch.setattr(ss, "_m30_days", lambda data_dir, repo, symbol, s, e, **kw: {k: _flat(100.6) for k in (D1, D2) if s <= k <= e})
    monkeypatch.setattr(ss, "reconcile", lambda *a, **k: {"ok": True, "issues": []})
    res = ss.run_daily(tmp_path, repo, day=D2)
    assert res["cells"]["recorded"] == 1 and "cells_error" not in res
    s = rf.read_summary(tmp_path)
    assert s["contract"] == "rule-forward-v1" and s["as_of"] == "2026-10-12" and s["cells"] == res["cells"]["written"]
    monkeypatch.setattr(rf, "run_daily", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    res2 = ss.run_daily(tmp_path, repo, day=D2)
    assert res2["cells_error"] == "RuntimeError" and "settle" in res2                            # 它失败不拖累板卡结算


def test_rule_forward_is_outside_the_trading_chain():
    """格子卡只在结算与规则接口里: 交易环 / 准入 / 下单簿 / 卡服务 / 推送 / 出卡不引用它。"""
    root = Path(rf.__file__).parents[1]
    for rel in ("trader/trader.py", "services/trading_loop.py", "services/entry_guard.py", "services/watchlist_book.py",
                "services/opportunity_service.py", "services/notify_opportunity.py", "services/opp_radar.py",
                "services/swing_desk.py", "services/opp_page.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "rule_forward" not in src and "opportunities/cells" not in src, rel
    src = Path(rf.__file__).read_text(encoding="utf-8")
    for banned in ("save_strategies", "save_day", "ledger", "notify"):
        assert banned not in src, banned


def test_summary_json_roundtrip_shape(tmp_path):
    p = rf.summary_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"contract": "other"}))
    assert rf.read_summary(tmp_path) is None                                                     # 合同不符不读
