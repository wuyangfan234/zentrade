/** 0.22.0 统一组件库 — 只用 index.css 设计 token (bull/bear/warning/accent/fg-*, surface/elevated/border) 与 rounded-card/btn。 */
import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

export type Tone = 'long' | 'short' | 'watch' | 'withdrawn' | 'ref' | 'warn' | 'accent' | 'neutral'

const TONE: Record<Tone, string> = {
  long: 'border-bull/40 bg-bull/10 text-bull',
  short: 'border-bear/40 bg-bear/10 text-bear',
  watch: 'border-border bg-elevated text-secondary',
  withdrawn: 'border-border bg-elevated text-muted line-through',
  ref: 'border-dashed border-border text-muted',
  warn: 'border-warning/40 bg-warning/10 text-warning',
  accent: 'border-accent/40 bg-accent/10 text-accent',
  neutral: 'border-border text-secondary',
}
export const TONE_TEXT: Record<Tone, string> = {
  long: 'text-bull', short: 'text-bear', watch: 'text-secondary', withdrawn: 'text-muted', ref: 'text-muted',
  warn: 'text-warning', accent: 'text-accent', neutral: 'text-foreground',
}

export function Badge({ tone = 'neutral', title, children, className }: { tone?: Tone; title?: string; children: ReactNode; className?: string }) {
  return (
    <span data-badge={tone} title={title}
      className={cn('inline-flex shrink-0 items-center gap-1 rounded-btn border px-1.5 py-0.5 text-[11px] font-medium leading-4 tabular-nums', TONE[tone], className)}>
      {children}
    </span>
  )
}

export function Card({ title, right, children, className, dashed, ...rest }: {
  title?: ReactNode; right?: ReactNode; children?: ReactNode; className?: string; dashed?: boolean
} & Record<`data-${string}`, string | undefined>) {
  return (
    <section data-card="" {...rest} className={cn('rounded-card border border-border bg-surface p-3 text-xs', dashed && 'border-dashed', className)}>
      {(title || right) && (
        <header className="mb-2 flex flex-wrap items-center gap-2">
          {title && <h3 className="text-sm font-medium text-foreground">{title}</h3>}
          {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

export function StatTile({ label, value, tone = 'neutral', hint }: { label: string; value: ReactNode; tone?: Tone; hint?: string }) {
  return (
    <div data-stat={label} title={hint} className="min-w-[72px] rounded-card border border-border bg-elevated px-3 py-2">
      <div className="text-[11px] leading-4 text-muted">{label}</div>
      <div className={cn('text-lg font-semibold leading-6 tabular-nums', TONE_TEXT[tone])}>{value}</div>
    </div>
  )
}

export type Chip = { label: string; value: ReactNode; tone?: Tone; title?: string }
/** outline = 描边 (「依据」类: 策略名 / 4h / 出卡条件), 默认灰底 (「指标」类: 盘前 / PCR / IV), 两类一眼分开 */
export function ContextChips({ items, outline }: { items: Chip[]; outline?: boolean }) {
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((c) => (
        <span key={c.label} data-chip={c.label} data-chip-kind={outline ? 'basis' : 'metric'} title={c.title}
          className={cn('inline-flex items-center gap-1 rounded-btn px-1.5 py-0.5 text-[11px] leading-4',
            outline ? 'border border-border' : 'border border-transparent bg-elevated')}>
          <span className="text-muted">{c.label}</span>
          <span className={cn('font-medium tabular-nums', TONE_TEXT[c.tone ?? 'neutral'])}>{c.value}</span>
        </span>
      ))}
    </div>
  )
}

/** at: 美东墙钟 ISO (YYYY-MM-DDTHH:MM...)。 */
export function Freshness({ at, stale, label }: { at?: string | null; stale?: boolean; label?: string }) {
  if (!at) return null
  const t = `${at.slice(11, 16)} ET`
  return (
    <span data-fresh={stale ? 'stale' : 'ok'} title={`${label ?? '更新于'} ${at.slice(0, 16).replace('T', ' ')} ET`}
      className={cn('inline-flex items-center gap-1 text-[11px] leading-4 tabular-nums', stale ? 'text-warning' : 'text-muted')}>
      <span className={cn('h-1.5 w-1.5 rounded-full', stale ? 'bg-warning' : 'bg-accent')} />
      {stale ? `${t} · 延迟` : t}
    </span>
  )
}

/** 价位在 [止损, 目标] 区间的百分比位置 (0–100, 截断)。空头止损在上方同样适用。 */
export function ladderPos(v: number, stop: number, target: number): number {
  if (stop === target) return 50
  const p = ((v - stop) / (target - stop)) * 100
  return Math.round(Math.max(0, Math.min(100, p)) * 10) / 10
}

const f2 = (n: number) => n.toFixed(2)
/** 止损 — 入场 — 目标 水平条, 现价圆点。替代成段的价位文字。 */
export function PriceLadder({ side, stop, entry, target, price }: {
  side: 'long' | 'short'; stop: number; entry?: number | null; target?: number | null; price?: number | null
}) {
  const tgt = target ?? (entry != null ? entry + (entry - stop) * 2 : stop)
  const ref = price ?? entry
  const dist = ref ? (stop / ref - 1) * 100 : null
  return (
    <div data-ladder={side} className="w-full">
      <div className="relative h-1.5 rounded-full bg-elevated">
        <div className={cn('absolute inset-y-0 left-0 rounded-full', side === 'long' ? 'bg-bull/30' : 'bg-bear/30')} style={{ width: '100%' }} />
        {entry != null && <span data-ladder-entry className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 bg-accent" style={{ left: `${ladderPos(entry, stop, tgt)}%` }} />}
        {price != null && <span data-ladder-price className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface bg-foreground" style={{ left: `${ladderPos(price, stop, tgt)}%` }} />}
      </div>
      <div className="mt-1 flex justify-between text-[11px] leading-4 tabular-nums">
        <span className="text-warning">{f2(stop)}</span>
        {dist != null && <span className="text-muted">{`止损 ${dist > 0 ? '+' : ''}${dist.toFixed(1)}%`}</span>}
        {target != null && <span className={side === 'long' ? 'text-bull' : 'text-bear'}>{f2(target)}</span>}
      </div>
    </div>
  )
}
