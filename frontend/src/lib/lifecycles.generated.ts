// 本文件由 scripts/gen_lifecycles.py 生成, 不要手改。
// 来源: backend/app/lifecycles.py (状态机登记表)。改状态后重新生成并提交。

export const LIFECYCLE_LABELS = {
  "opportunity_card": {
    "candidate": "初筛候选",
    "open": "待入场",
    "active": "监控中",
    "watch": "观察",
    "filled": "已入场",
    "closed": "已平仓",
    "expired": "已失效",
    "rejected": "已否决"
  },
  "mirror_order": {
    "new": "待提交",
    "submitting": "提交中",
    "submission_unknown": "提交结果待确认",
    "submitted": "已提交",
    "filled": "已成交",
    "rejected": "被拒",
    "alpaca_canceled": "Alpaca 已撤单",
    "alpaca_expired": "Alpaca 已过期",
    "alpaca_rejected": "Alpaca 拒单",
    "skipped_not_mirrored": "未镜像（开启前的持仓）",
    "skipped_internal_exit": "未提交（内部已平仓）",
    "superseded_by_final_close": "已并入全平",
    "skipped_zero_qty": "数量为 0",
    "blocked_daily_cap": "超过每日上限",
    "expired_out_of_session": "超时未提交"
  },
  "notification_outbox": {
    "pending": "待发送",
    "unconfigured": "未配置",
    "failed": "失败",
    "sent": "成功"
  },
  "factor_intake_item": {
    "queued": "排队中",
    "to_extract": "排队中",
    "to_screen": "快筛中",
    "pending": "待审",
    "merged": "已并入",
    "archived": "已归档"
  },
  "pipeline_job": {
    "pending": "排队中",
    "running": "运行中",
    "succeeded": "已完成",
    "failed": "失败"
  },
  "mining_run": {
    "queued": "排队中",
    "running": "运行中",
    "cancelling": "取消中",
    "succeeded": "已完成",
    "succeeded_with_budget_exhausted": "已完成（预算用尽）",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "被中断",
    "skipped_prerequisite": "前置条件不足 · 已跳过"
  }
} as const

export const LIFECYCLE_TERMINAL = {
  "opportunity_card": [
    "closed",
    "expired",
    "rejected"
  ],
  "mirror_order": [
    "filled",
    "rejected",
    "alpaca_canceled",
    "alpaca_expired",
    "alpaca_rejected",
    "skipped_not_mirrored",
    "skipped_internal_exit",
    "superseded_by_final_close",
    "skipped_zero_qty",
    "blocked_daily_cap",
    "expired_out_of_session"
  ],
  "notification_outbox": [
    "sent"
  ],
  "factor_intake_item": [
    "merged"
  ],
  "pipeline_job": [
    "succeeded",
    "failed"
  ],
  "mining_run": [
    "succeeded",
    "succeeded_with_budget_exhausted",
    "failed",
    "cancelled",
    "interrupted",
    "skipped_prerequisite"
  ]
} as const

export type LifecycleId = keyof typeof LIFECYCLE_LABELS

/** 状态中文名; 登记表里没有的代码原样返回。 */
export function lifecycleLabel(id: LifecycleId, code: string): string {
  return (LIFECYCLE_LABELS[id] as Record<string, string>)[code] ?? code
}
