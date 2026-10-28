import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type SettlementCell, type SettlementPayload } from '@/lib/api'
import { SettlementCards } from './SettlementCards'

/**
 * 卡结局 (方向 A, 2026-10-11): 自选机会卡的结局统计, 只读 GET /api/opportunities/settlement。
 * 能成交 = 按规则入场价 (开盘跳空作废的 PASS 不计) / 信号准 = PASS 按跳空开盘价虚拟入场计入。
 * live = 只统计盘后扫描当时写出的卡 / 全部 = 含补算。表内只有数字、码、—。
 * 着色: n < 20 灰且数字为 — ; 20 ≤ n < 30 只显数不着色; n ≥ 30 且超额区间不跨 0 才着色。
 */
type Kind = 'fill' | 'signal'
type Scope = 'live' | 'all'

const f2 = (n: number | null | undefined) => (n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(2)}`)
const p0 = (n: number | null | undefined) => (n == null ? '—' : `${Math.round(n * 100)}%`)
const TONE: Record<SettlementCell['tone'], string> = { gray: 'text-muted', plain: 'text-foreground', pos: 'text-bull', neg: 'text-bear' }

function Row({ c, head }: { c: SettlementCell; head: string }) {
  const g = c.tone === 'gray'
  return (
    <tr data-cell={head} data-tone={c.tone} className={`border-b border-border/50 ${TONE[c.tone]}`}>
      <td className="py-1.5 pr-3 text-left">{head}</td>
      <td className="pr-3 text-right">{c.n}</td>
      <td className="pr-3 text-right">{g ? '—' : p0(c.trigger_rate)}</td>
      <td className="pr-3 text-right">{c.exp}</td>
      <td className="pr-3 text-right">{c.pass}</td>
      <td className="pr-3 text-right">{g ? '—' : p0(c.win_rate)}</td>
      <td className="pr-3 text-right">{g ? '—' : f2(c.mean_r)}</td>
      <td className="pr-3 text-right">{g ? '—' : f2(c.mean_excess_r)}</td>
      <td className="pr-3 text-right">{g || !c.ci ? '—' : `${f2(c.ci[0])} ~ ${f2(c.ci[1])}`}</td>
      <td className="pr-3 text-right">{g ? '—' : f2(c.worst_r)}</td>
      <td className="pr-3 text-right">{c.same_bar}</td>
      <td className="pr-3 text-right">{c.unsettled}</td>
      <td className="text-right">{c.missing}</td>
    </tr>
  )
}

function Seg<T extends string>({ name, opts, v, set }: { name: string; opts: [T, string][]; v: T; set: (x: T) => void }) {
  return (
    <div data-seg={name} role="tablist" className="inline-flex rounded-btn border border-border bg-elevated p-0.5 text-xs">
      {opts.map(([id, label]) => (
        <button key={id} type="button" role="tab" aria-selected={v === id} data-seg-opt={id} onClick={() => set(id)}
          className={`rounded-[5px] px-3 py-1 ${v === id ? 'bg-surface font-medium text-foreground shadow-sm' : 'text-secondary hover:text-foreground'}`}>{label}</button>
      ))}
    </div>
  )
}

export function EvidenceSettlement() {
  const q = useQuery<SettlementPayload>({ queryKey: ['opportunities', 'settlement'], queryFn: () => api.cardSettlement(), refetchInterval: 300_000 })
  const [kind, setKind] = useState<Kind>('fill')
  const [scope, setScope] = useState<Scope>('live')
  const d = q.data
  if (q.isError) return <div data-settle-error className="py-6 text-sm text-danger">{(q.error as Error)?.message || '—'}</div>
  if (!d) return <div className="py-6 text-sm text-muted">…</div>
  const t = d.available && d.tables ? d.tables[kind][scope] : null
  return (
    <div data-settlement className="text-xs tabular-nums">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Seg name="kind" opts={[['fill', '能成交'], ['signal', '信号准']]} v={kind} set={setKind} />
        <Seg name="scope" opts={[['live', 'live'], ['all', '全部']]} v={scope} set={setScope} />
        <span data-settle-meta className="ml-auto text-muted">{`${d.contract} · ${d.data_asof ?? '—'} · m ${t?.m ?? 0}`}</span>
      </div>
      {!t ? (
        <div data-settle-empty className="rounded-card border border-dashed border-border py-6 text-center text-muted">—</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap">
            <thead className="text-muted">
              <tr className="border-b border-border">
                {['规则', 'n', '触发率', 'EXP', 'PASS', '胜率', '均R', '均超额R', '超额95%', '最差R', '同根', '未结', '缺数'].map((h, i) => (
                  <th key={h} className={`py-1.5 pr-3 font-normal ${i === 0 ? 'text-left' : 'text-right'}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {t.cells.map((c) => <Row key={`${c.rule}|${c.side}`} c={c} head={`${c.rule} ${c.side === 'short' ? '空' : '多'}`} />)}
            </tbody>
            <tfoot className="border-t border-border">
              <Row c={t.all} head="全部" />
            </tfoot>
          </table>
        </div>
      )}
      <SettlementCards />
    </div>
  )
}
