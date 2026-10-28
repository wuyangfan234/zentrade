"""OpportunityCard — 完整、不可变的交易提案 (docs/architecture.md)。

freeze 语义: 卡一旦生成, 入场区/失效点/目标区/时间边界全部不可变。
失效点是初始止损 (永不改卡); 持仓期的移动止损是交易员层的订单动作。

方向 (2026-10-03 无限要求支持做空): side = "long" (默认, 旧卡无此字段即做多) | "short"。
做空卡的价位镜像: 失效点在入场区上方、目标区在入场区下方。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date
from enum import StrEnum


class CardState(StrEnum):
    CANDIDATE = "candidate"   # 管线初筛产出
    OPEN = "open"             # 通过组合层约束, 待次日入场
    ACTIVE = "active"         # 开盘仍有效, 监控入场区
    WATCH = "watch"           # 组合约束降级 (只展示不开仓)
    FILLED = "filled"         # 已入场 (移交交易员)
    CLOSED = "closed"         # 已平仓
    EXPIRED = "expired"       # 入场窗内未触价/跳空越过
    REJECTED = "rejected"     # 评估否决 (带理由)


TERMINAL_STATES = frozenset({CardState.CLOSED, CardState.EXPIRED, CardState.REJECTED})


@dataclass(frozen=True)
class OpportunityCard:
    id: str
    symbol: str
    setup_type: str                    # breakout | pullback
    created_date: date                 # 生成日 (美东交易日)

    # 交易提案 (缺一不可成卡)
    entry_zone: tuple[float, float]
    invalidation: float
    target_zone: tuple[float, float]
    max_holding_days: int = 7          # 自成交日起算

    # 风险预算 (生成时冻结)
    risk_per_share: float = 0.0
    reward_risk: float = 0.0

    # 证据链 (生成时刻数据快照, 可回放审计)
    evidence: dict = field(default_factory=dict)
    narrative: str = ""                # LLM 生成的人话解释 (仅展示, 不参与决策)

    # 生命周期
    state: CardState = CardState.CANDIDATE
    state_reason: str = ""

    side: str = "long"                 # long | short

    @property
    def is_short(self) -> bool:
        return self.side == "short"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_date"] = self.created_date.isoformat()
        d["state"] = str(self.state)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OpportunityCard":
        d = dict(d)
        d["created_date"] = date.fromisoformat(d["created_date"])
        d["state"] = CardState(d["state"])
        d["entry_zone"] = tuple(d["entry_zone"])
        d["target_zone"] = tuple(d["target_zone"])
        return cls(**d)

    def with_state(self, state: CardState, reason: str = "") -> "OpportunityCard":
        """状态转换产新卡 (frozen 不改自身)。"""
        from dataclasses import replace
        return replace(self, state=state, state_reason=reason)

    @staticmethod
    def make_id(symbol: str, setup_type: str, created: date, revision: int) -> str:
        """确定性 ID: 同日复卡去重。revision 参与以区分参数版本。"""
        raw = f"{symbol}|{setup_type}|{created.isoformat()}|r{revision}"
        digest = hashlib.sha1(raw.encode()).hexdigest()[:8]
        return f"opp-{created.isoformat()}-{symbol}-{setup_type}-{digest}"

    def validate(self, min_rr: float) -> list[str]:
        """成卡门槛校验。返回违规列表 (空=合法)。"""
        errs = []
        lo, hi = self.entry_zone
        if self.side not in ("long", "short"):
            errs.append("side 必须是 long 或 short")
        if not (lo > 0 and hi >= lo):
            errs.append("entry_zone 非法")
        tlo, thi = self.target_zone
        if self.is_short:
            # 做空: 失效点高于入场区上沿; 目标区低于入场区下沿, 近端 (先触及) 是 thi
            if not (self.invalidation > hi):
                errs.append("做空 invalidation 必须高于入场区上沿")
            if not (0 < tlo <= thi < lo):
                errs.append("做空 target_zone 必须低于入场区下沿")
        else:
            if not (0 < self.invalidation < lo):
                errs.append("invalidation 必须低于入场区下沿")
            if not (thi >= tlo > hi):
                errs.append("target_zone 必须高于入场区上沿")
        if self.reward_risk < min_rr:
            errs.append(f"reward_risk {self.reward_risk:.2f} < {min_rr}")
        if self.risk_per_share <= 0:
            errs.append("risk_per_share 必须为正")
        if self.max_holding_days < 1:
            errs.append("max_holding_days 必须 >=1")
        return errs
