/** 机会卡 P1-B2 字段的卡面呈现: 时效标记 (T0 跨日有效 / T1 盘中切换 + 锚定价) + 观察级徽章 + 可展开的逐条归因。
 * 字段契约以 docs/P1-B2-CARD-ATTRIBUTION.md 为准; 归因逐条列出参与打分的规则, 不只给总分。 */
import { useState } from 'react'
import type { SwingBoardCard } from '@/lib/api'
import { badge } from '@/components/ui/badge'
import { toast } from '@/components/Toast'
import { anchorText, fR, gateText, isObserve, TIER_EN_ZH } from '@/components/opportunities/p1'

export const OBSERVE_HINT = '观察级不进纸面下单簿'

export function TimingTags({ c }: { c: SwingBoardCard }) {
  const obs = isObserve(c)
  return (
    <div data-card-timing={c.timing_tier ?? ''} className="mt-1 flex min-h-[22px] flex-wrap items-center gap-1.5">
      {c.timing_tier === 'T1' ? <>
        <span data-timing="T1" className={badge('warn')} title={c.t1 ? `${c.t1.from ?? ''} → ${c.t1.to ?? ''} · 有效至 ${c.t1.valid_until ?? '—'}` : undefined}>⚡ 盘中切换</span>
        <span data-anchor="" className="whitespace-nowrap text-[12px] tabular-nums text-secondary">{anchorText(c.t1)}</span>
      </> : c.timing_tier === 'T0' ? <span data-timing="T0" className={badge('status')}>跨日有效</span> : null}
      {obs && <span data-observe-badge="" className={badge('limit')}>{c.timing_tier === 'T1' ? '观察级·不进纸面下单' : '观察级'}</span>}
      {c.headwind && <span data-headwind="" className={badge('warn')} title="盘中切换后这个方向上限为 0 · 未成交卡排到后面">逆风</span>}
    </div>
  )
}

/** 观察级卡的下单按钮: 点了只提示, 不进下单链 */
export function ObserveOrderButton({ symbol }: { symbol: string }) {
  return (
    <button type="button" data-observe-order={symbol} title={OBSERVE_HINT}
      onClick={(e) => { e.stopPropagation(); toast(`${symbol} · ${OBSERVE_HINT}`, 'error') }}
      onKeyDown={(e) => e.stopPropagation()}
      className="inline-flex h-6 shrink-0 items-center rounded-btn border border-dashed border-secondary/70 px-2 text-[12px] font-medium text-secondary hover:text-foreground">
      下单
    </button>
  )
}

export function AttrToggle({ c, names }: { c: SwingBoardCard; names: Map<string, string> }) {
  const [open, setOpen] = useState(false)
  const sc = c.score_components
  const has = !!sc || !!c.gate_id || c.regime_state !== undefined
  if (!has) return null
  return (
    <div className="mt-1.5" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <button type="button" data-attr-toggle={c.symbol} aria-expanded={open} onClick={() => setOpen((v) => !v)}
        className="text-[12px] font-medium text-accent hover:underline dark:text-blue-400">{open ? '归因 ▾' : `归因 ▸ ${sc ? `${sc.rules.length} 条规则` : ''}`}</button>
      {open && (
        <div data-attr-panel={c.symbol} className="mt-1 rounded-btn border border-border bg-base/60 px-2 py-1.5 text-[12px]">
          {sc && sc.rules.length > 0 ? (
            <table className="w-full table-fixed tabular-nums">
              <thead><tr className="text-[11px] text-muted">
                <th className="text-left font-medium">规则</th><th className="w-[52px] text-left font-medium">档位</th>
                <th className="w-[38px] text-right font-medium">n</th><th className="w-[56px] text-right font-medium">超额</th><th className="w-[58px] text-right font-medium">收缩分</th>
              </tr></thead>
              <tbody>
                {sc.rules.map((r) => (
                  <tr key={r.strategy} data-attr-rule={r.strategy}>
                    <td className="truncate pr-1 text-foreground" title={r.strategy}>{names.get(`${r.strategy}|${c.side}`) ?? names.get(r.strategy) ?? r.strategy}</td>
                    <td className="text-secondary">{TIER_EN_ZH[r.tier] ?? r.tier}</td>
                    <td className="text-right text-secondary">{r.n}</td>
                    <td className="text-right text-foreground">{fR(r.excess)}</td>
                    <td className="text-right font-semibold text-foreground">{fR(r.shrunk)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="text-muted">无参与打分的规则</div>}
          {sc && <div data-attr-total="" className="mt-1 border-t border-border/60 pt-1 text-secondary">
            {`合计 ${fR(sc.total)} · k = ${sc.k} · 收缩分 = 超额 × n / (n + k)`}</div>}
          <div className="mt-0.5 text-secondary">{`市场状态 ${c.regime_state ?? '未提供'}`}</div>
          <div className="mt-0.5 break-all text-secondary" title={c.gate_id}>{`放行门 ${gateText(c.gate_id)}`}</div>
        </div>
      )}
    </div>
  )
}
