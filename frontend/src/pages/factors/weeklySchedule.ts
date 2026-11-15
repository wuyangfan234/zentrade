/** 2026-11-15 周度挖掘时间的文字 (因子页总览 / 发现页同一函数): 开关关着时不写“下次”。 */
import type { FactorLabPage } from '@/lib/api'

type Sched = FactorLabPage['schedule'] | undefined

const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')

export function lastWeeklyText(s: Sched): string {
  const l = s?.last_weekly
  if (!l) return '最近 —'
  return `最近 ${l.week.replace(/^\d{4}-/, '')}${l.day ? `（${md(l.day)}）` : ''}`
}

/** suffix = 「盘后」/「周五 收盘后」 */
export function weeklyText(s: Sched, suffix = '盘后'): string {
  if (s && s.enabled === false) return `已停用 · ${lastWeeklyText(s)}`
  return s?.next_weekly ? `下次 ${md(s.next_weekly)} ${suffix}` : '下次 —'
}

export const RUN_NOW_ZH: Record<string, string> = { mining: '挖掘中', discover: '回测中', done: '已完成', failed: '失败' }
