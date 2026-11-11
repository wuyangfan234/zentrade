/**
 * 徽章只有四类, 全站同一套样式 (字号 11px、圆角 4px、行高 20px):
 *   status 状态 — 淡底无边框: 盘前 / 盘中、偏多 / 偏空、正常、普涨
 *   action 动作 — 实色: 下单
 *   limit  限定 — 灰描边: 仅观察、模拟、策略命中、4h 趋势过滤
 *   warn   警示 — 琥珀描边: 非当日、财报、不下单、背离
 * 颜色语义: 蓝 = 状态 / 动作, 红绿 = 涨跌方向, 琥珀只表示警示。
 */
export type BadgeKind = 'status' | 'action' | 'limit' | 'warn'
export type BadgeTone = 'accent' | 'bull' | 'bear' | 'neutral' | 'ok'

const BASE = 'inline-flex shrink-0 items-center whitespace-nowrap rounded-[4px] px-1.5 text-[11px] font-medium leading-5'

const STATUS_TONE: Record<BadgeTone, string> = {
  accent: 'bg-accent/10 text-accent dark:text-blue-400',
  bull: 'bg-bull/10 text-bull dark:text-red-400',
  bear: 'bg-bear/10 text-emerald-800 dark:text-bear',
  neutral: 'bg-elevated text-secondary',
  ok: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
}

const KIND: Record<Exclude<BadgeKind, 'status'>, string> = {
  // 白字压彩色底: 暗色用 -700, 保证对比度
  action: 'bg-accent text-white dark:bg-blue-700',
  limit: 'border border-border text-secondary',
  warn: 'border border-warning/60 text-warning',
}

/** 徽章 className。status 可带色调 (默认 accent)。 */
export function badge(kind: BadgeKind, tone: BadgeTone = 'accent'): string {
  return `${BASE} ${kind === 'status' ? STATUS_TONE[tone] : KIND[kind]}`
}
