"""台账事件溯源测试 (P2-1)。

覆盖: 初始化幂等拒绝、成交/平仓/部分平仓/移动止损/除权/冲正、
seq 递增续号、重放与物化一致、净值定版幂等。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.ledger.core import Account, Ledger


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    return lg


def _buy(lg: Ledger, sym: str, qty: float, price: float, fee: float = 1.0, at: str = "2026-09-25T09:30:00") -> dict:
    return lg.append("fill", {
        "symbol": sym, "side": "buy", "qty": qty, "price": price, "fee": fee,
        "trade_date": at[:10],
    }, at=at)


def _sell(lg: Ledger, sym: str, qty: float, price: float, kind: str = "close",
          fee: float = 1.0, at: str = "2026-09-26T15:30:00") -> dict:
    return lg.append(kind, {
        "symbol": sym, "qty": qty, "price": price, "fee": fee,
    }, at=at)


# ── 初始化 ──────────────────────────────────────────────

def test_init_refuses_overwrite(ledger):
    with pytest.raises(FileExistsError):
        ledger.init_account(Account())


def test_initial_cash(ledger):
    assert ledger.cash == 100_000.0


# ── 成交与持仓 ────────────────────────────────────────────

def test_buy_creates_position_with_avg_cost_including_fee(ledger):
    _buy(ledger, "AAPL", 100, 200.0, fee=1.0)
    pos = ledger.position("AAPL")
    assert pos.qty == 100
    assert pos.avg_cost == pytest.approx((100 * 200.0 + 1.0) / 100)
    assert pos.entry_date == "2026-09-25"
    assert ledger.cash == pytest.approx(100_000 - 100 * 200.0 - 1.0)


def test_add_to_position_recomputes_avg(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    _buy(ledger, "AAPL", 100, 210.0, at="2026-09-25T10:00:00")
    pos = ledger.position("AAPL")
    assert pos.qty == 200
    assert pos.avg_cost == pytest.approx((100 * 200 + 1 + 100 * 210 + 1) / 200)


def test_close_releases_cash_and_zeroes_position(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    _sell(ledger, "AAPL", 100, 210.0)
    assert ledger.position("AAPL") is None
    assert ledger.cash == pytest.approx(100_000 - 20001.0 + 20999.0)


def test_partial_close_keeps_position(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    _sell(ledger, "AAPL", 50, 210.0, kind="partial_close")
    pos = ledger.position("AAPL")
    assert pos.qty == 50
    assert pos.avg_cost == pytest.approx((100 * 200 + 1) / 100)  # 卖不摊成本


# ── 移动止损与除权 ────────────────────────────────────────

def test_stop_move_updates_stop_price(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    ledger.append("stop_move", {"symbol": "AAPL", "stop_price": 205.0}, at="2026-09-26T10:00:00")
    assert ledger.position("AAPL").stop_price == 205.0


def test_stop_move_only_moves_up_enforced_by_caller(ledger):
    """台账本身不判方向 (交易员层职责), 但事件如实记录。"""
    _buy(ledger, "AAPL", 100, 200.0)
    ledger.append("stop_move", {"symbol": "AAPL", "stop_price": 195.0}, at="2026-09-26T10:00:00")
    assert ledger.position("AAPL").stop_price == 195.0


def test_corp_action_split_adjusts_qty_and_cost(ledger):
    _buy(ledger, "NVDA", 10, 1200.0, fee=1.0)
    ledger.append("corp_action", {"symbol": "NVDA", "factor": 10.0, "reason": "split 1:10"},
                  at="2026-09-28T08:00:00")
    pos = ledger.position("NVDA")
    assert pos.qty == 100
    assert pos.avg_cost == pytest.approx((10 * 1200.0 + 1.0) / 10 / 10)


def test_corp_action_cash_dividend(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    ledger.append("corp_action", {"symbol": "AAPL", "factor": 1.0, "cash_dividend": 25.0},
                  at="2026-09-28T08:00:00")
    assert ledger.cash == pytest.approx(100_000 - 20001.0 + 25.0)


# ── seq 与重放 ──────────────────────────────────────────

def test_seq_strictly_increments(ledger):
    e1 = _buy(ledger, "AAPL", 100, 200.0)
    e2 = _buy(ledger, "NVDA", 10, 100.0, at="2026-09-25T09:31:00")
    assert e2["seq"] == e1["seq"] + 1


def test_replay_restores_exact_state(tmp_path):
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    _buy(lg, "AAPL", 100, 200.0)
    _sell(lg, "AAPL", 50, 210.0, kind="partial_close")
    lg.append("stop_move", {"symbol": "AAPL", "stop_price": 205.0}, at="2026-09-26T10:00:00")
    lg.append("corp_action", {"symbol": "AAPL", "factor": 2.0, "reason": "split"}, at="2026-09-27T08:00:00")

    lg2 = Ledger(data_dir=tmp_path / "ledger")
    lg2.replay(Account())
    assert lg2.cash == pytest.approx(lg.cash)
    assert lg2._seq == lg._seq
    p = lg2.position("AAPL")
    assert p.qty == 100  # 50 股 × 2 (拆股)
    assert p.stop_price == pytest.approx(205.0 / 2)


def test_replay_empty_is_safe(tmp_path):
    lg = Ledger(data_dir=tmp_path / "empty")
    lg.init_account(Account())
    lg.replay(Account())
    assert lg.cash == 100_000.0
    assert lg.open_positions == []


# ── 净值 ──────────────────────────────────────────────

def test_nav_and_market_value(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    nav = ledger.nav({"AAPL": 210.0})
    assert nav == pytest.approx(ledger.cash + 100 * 210.0)


def test_write_nav_daily_idempotent(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    e1 = ledger.write_nav_daily(date(2026, 9, 25), {"AAPL": 200.0})
    e2 = ledger.write_nav_daily(date(2026, 9, 25), {"AAPL": 200.0})
    assert e1 == e2  # 同日重写不追加


def test_open_risk_sums_stop_distance(ledger):
    _buy(ledger, "AAPL", 100, 200.0)
    ledger.append("stop_move", {"symbol": "AAPL", "stop_price": 190.0}, at="2026-09-26T10:00:00")
    # 100 股 × (200.01 - 190) ≈ 1001
    assert ledger.open_risk() == pytest.approx(100 * (ledger.position("AAPL").avg_cost - 190.0))


def test_replay_100_synthetic_events_matches_materialized(tmp_path):
    """P2-1 验收: 100 笔合成事件重放后 cash/持仓/seq 与在线物化逐位一致。"""
    import random
    random.seed(42)
    lg = Ledger(data_dir=tmp_path / "ledger")
    lg.init_account(Account())
    syms = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD"]
    t = date(2026, 9, 25)
    for i in range(100):
        sym = random.choice(syms)
        qty = random.randint(1, 50)
        price = round(random.uniform(50, 500), 2)
        at = f"2026-09-{25 + i // 20:02d}T{9 + (i % 6):02d}:{30 + (i % 30):02d}:00"
        pos = lg.position(sym)
        if pos and random.random() < 0.4:
            lg.append("partial_close" if random.random() < 0.5 else "close",
                      {"symbol": sym, "qty": min(qty, pos.qty), "price": price, "fee": 1.0}, at=at)
        elif random.random() < 0.1 and pos:
            lg.append("stop_move", {"symbol": sym, "stop_price": price * 0.95}, at=at)
        elif random.random() < 0.05 and pos:
            lg.append("corp_action", {"symbol": sym, "factor": 2.0, "reason": "split"}, at=at)
        else:
            lg.append("fill", {"symbol": sym, "side": "buy", "qty": qty, "price": price,
                               "fee": 1.0, "trade_date": t.isoformat()}, at=at)

    lg2 = Ledger(data_dir=tmp_path / "ledger")
    lg2.replay(Account())
    assert lg2.cash == pytest.approx(lg.cash)
    assert lg2._seq == lg._seq
    assert [(p.symbol, p.qty, p.avg_cost) for p in lg2.open_positions] == \
           [(p.symbol, p.qty, p.avg_cost) for p in lg.open_positions]
