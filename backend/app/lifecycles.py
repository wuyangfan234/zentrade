"""状态机登记表 — 落盘状态的唯一词汇表 (状态、中文名、终态、允许的转换)。

为什么有这个文件: 各模块的状态原先只存在于散落的字符串字面量里 (``o["status"] = "submission_unknown"``),
前端再手抄一份中文映射。两边没有共同来源, 后端加了状态前端不知道, 页面就直接露出英文代码。

这里只登记, 不接管: 各模块照旧自己写状态, 交易链行为不变。登记表提供三样东西:

1. 词汇表 — ``tests/test_lifecycles.py`` 扫各模块源码, 写出登记表之外的状态即测试失败;
2. 转换表 — ``observe()`` 只记日志, 从不拦截、从不抛错 (交易环里出现表外转换时留下证据, 不中断);
3. 同一份数据的三个出口 — ``GET /api/lifecycles`` (运行时)、``docs/state-machines.md`` (读代码前先看)、
   ``frontend/src/lib/lifecycles.generated.ts`` (页面中文名)。后两个由 ``scripts/gen_lifecycles.py`` 生成。

新增或改名一个状态: 改这里 → 跑 ``python scripts/gen_lifecycles.py`` → 提交生成物。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class State:
    code: str
    label: str            # 页面与汇报用的中文名
    terminal: bool = False
    note: str = ""        # 什么情况下进入


@dataclass(frozen=True)
class Lifecycle:
    id: str
    title: str
    owner: str                          # 写这个状态的模块 (相对 backend/)
    field: str                          # 记录里的字段名
    stored: str                         # 落盘位置 (相对 data 目录)
    states: tuple[State, ...]
    initial: tuple[str, ...]
    transitions: dict[str, tuple[str, ...]]
    enforced: bool = False              # True = 属主模块自己会拒绝表外转换; False = 只登记

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(s.code for s in self.states)

    @property
    def terminal(self) -> frozenset[str]:
        return frozenset(s.code for s in self.states if s.terminal)

    def allows(self, src: str, dst: str) -> bool:
        return dst in self.transitions.get(src, ())


def _mining_run() -> Lifecycle:
    """挖掘任务本来就有显式转换表, 直接引用, 不抄第二份。"""
    from app.services import mining_jobs as mj

    labels = {
        "queued": "排队中", "running": "运行中", "cancelling": "取消中", "succeeded": "已完成",
        "succeeded_with_budget_exhausted": "已完成（预算用尽）", "failed": "失败", "cancelled": "已取消",  # noqa: RUF001
        "interrupted": "被中断", "skipped_prerequisite": "前置条件不足 · 已跳过",
    }
    order = list(labels)
    return Lifecycle(
        id="mining_run", title="因子挖掘任务", owner="app/services/mining_jobs.py", field="status",
        stored="research/mining/runs/<run_id>/",
        states=tuple(State(c, labels[c], terminal=c in mj.TERMINAL_RUN_STATUSES) for c in order),
        initial=("queued",),
        transitions={c: tuple(sorted(mj._ALLOWED_TRANSITIONS[c], key=order.index)) for c in order
                     if mj._ALLOWED_TRANSITIONS[c]},
        enforced=True,
    )


def _notification_outbox() -> Lifecycle:
    from app.services.notification_outbox import _LABELS

    return Lifecycle(
        id="notification_outbox", title="通知发件箱", owner="app/services/notification_outbox.py", field="status",
        stored="notifications/outbox.sqlite3 (表 outbox)",
        states=(
            State("pending", _LABELS["pending"], note="已入队, 等下一次投递"),
            State("unconfigured", _LABELS["unconfigured"], note="没有可用渠道; 配好后自动回到待发送"),
            State("failed", _LABELS["failed"], note="本次投递有渠道失败; 退避后重试, 最多 5 次, 耗尽后停在这里"),
            State("sent", _LABELS["sent"], terminal=True, note="勾选的渠道全部受理"),
        ),
        initial=("pending", "unconfigured"),
        transitions={
            "pending": ("sent", "failed", "unconfigured"),
            # → sent 不经 pending: 重试时发现勾选的渠道此前都已受理
            "unconfigured": ("pending", "sent"),
            "failed": ("pending", "sent", "unconfigured"),
        },
    )


def _build() -> dict[str, Lifecycle]:
    card = Lifecycle(
        id="opportunity_card", title="机会卡", owner="app/services/opportunity_service.py", field="state",
        stored="ledger/events.jsonl (opportunity / fill / close 事件重放得出)",
        states=(
            State("candidate", "初筛候选", note="管线初筛产出, 尚未过组合约束"),
            State("open", "待入场", note="通过组合约束, 等下一交易日开盘确认"),
            State("active", "监控中", note="开盘确认有效, 价格进入场区即可成交"),
            State("watch", "观察", note="准入或组合约束拦下, 只展示不开仓; 容量恢复且价在区内可回到监控中"),
            State("filled", "已入场", note="纸面成交, 移交交易员管理"),
            State("closed", "已平仓", terminal=True),
            State("expired", "已失效", terminal=True, note="入场窗结束 / 跳空越过 / 盘前越止损 / 除权后价位失效"),
            State("rejected", "已否决", terminal=True, note="评估否决"),
        ),
        initial=("candidate",),
        transitions={
            "candidate": ("open", "watch", "rejected"),
            "open": ("active", "watch", "expired"),
            "active": ("filled", "watch", "expired"),
            "watch": ("active", "expired"),
            "filled": ("closed",),
        },
    )
    mirror = Lifecycle(
        id="mirror_order", title="Alpaca 模拟镜像订单", owner="app/services/alpaca_mirror.py", field="status",
        stored="alpaca_mirror/state.json (orders)",
        states=(
            State("new", "待提交", note="台账成交已登记, 还没发给 Alpaca"),
            State("submitting", "提交中", note="发单前先落盘的围栏; 进程在发单途中死掉也只按订单号对账, 不重发"),
            State("submission_unknown", "提交结果待确认", note="发单超时或 5xx, 不知道 Alpaca 收没收到; 只对账不重发"),
            State("submitted", "已提交", note="Alpaca 已受理, 等成交"),
            State("filled", "已成交", terminal=True),
            State("rejected", "被拒", terminal=True, note="发单时 Alpaca 明确拒绝 (4xx)"),
            State("alpaca_canceled", "Alpaca 已撤单", terminal=True),
            State("alpaca_expired", "Alpaca 已过期", terminal=True),
            State("alpaca_rejected", "Alpaca 拒单", terminal=True, note="受理后被 Alpaca 拒绝"),
            State("skipped_not_mirrored", "未镜像（开启前的持仓）", terminal=True),  # noqa: RUF001
            State("skipped_internal_exit", "未提交（内部已平仓）", terminal=True,  # noqa: RUF001
                  note="开仓单还没发出, 内部这一轮已经平仓"),
            State("superseded_by_final_close", "已并入全平", terminal=True,
                  note="减仓单还没发出, 后面的全平单一并处理"),
            State("skipped_zero_qty", "数量为 0", terminal=True),
            State("blocked_daily_cap", "超过每日上限", terminal=True, note="只拦开仓单; 平仓单等到次日不作废"),
            State("expired_out_of_session", "超时未提交", terminal=True, note="开仓单错过交易时段或提交窗口"),
        ),
        initial=("new",),
        transitions={
            # new → submitted/filled/alpaca_*: 旧版本留下的“已尝试但仍是 new”的订单按订单号对账
            "new": ("submitting", "submitted", "filled", "alpaca_canceled", "alpaca_expired", "alpaca_rejected",
                    "skipped_not_mirrored", "skipped_internal_exit", "superseded_by_final_close", "skipped_zero_qty",
                    "blocked_daily_cap", "expired_out_of_session"),
            "submitting": ("submitted", "filled", "rejected", "submission_unknown", "alpaca_canceled",
                           "alpaca_expired", "alpaca_rejected"),
            "submission_unknown": ("submitted", "filled", "alpaca_canceled", "alpaca_expired", "alpaca_rejected"),
            "submitted": ("filled", "alpaca_canceled", "alpaca_expired", "alpaca_rejected"),
        },
    )
    intake = Lifecycle(
        id="factor_intake_item", title="因子采集条目", owner="app/services/factor_intake.py", field="stage",
        stored="research/intake/items.json",
        states=(
            State("queued", "排队中", note="过了预筛, 等模型判断是否值得提取"),
            State("to_extract", "排队中", note="等模型提取公式"),
            State("to_screen", "快筛中", note="公式已提取, 等数据快筛"),
            State("pending", "待审", note="快筛完成, 等用户并入或忽略"),
            State("merged", "已并入", terminal=True, note="已进因子库 (观察)"),
            State("archived", "已归档", note="预筛丢弃 / 失败 / 重复 / 已忽略; 可恢复或重试"),
        ),
        initial=("queued", "to_extract", "archived"),
        transitions={
            "queued": ("to_extract", "archived"),
            "to_extract": ("to_screen", "archived"),
            "to_screen": ("pending", "archived"),
            "pending": ("merged", "archived"),
            "archived": ("pending", "to_screen", "to_extract"),
        },
    )
    job = Lifecycle(
        id="pipeline_job", title="数据同步任务", owner="app/services/pipeline_jobs.py", field="status",
        stored="job_store/<id>.json",
        states=(
            State("pending", "排队中"),
            State("running", "运行中"),
            State("succeeded", "已完成", terminal=True),
            State("failed", "失败", terminal=True, note="出错 / 取消 / 排队超时 / 后端重启时补录"),
        ),
        initial=("pending",),
        transitions={"pending": ("running", "failed"), "running": ("succeeded", "failed")},
    )
    return {lc.id: lc for lc in (card, mirror, _notification_outbox(), intake, job, _mining_run())}


_REGISTRY: dict[str, Lifecycle] | None = None


def registry() -> dict[str, Lifecycle]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build()
    return _REGISTRY


def get(lifecycle_id: str) -> Lifecycle:
    return registry()[lifecycle_id]


def label(lifecycle_id: str, code: str) -> str:
    """状态中文名; 登记表里没有的代码原样返回 (不抛错)。"""
    lc = registry().get(lifecycle_id)
    if lc is not None:
        for s in lc.states:
            if s.code == code:
                return s.label
    return code


_SEEN: set[tuple[str, str, str]] = set()


def observe(lifecycle_id: str, src: object, dst: object, **context: object) -> bool:
    """记录一次状态转换是否在表内。只记日志: 不拦截、不抛错, 同一种表外转换每个进程只记一次。"""
    try:
        a, b = str(src), str(dst)
        lc = registry().get(lifecycle_id)
        if lc is None or a == b or lc.allows(a, b):
            return True
        key = (lifecycle_id, a, b)
        if key not in _SEEN:
            _SEEN.add(key)
            logger.warning("lifecycle %s: transition %s -> %s is not in the registry %s", lifecycle_id, a, b, context or "")
        return False
    except Exception:  # 登记表出任何问题都不能影响调用方
        return True


def as_payload() -> dict:
    """``GET /api/lifecycles`` 与前端生成物共用的 JSON 形态。"""
    out = []
    for lc in registry().values():
        out.append({
            "id": lc.id, "title": lc.title, "owner": lc.owner, "field": lc.field, "stored": lc.stored,
            "enforced": lc.enforced, "initial": list(lc.initial),
            "states": [{"code": s.code, "label": s.label, "terminal": s.terminal, "note": s.note} for s in lc.states],
            "transitions": {src: list(dst) for src, dst in lc.transitions.items()},
        })
    return {"lifecycles": out}
