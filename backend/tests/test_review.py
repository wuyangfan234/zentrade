"""复盘模块单测 (P2-5)。合成事件流 → 回合重建 → 分组统计 → 周报。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.ledger.core import Account, Ledger
from app.review.review import build_round_trips, setup_stats, weekly_report


def _trade(lg: Ledger, sym: str, card_id: str, setup: str, qty: float,
           entry: float, exit_: float, day0: date, day1: date,
           rps: float = 6.0, reason: str = "target") -> None:
    lg.append("fill", {"symbol": sym, "side": "buy", "qty": qty, "price": entry,
                       "fee": 1.0, "trade_date": day0.isoformat(),
                       "card_id": card_id, "setup_type": setup, "risk_per_share": rps},
              at=f"{day0.isoformat()}T10:00:00")
    lg.append("close", {"symbol": sym, "qty": qty, "price": exit_, "fee": 1.0,
                        "trade_date": day1.isoformat(), "card_id": card_id,
                        "reason": reason}, at=f"{day1.isoformat()}T16:30:00")


@pytest.fixture
def ledger_with_trades(tmp_path):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    d0, d1 = date(2026, 9, 22), date(2026, 9, 25)
    # 两盈一亏 (突破 2 笔, 回踩 1 笔)
    _trade(lg, "AAPL", "c1", "breakout", 100, 100.0, 110.0, d0, d1)
    _trade(lg, "NVDA", "c2", "breakout", 50, 200.0, 230.0, d0, d1)
    _trade(lg, "AMD", "c3", "pullback", 200, 50.0, 48.0, d0, d1, reason="invalidation")
    return lg


def test_round_trips_fifo_pairing(ledger_with_trades):
    trips = build_round_trips(ledger_with_trades.read_events())
    assert len(trips) == 3
    by_sym = {t.symbol: t for t in trips}
    assert by_sym["AAPL"].pnl == pytest.approx(100 * 110 - 1 - 100 * 100 - 1)
    assert by_sym["AMD"].r_multiple < 0
    assert by_sym["AMD"].exit_reason == "invalidation"


def test_r_multiple_uses_planned_risk(ledger_with_trades):
    trips = build_round_trips(ledger_with_trades.read_events())
    aapl = next(t for t in trips if t.symbol == "AAPL")
    # 计划风险 = 100 股 × $6 = $600; pnl ≈ +$998 → R ≈ 1.66
    assert aapl.planned_risk == 600.0
    assert aapl.r_multiple == pytest.approx(aapl.pnl / 600.0, rel=1e-2)


def test_setup_stats_groups(ledger_with_trades):
    trips = build_round_trips(ledger_with_trades.read_events())
    stats = setup_stats(trips)
    by = {s.setup_type: s for s in stats}
    assert by["breakout"].count == 2
    assert by["breakout"].wins == 2
    assert by["pullback"].count == 1
    assert by["pullback"].wins == 0


def test_open_position_not_counted(tmp_path):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    lg.append("fill", {"symbol": "AAPL", "side": "buy", "qty": 100, "price": 100.0,
                       "fee": 1.0, "trade_date": "2026-09-25", "card_id": "c9",
                       "setup_type": "breakout", "risk_per_share": 6.0},
              at="2026-09-25T10:00:00")
    trips = build_round_trips(lg.read_events())
    assert trips == []  # 未平不入回合


def test_weekly_report_shape(ledger_with_trades, tmp_path):
    rep = weekly_report(tmp_path, date(2026, 9, 27))
    assert rep["trips"] == 3
    assert len(rep["by_setup"]) == 2
    assert rep["week_end"] == "2026-09-27"


def test_degrade_candidate_rule(ledger_with_trades, tmp_path):
    """亏钱的 setup 且样本≥5 才进降级名单 (这里样本不足, 不进)。"""
    rep = weekly_report(tmp_path, date(2026, 9, 27))
    assert rep["degrade_candidates"] == []


def test_mood_breakdown_groups_by_entry_day_and_skips_backfill(tmp_path):
    """按开仓日盘前情绪 / 实际走势分组; 回补回合不计 (2026-10-12)。"""
    import json as _json
    from datetime import date as _d

    from app.review.review import RoundTrip, mood_breakdown
    j = tmp_path / "market_mood" / "judgments"
    j.mkdir(parents=True)
    (j / "2026-10-12.json").write_text(_json.dumps({"day": "2026-10-12", "stance": "range"}), encoding="utf-8")
    (tmp_path / "market_mood" / "scores.jsonl").write_text(
        _json.dumps({"day": "2026-10-12", "outcome": "down"}) + "\n", encoding="utf-8")

    def trip(day, pnl, r, backfill=False):
        return RoundTrip("X", None, "breakout", day, day, 1, 1, 1, pnl, 1, r, "stop", backfill=backfill)
    trips = [trip(_d(2026, 10, 12), 10, 1.0), trip(_d(2026, 10, 12), -5, -0.5),
             trip(_d(2026, 10, 13), 3, None), trip(_d(2026, 10, 9), 99, 9.0, backfill=True)]
    out = mood_breakdown(tmp_path, trips)
    st = {r["key"]: r for r in out["by_stance"]}
    assert st["range"]["count"] == 2 and st["range"]["wins"] == 1 and st["range"]["avg_r"] == 0.25
    assert st["none"]["count"] == 1 and st["none"]["avg_r"] is None
    assert {r["key"]: r["count"] for r in out["by_outcome"]} == {"down": 2, "none": 1}
    assert sum(r["count"] for r in out["by_stance"]) == 3            # 回补那笔不计
