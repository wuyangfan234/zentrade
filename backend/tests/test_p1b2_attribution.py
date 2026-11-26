"""P1-B2: 卡归因五字段 / T1 状态切换卡 / 历史月无账本 fail-closed (禁影子: 全部走 board_for_day 真读点)。"""
import json
import re
from datetime import date

import pytest

from app.market_clock import us_clock
from app.opportunity import regime as rg
from app.opportunity import strategy_ledger as sl
from app.services import swing_desk as sd
from app.services import watchlist_book as wb
from app.services import watchlist_v4 as wv
from test_regime_p1a import LIVE_DAY, SIG_DAY, _empty_repo, _hourly_state, _setup, cal  # noqa: F401

pytestmark = pytest.mark.real_strategy_ledger
A, R, D = rg.ATTACK, rg.RANGE, rg.DEFENSE
STATS = {"cells": {"macd_golden|long": {"n": 200, "excess": 0.10, "ci_low": 0.01, "ci_high": 0.20},
                   "tv_halftrend|short": {"n": 164, "excess": 0.25, "ci_low": 0.15, "ci_high": 0.35}},
         "window": ["2023-01-03", "2026-10-30"], "data_end": "2026-11-09"}
LIVE = {"AMD": 198.0, "L1": 101.0, "L2": 101.0, "L3": 101.0}


@pytest.fixture
def ledger_ok(monkeypatch):
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: STATS)
    monkeypatch.setattr(sl, "_today", lambda: SIG_DAY)


def _live(syms, d):
    assert d == LIVE_DAY                       # 现价按切换当日 (快照日的下一个交易日) 取
    return {s: v for s, v in LIVE.items() if s in syms}


# ── B. 卡归因五字段 ────────────────────────────────────────────────────────────

def test_five_attribution_fields_on_board_cards(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert len(b["long"]) == 3
    k = sl.DEFAULT_K
    for c in b["long"]:
        assert c["regime_state"] == A and isinstance(c["regime_state"], str)
        assert isinstance(c["gate_id"], str) and re.fullmatch(r"[\w.-]+\+4h\.(event|state)@tight-v1", c["gate_id"])
        assert c["ledger_tier"] in ("active", "observe", None) and c["ledger_tier"] == "active"
        assert c["timing_tier"] in sd.TIMING_TIERS and c["timing_tier"] == "T0" and "T2" not in sd.TIMING_TIERS
        sc = c["score_components"]
        assert set(sc) == {"rules", "k", "total"} and sc["k"] == k
        assert sc["rules"] == [{"strategy": "macd_golden", "n": 200, "excess": 0.10,
                                "shrunk": round(0.10 * 200 / (200 + k), 4), "tier": "active"}]
        assert sc["total"] == c["score"] == pytest.approx(sum(x["shrunk"] for x in sc["rules"]), abs=1e-4)


def test_score_components_list_every_hit_rule():
    lg = {"contract": sl.CONTRACT, "status": "ok", "month": "2026-11", "regime": R, "k": 100,
          "cells": {"ra|long": {"n": 500, "excess": 0.10, "tier": sl.ACTIVE},
                    "rb|long": {"n": 50, "excess": 0.04, "tier": sl.OBSERVE}}}
    sig = [{"symbol": "X", "rule": r, "side": "long"} for r in ("ra", "rb", "rank_q")] + [
        {"symbol": "X", "rule": "rank_q", "side": "long", "rank_pos": 1}]
    b = sd.compose_boards(signals=sig, closes={"X": 100.0}, atr={"X": 2.0}, h4_state={"X": "long"},
                          as_of=date(2026, 11, 20), long_rules=("ra", "rb", "rank_q"), short_rules=(), ledger=lg)
    sc = b["long"][0]["score_components"]
    assert [(x["strategy"], x["tier"], x["n"]) for x in sc["rules"]] == [
        ("ra", "active", 500), ("rb", "observe", 50), ("rank_q", "rank", 0)]
    assert sc["total"] == pytest.approx(round(0.10 * 500 / 600 + 0.04 * 50 / 150, 4))
    assert b["long"][0]["regime_state"] is None and b["long"][0]["gate_id"] == "unknown+4h.state@tight-v1"


# ── C. T1 状态切换卡 ───────────────────────────────────────────────────────────

def test_t1_switch_day_emits_t1_card_with_live_geometry(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    _hourly_state(tmp_path, D, LIVE_DAY)                                     # 整点重判: 进攻 → 防守
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object(), live_prices=_live)
    assert b["long"] == []                                                   # 防守态多头上限 0 (只降不升)
    (c,) = b["short"]
    assert c["symbol"] == "AMD" and c["timing_tier"] == "T1" and c["regime_state"] == D
    g = sd.geometry("short", 198.0, 4.0)                                     # 公式不变, 只把 c 换成现价
    assert (c["entry_zone"], c["stop"], c["target"]) == (g["entry_zone"], g["stop"], g["target"])
    assert c["close"] == 198.0 and c["signal_close"] == 200.0
    assert (c["entry_days"], c["hold_days"]) == (2, 5)
    assert c["t1"]["day"] == LIVE_DAY.isoformat()
    assert c["t1"]["valid_until"] == us_clock.next_trading_day(LIVE_DAY).isoformat()   # 当日 + 次日
    assert (c["t1"]["from"], c["t1"]["to"]) == (A, D)
    assert c["grade"] == "observe"                                           # 空头仍为观察级
    assert b["meta"]["t1"]["sides"] == ["short"]


@pytest.mark.parametrize("hourly", [None, A, R])
def test_no_t1_without_direction_switch(tmp_path, monkeypatch, cal, ledger_ok, hourly):  # noqa: F811
    """没有整点状态 / 同态 / 进攻→震荡 (没有新开方向) → 不出 T1 卡; 多头照常 T0。"""
    _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    if hourly:
        _hourly_state(tmp_path, hourly, LIVE_DAY)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object(), live_prices=_live)
    assert b["short"] == [] and "t1" not in b["meta"]
    assert len(b["long"]) == 3 and {c["timing_tier"] for c in b["long"]} == {"T0"}
    assert {c["close"] for c in b["long"]} == {100.0}                        # T0 用信号日收盘, 不用现价


def test_old_direction_cards_marked_headwind_book_untouched(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    watch = _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert len(built["cards"]) == 3
    wb.save_day(tmp_path, built)
    before = wb.book_path(tmp_path, SIG_DAY).read_bytes()
    _hourly_state(tmp_path, D, LIVE_DAY)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object(), live_prices=_live)
    rows = [dict(c, state="pending") for c in sd._book_only_cards(wb.load_day(tmp_path, SIG_DAY), set(), SIG_DAY)]
    rows += [dict(c, state="pending") for c in b["short"]]
    sd.mark_headwind(rows, b["meta"]["regime"])
    assert {r["symbol"] for r in rows if r.get("headwind")} == {"L1", "L2", "L3"}   # 旧方向 (多头) 未成交卡
    assert not any(r.get("headwind") for r in rows if r["side"] == "short")      # T1 新方向卡不标
    assert wb.book_path(tmp_path, SIG_DAY).read_bytes() == before                  # 不撕单、不改写落盘卡
    held = [{k: v for k, v in rows[0].items() if k != "headwind"} | {"state": "holding"}]
    assert not sd.mark_headwind(held, b["meta"]["regime"])[0].get("headwind")     # 已成交的不标
    calm = [dict(rows[0], state="pending")]
    calm[0].pop("headwind", None)
    assert not sd.mark_headwind(calm, {**b["meta"]["regime"], "source": "snapshot"})[0].get("headwind")


def test_observe_t1_card_not_in_paper_book(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    watch = _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    _hourly_state(tmp_path, D, LIVE_DAY)
    board = sd.board_for_day(tmp_path, SIG_DAY, repo=object(), live_prices=_live)
    assert board["short"][0]["timing_tier"] == "T1" and board["short"][0]["grade"] == "observe"
    monkeypatch.setattr(sd, "board_for_day", lambda *a, **k: board)       # 即使板上带着 T1 卡交给落簿
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []
    assert {"symbol": "AMD", "side": "short", "reason": "observe_only"} in built["skipped"]


def test_t1_without_live_price_fails_closed(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    _hourly_state(tmp_path, D, LIVE_DAY)
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object(), live_prices=lambda syms, d: {})
    assert b["short"] == []
    assert {"symbol": "AMD", "side": "short", "reason": "t1_no_live_price"} in b["meta"]["skipped"]


# ── D. 历史月份无复核记录 → fail-closed ────────────────────────────────────────

def test_history_month_without_review_fails_closed(tmp_path, monkeypatch, cal):  # noqa: F811
    monkeypatch.setattr(sl, "artifact_stats", lambda config_dir=None: {
        "cells": {**STATS["cells"], "trend_breakout|long": {"n": 210, "excess": -0.0056, "ci_low": -0.15,
                                                             "ci_high": 0.14}},
        "window": None, "data_end": None})
    monkeypatch.setattr(sl, "_today", lambda: date(2026, 11, 26))          # SIG_DAY 在 2026-10: 历史月, 无记录
    watch = _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    sd.save_strategies(tmp_path, ["macd_golden", "trend_breakout"], ["tv_halftrend"])
    b = sd.board_for_day(tmp_path, SIG_DAY, repo=object())
    assert b["meta"]["ledger"]["status"] == sl.STATUS_UNAVAILABLE
    assert b["long"] == [] and b["short"] == []                              # 待定档 (trend_breakout) 与其余全不出
    assert {"symbol": "L1", "side": "long", "reason": "ledger_inactive"} in b["meta"]["skipped"]
    assert not sl.audit_path(tmp_path).exists()                              # 不给历史月补写复核
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["cards"] == []
    assert built["ledger"] is not None and built["ledger"]["status"] == sl.STATUS_UNAVAILABLE   # 不冻结 None
    wb.save_day(tmp_path, built)
    raw = json.loads(wb.book_path(tmp_path, SIG_DAY).read_text())
    assert sd.ledger_for_day(tmp_path, SIG_DAY, A)["status"] == sl.STATUS_UNAVAILABLE == raw["ledger"]["status"]


def test_empty_long_default_book_still_frozen(tmp_path, monkeypatch, cal, ledger_ok):  # noqa: F811
    """多头默认可为空: 簿冻结 long=[] 后, 再改 3+3 不得让板与簿分家 (_frozen_book 按「是列表」认)。"""
    watch = _setup(tmp_path, monkeypatch, regime_state=A, spy_ok=True)
    p = tmp_path / "rules" / "swing.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "long": [], "short": ["tv_halftrend"]}))
    built = wb.build_day(tmp_path, _empty_repo(tmp_path), SIG_DAY, as_of_close=SIG_DAY, watch=watch,
                         pm=wv.permission(tmp_path))
    assert built["swing_rules"]["long"] == []
    wb.save_day(tmp_path, built)
    sd.save_strategies(tmp_path, ["macd_golden"], ["tv_halftrend"])
    assert sd.rules_for_day(tmp_path, SIG_DAY)[0] == ()                     # 旧认法 (要求非空) 会读成 macd_golden
