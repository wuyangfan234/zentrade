/**
 * 回放数字的口径标签 (2026-11-15 B 档): 这个回放数字按哪份名单、哪段窗口算的。
 * 正文只放「事后名单 46」/「名单 46」/「名单未记录」, 说明放悬停; 名单与现在的自选不同时标签后加一个点。
 * 生成规则与齿轮「来源」同一函数 (rulerLabel / rulerTitle)。
 */
import type { ReplayRuler } from '@/lib/api'
import { badge } from '@/components/ui/badge'

export function rulerLabel(r: ReplayRuler | null | undefined): string {
  if (!r) return ''
  if (r.kind === 'unrecorded') return '名单未记录'
  return `${r.kind === 'post_hoc' ? '事后名单' : '名单'} ${r.n ?? ''}`.trim()
}

export function rulerTitle(r: ReplayRuler | null | undefined): string {
  if (!r) return ''
  const win = r.window ? `回看 ${r.window[0]} → ${r.window[1]}` : ''
  const now = r.watch_now != null ? `现名单 ${r.watch_now} 只` : ''
  const head = r.kind === 'unrecorded' ? '名单未记录' : `名单 ${r.fixed} · ${r.n} 只`
  const tail = r.kind === 'post_hoc' ? '名单晚于回看起点 · 事后挑选' : ''
  const diff = r.changed ? '与现名单不同' : ''
  return [head, win, now, diff, tail].filter(Boolean).join(' · ')
}

export function RulerTag({ r, className = '' }: { r: ReplayRuler | null | undefined; className?: string }) {
  if (!r) return null
  return (
    <span data-ruler={r.kind} data-ruler-changed={r.changed ? '' : undefined} title={rulerTitle(r)}
      className={`${badge('limit')} gap-1 ${className}`}>
      {rulerLabel(r)}
      {r.changed && <i aria-hidden className="h-1 w-1 rounded-full bg-warning" />}
    </span>
  )
}
