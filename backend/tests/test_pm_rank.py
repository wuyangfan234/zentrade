"""盘前定稿 + 同向放量排前 (2026-10-21 19:40 SGT 产品决策)。只重排已出的卡: 不新增、不作废、不改几何/仓位/结算。"""
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services import pm_rank as pr

D = date(2026, 10, 21)


def test_ratio_matches_replay_definition():
    # 回放口径: 04:00-09:00 盘前累计量 / 前 20 个交易日同窗均量, 有效基线日 >= 10
    assert pr.vol_ratio(300.0, [100.0] * 20) == pytest.approx(3.0)
    assert pr.vol_ratio(300.0, [100.0] * 9 + [0.0] * 11) is None      # 有效日不足 10
    assert pr.vol_ratio(0.0, [100.0] * 20) is None
    assert pr.vol_ratio(300.0, [100.0] * 25) == pytest.approx(3.0)    # 只取最近 20 日


def test_gap_atr_sign_by_side():
    assert pr.gap_atr("long", 102.0, 100.0, 4.0) == pytest.approx(0.5)
    assert pr.gap_atr("short", 102.0, 100.0, 4.0) == pytest.approx(-0.5)
    assert pr.gap_atr("long", None, 100.0, 4.0) is None
    assert pr.gap_atr("long", 102.0, 100.0, 0.0) is None


def test_priority_rule():
    assert pr.is_priority(2.1, 0.1) is True
    assert pr.is_priority(2.0, 0.1) is False          # 回放: > 2
    assert pr.is_priority(3.0, -0.1) is False         # 逆向
    assert pr.is_priority(None, 0.5) is False         # 缺数据 = 原序
    assert pr.is_priority(3.0, None) is False


def _write(tmp: Path, cards: dict):
    p = pr.path(tmp, D)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": D.isoformat(), "contract": pr.CONTRACT, "cards": cards}), encoding="utf-8")


def test_reorder_only_permutes(tmp_path):
    cards = [{"card_id": f"c{i}", "symbol": f"S{i}", "side": "long"} for i in range(5)]
    _write(tmp_path, {"c3": {"priority": True}, "c1": {"priority": False}})
    out = pr.reorder(cards, pr.priority_ids(tmp_path, D))
    assert [c["card_id"] for c in out] == ["c3", "c0", "c1", "c2", "c4"]
    assert sorted(map(id, out)) == sorted(map(id, cards))          # 不增减、同一批对象
    assert all(set(c) <= {"card_id", "symbol", "side"} for c in out)  # 不改字段


def test_missing_file_keeps_order(tmp_path):
    cards = [{"card_id": "a"}, {"card_id": "b"}]
    assert pr.priority_ids(tmp_path, D) == set()
    assert [c["card_id"] for c in pr.reorder(cards, set())] == ["a", "b"]


def test_corrupt_file_keeps_order(tmp_path):
    p = pr.path(tmp_path, D)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{bad", encoding="utf-8")
    assert pr.priority_ids(tmp_path, D) == set()


def test_compute_and_write_idempotent(tmp_path):
    book = [{"id": "c1", "symbol": "AAA", "side": "long", "evidence": {"ref_close": 100.0, "atr14": 4.0}},
            {"id": "c2", "symbol": "BBB", "side": "short", "evidence": {"ref_close": 50.0, "atr14": 2.0}}]
    pre = {"AAA": (103.0, 3000.0), "BBB": (51.0, 5000.0), "SPY": (701.0, 1.0)}
    base = {"AAA": [1000.0] * 20, "BBB": [1000.0] * 20}
    rec = pr.compute(book, pre=pre, base=base, spy_prev_close=700.0, news={"AAA": 2})
    assert rec["cards"]["c1"] == {"symbol": "AAA", "side": "long", "gap_atr": 0.75, "pvr": 3.0, "news_n": 2, "priority": True}
    assert rec["cards"]["c2"]["priority"] is False and rec["cards"]["c2"]["gap_atr"] == -0.5   # 空头遇高开 = 逆向
    assert rec["spy_chg"] == pytest.approx(1 / 700, rel=1e-3)
    assert pr.save(tmp_path, D, rec) is True
    assert pr.save(tmp_path, D, {**rec, "cards": {}}) is False         # 已定稿不改写
    assert pr.priority_ids(tmp_path, D) == {"c1"}


def test_display_fields_not_in_verdict_or_guard():
    """守护: 盘前定稿字段只展示/排序, 不进裁决、准入、下单簿、结算。"""
    root = Path(pr.__file__).parent
    for name in ("swing_verdict.py", "entry_guard.py", "watchlist_book.py", "swing_settlement.py", "swing_pm_check.py"):
        assert "pm_rank" not in (root / name).read_text(encoding="utf-8"), name


def test_trading_loop_uses_rank_only_for_order():
    """交易环只用 pm_rank 决定同一轮内尝试顺序 (sorted 重排), 不用它拦截或改单。"""
    src = (Path(pr.__file__).parent / "trading_loop.py").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in src.splitlines() if "pm_rank" in ln and not ln.strip().startswith("#")]
    assert lines, "交易环应接入排序"
    assert all(("import" in ln) or ("pm_rank.entry_order" in ln) for ln in lines), lines


def test_entry_order_is_permutation(tmp_path):
    class C:
        def __init__(self, i): self.id = i
    cs = [C("a"), C("b"), C("c")]
    _write(tmp_path, {"c": {"priority": True}})
    out = pr.entry_order(cs, tmp_path, D)
    assert [c.id for c in out] == ["c", "a", "b"] and set(map(id, out)) == set(map(id, cs))
    assert [c.id for c in pr.entry_order(cs, tmp_path / "none", D)] == ["a", "b", "c"]


def test_board_annotate_within_group_only():
    """机会板: 卡面带 pm 一行; 排前只在同一状态/裁决组内, 不越过持仓或不下单分组, 不增减卡。"""
    from app.services import swing_desk as sd
    cards = [{"card_id": "h", "state": "holding", "verdict": "holding"},
             {"card_id": "a", "state": "pending", "verdict": "order"},
             {"card_id": "b", "state": "pending", "verdict": "order"},
             {"card_id": "s", "state": "pending", "verdict": "skip"},
             {"card_id": "t", "state": "pending", "verdict": "skip"}]
    view = {"at": "09:20", "spy_chg": 0.001, "cards": {"b": {"gap_atr": 0.4, "pvr": 2.5, "news_n": 1, "priority": True},
                                                       "t": {"gap_atr": 0.4, "pvr": 2.5, "news_n": 0, "priority": True}}}
    out = sd.apply_pm_rank(cards, view)
    assert [c["card_id"] for c in out] == ["h", "b", "a", "t", "s"]
    assert out[1]["pm"]["pvr"] == 2.5 and "pm" not in out[2]
    assert sd.apply_pm_rank(cards, None) == cards
