"""波段纸面交易员 (trader) — P2-3。

职责边界 (docs/architecture.md): 只消费机会卡, 永不自己选股。管入场/持仓管理/出场三件事。
账户与钱归 ledger 模块, 交易员只发事件。

模块:
- sizing.py: 仓位计算 (风险预算 ÷ 每股风险, 名义额封顶 25%)
- executor.py: 入场撮合 (入场区内下一快照价 + 滑点; 跳空越过即弃)
- manager.py: 持仓管理 (失效点收盘破/目标区分批/移动止损/时间止损 7 日硬约束)
"""
from app.trader.trader import SwingTrader

__all__ = ["SwingTrader"]
