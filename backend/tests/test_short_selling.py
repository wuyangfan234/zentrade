"""做空 (2026-10-03): 卡/台账/交易员/复盘/统一纪律重放的空头镜像。手算样例, 每个数字都能用纸笔核对。"""
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.ledger.core import Account, Ledger
from app.market_clock import us_clock
from app.opportunity.card import CardState, OpportunityCard
from app.review.review import build_round_trips
from app.services import decision_desk as dd
from app.services import strategy_evaluation as se
from app.trader.trader import SwingTrader


def _short_card(**kw):
    base = dict(id="opp-2026-10-02-XYZ-short", symbol="XYZ", setup_type="strategy:phantom_4h_short",
                created_date=date(2026, 10, 2), entry_zone=(99.0, 100.0), invalidation=104.0,
                target_zone=(88.0, 89.76), risk_per_share=4.5, reward_risk=2.3, side="short", state=CardState.ACTIVE)
    base.update(kw)
    return OpportunityCard(**base)


# ── 卡 ───────────────────────────────────────────────────────

def test_short_card_validation_mirrors_long():
    assert _short_card().validate(2.0) == []
    assert "做空 invalidation 必须高于入场区上沿" in _short_card(invalidation=99.5).validate(2.0)
    assert "做空 target_zone 必须低于入场区下沿" in _short_card(target_zone=(98.0, 99.5)).validate(2.0)
    old = _short_card().to_dict()
    old.pop("side")
    assert OpportunityCard.from_dict(old).side == "long"  # 旧卡无 side 字段 = 做多
    assert OpportunityCard.from_dict(_short_card().to_dict()).is_short


def test_card_from_pick_short_levels():
    n = 70
    c = np.linspace(120, 100, n)
    b = pl.DataFrame({"date": [date(2026, 6, 1) + timedelta(days=i) for i in range(n)], "open": c, "high": c + 1.0,
                      "low": c - 1.0, "close": c, "volume": 1e6})
    card = dd.card_from_pick("phantom_4h_short", "XYZ", b, as_of=date(2026, 10, 2), stop_loss=0.03, side="short")
    assert card is not None and card.side == "short"
    lo, hi = card.entry_zone
    assert (lo, hi) == (99.0, 100.0)
    mid = 99.5
    # 2ATR 失效 (ATR≈2.29 → 104.08) 与策略止损 +3% (102.485) 取较紧 (较低)
    assert card.invalidation == pytest.approx(mid * 1.03, abs=1e-3)
    risk = card.invalidation - mid
    # 目标 = min(60 日最低 99.0, 中值 − 2R) → 这里 2R 投影更远
    assert card.target_zone[0] == pytest.approx(min(float(b["low"].tail(60).min()), mid - 2 * risk), abs=1e-3)
    assert card.target_zone[1] == pytest.approx(card.target_zone[0] * 1.02, abs=1e-3)
    assert card.validate(2.0) == []


# ── 台账 ─────────────────────────────────────────────────────

def test_ledger_short_cash_nav_risk_and_cover(tmp_path):
    led = Ledger(data_dir=tmp_path)
    led.init_account(Account())
    led.append("fill", {"symbol": "XYZ", "side": "short", "qty": 100, "price": 100.0, "fee": 1.0,
                        "trade_date": "2026-10-05", "card_id": "c1", "stop_price": 104.0}, at="2026-10-05T10:00:00-04:00")
    pos = led.position("XYZ")
    assert pos.qty == -100 and pos.side == "short" and pos.avg_cost == pytest.approx(99.99)
    assert led.cash == pytest.approx(100_000 + 10_000 - 1)
    # 价格跌到 95: 净值 = 现金 − 回补负债 = 109999 − 9500 = 100499
    assert led.nav({"XYZ": 95.0}) == pytest.approx(100_499.0)
    # 风险 = 100 × (104 − 99.99)
    assert led.open_risk() == pytest.approx(100 * (104 - 99.99))
    # 可用资金 = 现金 − 2×空头名义 = 109999 − 2×10000 (按均价估) ≈ 89997
    assert led.buying_power() == pytest.approx(109_999 - 2 * 100 * 99.99)
    with pytest.raises(ValueError):
        led.append("fill", {"symbol": "XYZ", "side": "buy", "qty": 10, "price": 95.0, "fee": 1.0}, at="2026-10-05T11:00:00-04:00")
    led.append("partial_close", {"symbol": "XYZ", "qty": 50, "price": 95.0, "fee": 1.0, "card_id": "c1",
                                 "reason": "target_zone_lower"}, at="2026-10-06T10:00:00-04:00")
    assert led.position("XYZ").qty == -50 and led.cash == pytest.approx(109_999 - 4_751)
    led.append("close", {"symbol": "XYZ", "qty": 50, "price": 90.0, "fee": 1.0, "card_id": "c1", "reason": "target"},
               at="2026-10-07T10:00:00-04:00")
    assert led.position("XYZ") is None
    # 收益 = 卖空 10000 − 回补 4750 − 4500 − 费 3 = 747
    assert led.cash == pytest.approx(100_747.0)
    trips = build_round_trips(led.read_events())
    assert len(trips) == 1 and trips[0].pnl == pytest.approx(747.0)
    assert trips[0].avg_entry == pytest.approx(99.99) and trips[0].avg_exit == pytest.approx((4751 + 4501) / 100)


def test_ledger_replay_reproduces_short_state(tmp_path):
    led = Ledger(data_dir=tmp_path)
    led.init_account(Account())
    led.append("fill", {"symbol": "XYZ", "side": "short", "qty": 30, "price": 50.0, "fee": 1.0, "trade_date": "2026-10-05",
                        "card_id": "c1", "stop_price": 52.0}, at="2026-10-05T10:00:00-04:00")
    led.append("corp_action", {"symbol": "XYZ", "action_id": "d1", "cash_dividend": -15.0, "trade_date": "2026-10-06"},
               at="2026-10-06T09:30:00-04:00")
    again = Ledger(data_dir=tmp_path, read_only=True)
    again.replay(Account(), persist=False)
    assert again.position("XYZ").qty == -30 and again.cash == pytest.approx(100_000 + 1_500 - 1 - 15)


# ── 交易员 ───────────────────────────────────────────────────

def _rig(tmp_path, monkeypatch):
    monkeypatch.setattr(us_clock, "_calendar_provider", None)
    led = Ledger(data_dir=tmp_path)
    led.init_account(Account())
    tr = SwingTrader(led, Account())
    return led, tr


def _t(h, m, d=5):
    return datetime(2026, 10, d, h, m, tzinfo=us_clock.tz)


def test_trader_short_entry_targets_and_stop(tmp_path, monkeypatch):
    led, tr = _rig(tmp_path, monkeypatch)
    card = _short_card()
    tr.register_card(card)
    assert tr.try_fill_entry(card, price=99.5, trade_date=date(2026, 10, 5), at=_t(10, 0).isoformat()) is None  # 首帧只记意向
    fill = tr.try_fill_entry(card, price=99.4, trade_date=date(2026, 10, 5), at=_t(10, 1).isoformat())
    assert fill["side"] == "short" and fill["price"] == pytest.approx(round(99.4 * (1 - 0.0005), 4))
    pos = led.position("XYZ")
    assert pos.qty < 0
    # 1R 浮盈 → 止损下移到保本 (只下移)
    ev = tr.manage_intraday("XYZ", price=pos.avg_cost - 4.6, trade_date=date(2026, 10, 5), at=_t(11, 0).isoformat())
    assert any(e["kind"] == "stop_move" and e["stop_price"] == pytest.approx(round(pos.avg_cost, 4)) for e in ev)
    # 触及目标近端 89.76 → 回补一半; 触及远端 88 → 全部回补
    ev = tr.manage_intraday("XYZ", price=89.7, trade_date=date(2026, 10, 5), at=_t(12, 0).isoformat())
    assert ev[0]["kind"] == "partial_close" and ev[0]["price"] == pytest.approx(round(89.7 * 1.0005, 4))
    ev = tr.manage_intraday("XYZ", price=87.9, trade_date=date(2026, 10, 5), at=_t(13, 0).isoformat())
    assert ev[0]["kind"] == "close" and ev[0]["r_multiple"] > 2
    assert led.position("XYZ") is None


def test_trader_short_close_above_invalidation_exits_next_open(tmp_path, monkeypatch):
    led, tr = _rig(tmp_path, monkeypatch)
    card = _short_card()
    tr.register_card(card)
    tr.try_fill_entry(card, price=99.5, trade_date=date(2026, 10, 5), at=_t(10, 0).isoformat())
    tr.try_fill_entry(card, price=99.4, trade_date=date(2026, 10, 5), at=_t(10, 1).isoformat())
    assert tr.manage_close("XYZ", close_price=103.9, trade_date=date(2026, 10, 5), at=_t(16, 0).isoformat()) == []
    ev = tr.manage_close("XYZ", close_price=104.2, trade_date=date(2026, 10, 6), at=_t(16, 0, 6).isoformat())
    assert ev[0]["kind"] == "exit_intent" and ev[0]["reason"] == "invalidation"
    out = tr.execute_pending_exit("XYZ", open_price=105.0, trade_date=date(2026, 10, 7), at=_t(9, 31, 7).isoformat())
    assert out["kind"] == "close" and out["price"] == pytest.approx(round(105.0 * 1.0005, 4)) and out["r_multiple"] < -1


def test_trader_short_needs_double_buying_power(tmp_path, monkeypatch):
    """卖空所得留作抵押且再押同额自有资金: 成交前可用资金须 ≥ 1×新空单名义 (成交后可用资金 = 原值 − 名义)。
    权益 10 万中 8 万在多头, 现金 2 万; 新空单名义 ≈2.49 万 (25% 上限) > 2 万 → 拒绝。"""
    led, tr = _rig(tmp_path, monkeypatch)
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 800, "price": 100.0, "fee": 0.0, "trade_date": "2026-10-02",
                        "card_id": "cA", "stop_price": 99.0}, at=_t(10, 0, 2).isoformat())
    assert led.cash == 20_000
    card = _short_card(risk_per_share=0.5, invalidation=100.5, target_zone=(97.0, 98.0), reward_risk=3.0)
    tr.register_card(card)
    tr.try_fill_entry(card, price=99.5, trade_date=date(2026, 10, 5), at=_t(10, 0).isoformat())
    # 2026-10-05 起现金不足按剩余可用资金缩量 (产品决策)。约束不变: 空单名义 + 佣金 ≤ 成交前可用资金 2 万,
    # 成交后可用资金 (现金 − 2×空头名义) 不为负。
    fill = tr.try_fill_entry(card, price=99.4, trade_date=date(2026, 10, 5), at=_t(10, 1).isoformat())
    assert fill is not None and fill["side"] == "short" and fill["sizing"]["reason"] == "cash_downsized"
    assert fill["qty"] * fill["price"] + fill["fee"] <= 20_000 + 1e-6
    assert fill["sizing"]["planned_qty"] * fill["price"] > 20_000          # 满额会超出 → 确实是缩量
    assert led.buying_power({"XYZ": fill["price"], "AAA": 100.0}) >= -1e-6


def test_trader_short_blocked_when_cash_covers_under_quarter_of_plan(tmp_path, monkeypatch):
    led, tr = _rig(tmp_path, monkeypatch)
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 950, "price": 100.0, "fee": 0.0, "trade_date": "2026-10-02",
                        "card_id": "cA", "stop_price": 99.0}, at=_t(10, 0, 2).isoformat())
    assert led.cash == 5_000                                                   # 满额约 2.49 万, 5 千 < 25%
    card = _short_card(risk_per_share=0.5, invalidation=100.5, target_zone=(97.0, 98.0), reward_risk=3.0)
    tr.register_card(card)
    tr.try_fill_entry(card, price=99.5, trade_date=date(2026, 10, 5), at=_t(10, 0).isoformat())
    assert tr.try_fill_entry(card, price=99.4, trade_date=date(2026, 10, 5), at=_t(10, 1).isoformat()) is None
    blocked = [e for e in led.read_events() if e["kind"] == "entry_intent" and e.get("status") == "blocked"]
    assert blocked and blocked[-1]["reason"] == "cash"


# ── 统一 7 日纪律重放 ────────────────────────────────────────

def test_simulate_short_hand_computed():
    # 次日开 99.5 (区内) 卖空 → 第 2 天最低 89 触及近端 89.76 → 按 89.76 回补
    o = np.array([99.5, 97.0, 95.0])
    h = np.array([100.0, 98.0, 96.0])
    lo = np.array([98.0, 89.0, 94.0])
    c = np.array([98.5, 90.0, 95.0])
    r = se.simulate_7d(o, h, lo, c, entry_zone=(99.0, 100.0), invalidation=104.0, target=(88.0, 89.76), risk=4.5, side="short")
    entry, exit_ = 99.5 * (1 - se.SLIP), 89.76 * (1 + se.SLIP)
    assert r["status"] == "closed" and r["exit_reason"] == "target_zone_lower" and r["days"] == 2
    assert r["r"] == pytest.approx(round((entry - exit_) / 4.5 - se.FEE_R(4.5, entry), 4))


def test_simulate_short_gap_down_not_chased_and_stop_next_open():
    gap = se.simulate_7d(np.array([96.0]), np.array([97.0]), np.array([95.0]), np.array([96.0]),
                         entry_zone=(99.0, 100.0), invalidation=104.0, target=(88.0, 89.76), risk=4.5, side="short")
    assert gap["status"] == "gap_expired"
    o = np.array([99.5, 103.0, 106.0])
    h = np.array([100.0, 105.0, 107.0])
    lo = np.array([99.0, 102.0, 105.0])
    c = np.array([99.8, 104.5, 106.0])
    r = se.simulate_7d(o, h, lo, c, entry_zone=(99.0, 100.0), invalidation=104.0, target=(88.0, 89.76), risk=4.5, side="short")
    assert r["exit_reason"] == "invalidation" and r["days"] == 3 and r["exit_price"] == pytest.approx(106.0 * (1 + se.SLIP), abs=1e-4)


def test_short_buying_power_after_fill_is_never_negative(tmp_path, monkeypatch):
    led, tr = _rig(tmp_path, monkeypatch)
    led.append("fill", {"symbol": "AAA", "side": "buy", "qty": 700, "price": 100.0, "fee": 0.0, "trade_date": "2026-10-02",
                        "card_id": "cA", "stop_price": 99.0}, at=_t(10, 0, 2).isoformat())
    card = _short_card(risk_per_share=0.5, invalidation=100.5, target_zone=(97.0, 98.0), reward_risk=3.0)
    tr.register_card(card)
    tr.try_fill_entry(card, price=99.5, trade_date=date(2026, 10, 5), at=_t(10, 0).isoformat())
    fill = tr.try_fill_entry(card, price=99.4, trade_date=date(2026, 10, 5), at=_t(10, 1).isoformat())
    assert fill is not None and fill["side"] == "short"
    assert led.buying_power() >= 0


def test_dividend_sign_must_match_position_side(tmp_path):
    led = Ledger(data_dir=tmp_path)
    led.init_account(Account())
    led.append("fill", {"symbol": "XYZ", "side": "short", "qty": 30, "price": 50.0, "fee": 1.0, "trade_date": "2026-10-05",
                        "card_id": "c1", "stop_price": 52.0}, at="2026-10-05T10:00:00-04:00")
    with pytest.raises(ValueError):  # 空头不会“收到”分红
        led.append("corp_action", {"symbol": "XYZ", "action_id": "d0", "cash_dividend": 15.0}, at="2026-10-06T09:30:00-04:00")
    led.append("corp_action", {"symbol": "XYZ", "action_id": "d1", "cash_dividend": -15.0}, at="2026-10-06T09:30:00-04:00")
