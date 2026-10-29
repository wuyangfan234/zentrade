"""仓位计算 (docs/architecture.md 确认参数)。

固定分数法: 股数 = (权益 × 单笔风险%) ÷ 每股风险 (入场中值 - 失效点)
名义额封顶: 权益 × 25% (防失效点过近导致的巨型仓位)
费用: 每股 $0.005 最低 $1 (台账 Account 同口径)
"""
from __future__ import annotations

from app.ledger.core import Account
from app.opportunity.card import OpportunityCard


def size_position(card: OpportunityCard, equity: float, account: Account) -> int:
    """返回股数 (0 = 不可开)。"""
    if card.risk_per_share <= 0 or equity <= 0:
        return 0
    risk_budget = equity * account.risk_per_trade_pct
    qty = int(risk_budget / card.risk_per_share)
    # 名义额封顶: 权益 × 25%
    entry_mid = sum(card.entry_zone) / 2
    max_qty_by_notional = int(equity * 0.25 / entry_mid) if entry_mid > 0 else 0
    qty = min(qty, max_qty_by_notional)
    return max(qty, 0)


# 现金不足时按剩余可用资金缩量成交 (2026-10-05 产品决策)。缩量后计划风险低于原计划的这个比例就不成交:
# 碎仓占用持仓名额与一张卡的唯一回合, 却几乎不产生可学习的结果。25% 与探索单 (0.25% vs 1%) 同比例。
MIN_CASH_DOWNSIZE_RATIO = 0.25


def downsize_for_cash(qty: int, price: float, available: float, account: Account) -> int:
    """在可用资金内能买到的最大股数 (含佣金)。qty 为原计划股数; 返回 0 表示连 1 股都买不起。"""
    if qty <= 0 or price <= 0 or not available > 0:
        return 0
    n = min(qty, int(max(available - account.min_commission, 0) // price))
    while n > 0 and n * price + fee_for(n, account) > available + 1e-9:
        n -= 1
    return max(n, 0)


def fee_for(qty: int, account: Account) -> float:
    """每股佣金, 最低 $1。"""
    return round(max(qty * account.commission_per_share, account.min_commission), 2)
