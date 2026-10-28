"""ZenTrade 机会引擎 (opportunity) — P2-2。

核心创新 (docs/architecture.md): 系统的最小产出单位是 OpportunityCard — 一张完整、
不可变的交易提案。信号只是卡的触发依据字段, 而非全部。

模块边界:
- card.py: OpportunityCard 不可变结构 + 状态机
- setups/: setup 识别纯函数 (零 IO, 可回放)
- scanner.py: 全市场扫描管线 (流动性过滤 → setup → 提案构造 → 组合约束)
- engine.py: 每日管线编排 (市场状态门 + LLM 叙事钩子)

铁律:
- 引擎是纯函数: scan(bars_panel, params, as_of) — 生产与回测吃同一函数
- 参数即数据: 一切阈值来自 config/setups.json (带 version/revision), 代码零魔数
- 时间锚定: as_of = 美东交易日; 函数只消费 as_of 及之前的 bars (回放安全)
"""
from app.opportunity.card import OpportunityCard, CardState

__all__ = ["OpportunityCard", "CardState"]
