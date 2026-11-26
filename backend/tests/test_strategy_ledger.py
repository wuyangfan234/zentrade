"""P1-B1 策略账本: 五档判定 / 收缩超额打分替换排序主键 / 冷冻不打分 / 观察不进簿 / 可复现。"""
import json
from datetime import date

import pytest

from app.opportunity import strategy_ledger as sl
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv
from test_regime_p1a import SIG_DAY, _empty_repo, _setup, cal  # noqa: F401  (cal = fixture)

pytestmark = pytest.mark.real_strategy_ledger
DAY = date(2026, 11, 20)


# ── 1. 五档判定 (含边界) ───────────────────────────────────────────────────────

def test_classify_active_boundary_n29_vs_n30():
    assert sl.classify(30, 0.12, 0.01, 0.23) == sl.ACTIVE
    assert sl.classify(29, 0.12, 0.01, 0.23) == sl.PENDING         # n < 30: 证据不足 → 待定 (不是冷冻)
    assert sl.classify(200, 0.12, None, None) == sl.PENDING        # CI 缺 → 待定


def test_classify_observe_and_ci_low_exactly_zero():
    assert sl.classify(200, 0.06, -0.01, 0.13) == sl.OBSERVE
    assert sl.classify(200, 0.06, 0.0, 0.12) == sl.OBSERVE          # ci_low 恰为 0 → 不是在用, 是观察
    assert sl.classify(29, 0.06, -0.01, 0.13) == sl.PENDING


def test_classify_frozen_streak_1_vs_2_and_ci_high_below_zero():
    assert sl.classify(200, -0.01, -0.08, 0.06, freeze_streak=1) == sl.PENDING  # 负一个月 → 待定, 不冷冻
    assert sl.classify(200, -0.01, -0.08, 0.06, freeze_streak=2) == sl.FROZEN   # 连续两个月 → 冷冻
    assert sl.classify(200, 0.0, -0.08, 0.08, freeze_streak=0) == sl.PENDING    # 点估计 = 0 → 待定
    assert sl.classify(200, 0.06, -0.01, 0.13, freeze_streak=1) == sl.OBSERVE   # streak=1 不冷冻
    assert sl.classify(200, 0.06, -0.01, 0.13, freeze_streak=2) == sl.FROZEN    # streak=2 冷冻
    assert sl.classify(200, 0.30, 0.10, 0.50, freeze_streak=2) == sl.FROZEN
    assert sl.classify(200, -0.20, -0.30, -0.01) == sl.FROZEN                   # ci_high < 0


def test_classify_revive_only_from_frozen_with_new_sample():
    assert sl.classify(30, 0.2, 0.01, 0.4, prev_tier=sl.FROZEN) == sl.REVIVED
    assert sl.classify(29, 0.2, 0.01, 0.4, prev_tier=sl.FROZEN) == sl.FROZEN
    assert sl.classify(300, 0.2, 0.0, 0.4, prev_tier=sl.FROZEN) == sl.FROZEN     # ci_low 恰 0 不复活
    assert sl.classify(30, 0.2, 0.01, 0.4, prev_tier=sl.ACTIVE) == sl.ACTIVE
    assert sl.classify(30, 0.2, 0.01, 0.4, prev_tier=sl.FROZEN, freeze_streak=2) == sl.FROZEN  # 冷冻条件优先
    assert sl.classify(200, 0.06, -0.01, 0.13, prev_tier=sl.FROZEN) == sl.FROZEN    # CI 跨 0 不复活


def test_pending_last_month_n29_becomes_observe_when_evidence_arrives():
    """审查复现: 上月 n=29 → 待定; 本月 n=35, excess=0.2, CI 跨 0 → 必须是观察 (旧实现卡在冷冻)。"""
    prev = sl.review_cells({"r|short": {"n": 29, "excess": 0.29, "ci_low": -0.2, "ci_high": 0.79}}, None)
    assert prev["r|short|震荡"]["tier"] == sl.PENDING
    cur = sl.review_cells({"r|short": {"n": 35, "excess": 0.2, "ci_low": -0.05, "ci_high": 0.45}}, prev)
    assert cur["r|short|震荡"]["tier"] == sl.OBSERVE


def test_frozen_then_revived_via_review():
    prev = sl.review_cells({"r|long": {"n": 300, "excess": -0.2, "ci_low": -0.3, "ci_high": -0.05}}, None)
    assert prev["r|long|进攻"]["tier"] == sl.FROZEN
    cur = sl.review_cells({"r|long": {"n": 300, "excess": 0.05, "ci_low": -0.01, "ci_high": 0.1}}, prev)
    assert cur["r|long|进攻"]["tier"] == sl.FROZEN                                  # 冷冻后不回观察
    nxt = sl.review_cells({"r|long": {"n": 300, "excess": 0.2, "ci_low": 0.02, "ci_high": 0.4}}, cur)
    assert nxt["r|long|进攻"]["tier"] == sl.REVIVED


def test_monthly_review_streak_freezes_on_second_negative_month(tmp_path, monkeypatch):
    stats = {"cells": {"r1|long": {"n": 200, "excess": -0.01, "ci_low": -0.08, "ci_high": 0.06}},
             "window": ["2023-01-03", "2026-10-30"], "data_end": "2026-11-09"}
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: stats)
    r1 = sl.monthly_review(tmp_path, date(2026, 10, 5), today=date(2026, 10, 5))
    assert (r1["cells"]["r1|long|震荡"]["freeze_streak"], r1["cells"]["r1|long|震荡"]["tier"]) == (1, sl.PENDING)
    assert sl.monthly_review(tmp_path, date(2026, 10, 20), today=date(2026, 10, 20)) == r1   # 同月只认第一条
    r2 = sl.monthly_review(tmp_path, date(2026, 11, 3), today=date(2026, 11, 3))
    assert r2["cells"]["r1|long|震荡"] == {**r1["cells"]["r1|long|震荡"], "freeze_streak": 2, "tier": sl.FROZEN}
    lines = sl.audit_path(tmp_path).read_text().splitlines()
    assert [json.loads(x)["month"] for x in lines] == ["2026-10", "2026-11"]   # 只追加, 每月一条


def test_history_month_never_backfilled(tmp_path, monkeypatch):
    stats = {"cells": {"r1|long": {"n": 200, "excess": 0.1, "ci_low": 0.01, "ci_high": 0.2}},
             "window": None, "data_end": None}
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: stats)
    sl.monthly_review(tmp_path, date(2026, 11, 3), today=date(2026, 11, 3))
    before = sl.audit_path(tmp_path).read_text().splitlines()
    assert sl.monthly_review(tmp_path, date(2026, 9, 15), today=date(2026, 11, 3)) is None  # 过去月份: 只读
    assert sl.monthly_review(tmp_path, date(2026, 12, 1), today=date(2026, 11, 3)) is None  # 未来月份同理
    assert sl.audit_path(tmp_path).read_text().splitlines() == before                       # 行数不变
    assert sl.monthly_review(tmp_path, date(2026, 11, 28), today=date(2026, 12, 2))["month"] == "2026-11"
    monkeypatch.setattr(sl, "_today", lambda: date(2026, 11, 3))
    lg = sl.ledger_for(tmp_path, date(2026, 8, 3), "震荡")                                  # P1-B2: 无账本 → fail-closed
    assert lg["status"] == sl.STATUS_UNAVAILABLE and lg["cells"] == {}
    assert sl.lookup(lg, "r1", "long")[0] == sl.PENDING and sl.lookup(lg, "rank_x", "long")[0] == sl.PENDING
    assert len(sl.audit_path(tmp_path).read_text().splitlines()) == 1


def test_ci_reuses_week_boot_and_summary_inverse():
    pairs = [(t, 0.1 + (0.05 if t % 2 else -0.05)) for t in range(60)]
    lo, hi = sl.ci_from_pairs(pairs)
    from app.services import swing_candidates as sc
    assert [lo, hi] == sc.week_boot_ci(pairs)
    lo, hi = sl.ci_from_summary(0.1267, 0.024)                                  # 生产 zlsma: p<0.025 ↔ 下限>0
    assert lo > 0 and hi > 0.2
    lo, _ = sl.ci_from_summary(0.0607, 0.035)                                   # 生产 ibs: p>0.025 ↔ 下限≤0
    assert lo <= 0


# ── 2-4. 收缩超额打分 ─────────────────────────────────────────────────────────

def _ledger(cells, k=100):
    return {"contract": sl.CONTRACT, "month": "2026-11", "regime": "震荡", "k": k,
            "cells": {sl.cell_key(r, s): {"n": n, "excess": e, "tier": t} for (r, s), (n, e, t) in cells.items()}}


def _sig(rule, sym, side="long"):
    return {"symbol": sym, "rule": rule, "side": side}


def _board(ledger, signals, syms, rules=("ra", "rb", "rc"), **kw):
    return sd.compose_boards(signals=signals, closes={s: 100.0 for s in syms}, atr={s: 2.0 for s in syms},
                             h4_state={s: "long" for s in syms}, as_of=DAY, long_rules=rules, short_rules=(),
                             ledger=ledger, **kw)


def test_off_table_rules_no_longer_tie():
    """旧实现: 表外规则一律 1 + 0.05×0.1 = 1.005, 两张卡同分; 现在按各自收缩超额分开。"""
    lg = _ledger({("ra", "long"): (500, 0.10, sl.ACTIVE), ("rb", "long"): (500, 0.05, sl.ACTIVE)})
    b = _board(lg, [_sig("ra", "AAA"), _sig("rb", "BBB")], ["AAA", "BBB"], rules=("ra", "rb"))
    s = {c["symbol"]: c["score"] for c in b["long"]}
    assert s["AAA"] != s["BBB"]
    assert s == {"AAA": round(0.10 * 500 / 600, 4), "BBB": round(0.05 * 500 / 600, 4)}
    assert [c["symbol"] for c in b["long"]] == ["AAA", "BBB"]


def test_small_sample_shrunk():
    small, big = sl.shrink(0.2, 10, 100), sl.shrink(0.2, 1000, 100)
    assert small == pytest.approx(0.2 * 10 / 110) == pytest.approx(0.018182, abs=1e-6)
    assert big == pytest.approx(0.2 * 1000 / 1100) == pytest.approx(0.181818, abs=1e-6)
    assert small < big / 9.9                                                    # n=10 的分不到 n=1000 的 1/10


def test_k_configurable_k0_is_plain_sum(tmp_path):
    assert sl.card_score([(0.1, 10), (0.25, 1000)], k=0) == pytest.approx(0.35)
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({"ledger_k": 0}))
    assert sl.ledger_k(tmp_path) == 0.0
    (tmp_path / "rules" / "swing.json").write_text(json.dumps({}))
    assert sl.ledger_k(tmp_path) == sl.DEFAULT_K
    lg = _ledger({("ra", "long"): (10, 0.10, sl.ACTIVE), ("rb", "long"): (1000, 0.25, sl.ACTIVE)}, k=0)
    b = _board(lg, [_sig("ra", "AAA"), _sig("rb", "AAA")], ["AAA"], rules=("ra", "rb"))
    assert b["long"][0]["score"] == pytest.approx(0.35)


def test_score_is_primary_key_over_hit_count():
    """排序主键是卡分, 不是命中条数: 1 条强规则的卡排在 2 条弱规则的卡前面。"""
    lg = _ledger({("ra", "long"): (800, 0.20, sl.ACTIVE), ("rb", "long"): (800, 0.02, sl.ACTIVE),
                  ("rc", "long"): (800, 0.03, sl.ACTIVE)})
    b = _board(lg, [_sig("ra", "ONE"), _sig("rb", "TWO"), _sig("rc", "TWO")], ["ONE", "TWO"])
    assert [(c["symbol"], c["hit_count"]) for c in b["long"]] == [("ONE", 1), ("TWO", 2)]


# ── 5. 冷冻不打分; 观察档卡带 ledger_tier=observe 且不进下单簿 ──────────────────

@pytest.mark.parametrize("tier", [sl.FROZEN, sl.PENDING])
def test_frozen_or_pending_rule_not_scored_and_not_a_hit(tier):
    lg = _ledger({("ra", "long"): (500, 0.10, sl.ACTIVE), ("rb", "long"): (500, 0.40, tier)})
    b = _board(lg, [_sig("ra", "AAA"), _sig("rb", "AAA"), _sig("rb", "FRZ")], ["AAA", "FRZ"], rules=("ra", "rb"))
    assert [c["symbol"] for c in b["long"]] == ["AAA"]
    a = b["long"][0]
    assert a["hit_count"] == 1 and [s["id"] for s in a["strategies"]] == ["ra"]
    assert a["score"] == round(0.10 * 500 / 600, 4)
    assert {"symbol": "FRZ", "side": "long", "reason": "ledger_inactive"} in b["meta"]["skipped"]


def test_rank_factor_does_not_decide_card_tier():
    """rank_* + 观察档 → 仍是观察 (不能借 rank 进簿); 只有 rank_* → 无账本档位, 不额外限制。"""
    lg = _ledger({("rb", "long"): (500, 0.05, sl.OBSERVE)})
    sigs = [{**_sig("rank_x", "MIX"), "rank_pos": 1}, _sig("rb", "MIX"), {**_sig("rank_x", "RNK"), "rank_pos": 2}]
    by = {c["symbol"]: c for c in _board(lg, sigs, ["MIX", "RNK"], rules=("rank_x", "rb"))["long"]}
    assert by["MIX"]["ledger_tier"] == "observe" and by["MIX"]["grade"] == "observe"
    assert by["RNK"]["ledger_tier"] is None and by["RNK"].get("grade") != "observe"


def test_observe_tier_card_marked():
    lg = _ledger({("ra", "long"): (500, 0.10, sl.ACTIVE), ("rb", "long"): (500, 0.05, sl.OBSERVE)})
    b = _board(lg, [_sig("ra", "AAA"), _sig("rb", "AAA"), _sig("rb", "OBS")], ["AAA", "OBS"], rules=("ra", "rb"))
    by = {c["symbol"]: c for c in b["long"]}
    assert by["AAA"]["ledger_tier"] == "active" and "grade" not in by["AAA"]
    assert by["OBS"]["ledger_tier"] == "observe" and by["OBS"]["grade"] == "observe"


def test_board_reads_ledger_and_observe_cards_not_booked(tmp_path, monkeypatch, cal):  # noqa: F811
    """真读点: board_for_day → 月度复核 (落审计) → 出卡; 观察档出的卡纸面下单簿不收, 冷冻档不出卡。"""
    stats = {"cells": {"macd_golden|long": {"n": 200, "excess": 0.05, "ci_low": -0.02, "ci_high": 0.12},
                       "tv_halftrend|short": {"n": 164, "excess": -0.1, "ci_low": -0.2, "ci_high": -0.01}},
             "window": ["2023-01-03", "2026-10-30"], "data_end": "2026-11-09"}
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: stats)
    monkeypatch.setattr(sl, "_today", lambda: SIG_DAY)
    watch = _setup(tmp_path, monkeypatch, regime_state="震荡", spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert b["meta"]["ledger"]["cells"]["macd_golden|long"]["tier"] == sl.OBSERVE
    assert [c["ledger_tier"] for c in b["long"]] == ["observe"] * 3
    assert sl.audit_path(tmp_path).exists()
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []
    assert {"symbol": "L1", "side": "long", "reason": "observe_only"} in built["skipped"]
    assert built["ledger"]["cells"]["macd_golden|long"]["tier"] == sl.OBSERVE   # 簿冻结本日账本切片
    # 回滚开关: swing.json "ledger": false → 不按档位过滤, 三张卡都进簿
    p = tmp_path / "rules" / "swing.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "ledger": False}))
    assert sd.board_for_day(tmp_path, SIG_DAY, repo=object())["meta"]["ledger"]["status"] == sl.STATUS_DISABLED
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert {c["symbol"] for c in built["cards"]} == {"L1", "L2", "L3"}


# ── 6. 同分 tie-break 可复现 ──────────────────────────────────────────────────

def test_tie_break_reproducible():
    lg = _ledger({("ra", "long"): (500, 0.10, sl.ACTIVE)})
    syms = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    sigs = [_sig("ra", s) for s in syms]
    a = _board(lg, sigs, syms, rules=("ra",))
    b = _board(lg, list(reversed(sigs)), list(reversed(syms)), rules=("ra",))
    order = [c["symbol"] for c in a["long"]]
    assert order == [c["symbol"] for c in b["long"]] and len(order) == sd.MAX_PER_SIDE
    assert order == sorted(syms, key=lambda s: sd.tie_key(DAY, s))[:sd.MAX_PER_SIDE]
    assert len({c["score"] for c in a["long"]}) == 1                             # 全同分, 由 hash-v1 定先后


def test_pending_rule_gives_no_card_full_chain(tmp_path, monkeypatch, cal):  # noqa: F811
    """待定档 (n < 30) 规则: board_for_day 全链路不出卡, 下单簿为空; 冷冻条件没满足 (档位是待定不是冷冻)。"""
    stats = {"cells": {"macd_golden|long": {"n": 29, "excess": 0.3, "ci_low": -0.1, "ci_high": 0.7}},
             "window": None, "data_end": None}
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: stats)
    monkeypatch.setattr(sl, "_today", lambda: SIG_DAY)
    watch = _setup(tmp_path, monkeypatch, regime_state="进攻", spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert b["meta"]["ledger"]["cells"]["macd_golden|long"]["tier"] == sl.PENDING
    assert b["long"] == []
    assert {"symbol": "L1", "side": "long", "reason": "ledger_inactive"} in b["meta"]["skipped"]
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []


def test_reconcile_skips_pre_ledger_book(tmp_path, monkeypatch, cal):  # noqa: F811
    """过渡窗口: P1-B1 之前落的簿没有 ledger 键 → 对账跳过板层, 不报 book_card_not_recomputed。"""
    from app.services import swing_settlement as ss
    stats = {"cells": {"macd_golden|long": {"n": 200, "excess": 0.1, "ci_low": 0.01, "ci_high": 0.2}},
             "window": None, "data_end": None}
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: stats)
    monkeypatch.setattr(sl, "_today", lambda: SIG_DAY)     # P1-B2: 无当月复核 = fail-closed, 这里要有账本才出卡
    watch = _setup(tmp_path, monkeypatch, regime_state=None, spy_ok=True)
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert "ledger" in built and built["cards"]
    wb.save_day(tmp_path, built)
    p = wb.book_path(tmp_path, SIG_DAY)
    raw = json.loads(p.read_text())
    sp = wv.signals_dir(tmp_path) / f"{SIG_DAY.isoformat()}.json"
    snap = json.loads(sp.read_text())
    sp.write_text(json.dumps({**snap, "signals": []}))        # 重算出不了簿里的卡 (新簿会报 not_recomputed)
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, notify=False, replay=lambda d, syms, rules: {})
    assert out.get("board_check") is None
    assert any(x["kind"] == "book_card_not_recomputed" for x in out["issues"])
    raw.pop("ledger")                                         # 旧簿 (P1-B1 之前落的): 没有 ledger 键
    p.write_text(json.dumps(raw))
    out = ss.reconcile(tmp_path, object(), day=SIG_DAY, notify=False, replay=lambda d, syms, rules: {})
    assert out.get("board_check") == "skipped_pre_ledger_book"
    assert not any(x["kind"] == "book_card_not_recomputed" for x in out["issues"])
