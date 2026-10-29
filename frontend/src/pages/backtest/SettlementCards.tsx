import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type SettlementCardRow, type SettlementCardsPayload } from '@/lib/api'

/**
 * 已结算卡逐张明细 (0.18.0, 2026-10-14 产品决策): 只读 GET /api/opportunities/settlement/cards
 * = cards.zt-settle-v1.jsonl 原样逐行; 按规则 / 退出原因在前端筛选, 不改任何结算口径。
 */
const f2 = (n: number | null | undefined) => (n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(2)}`)
const tone = (n: number | null | undefined) => (n == null || n === 0 ? 'text-secondary' : n > 0 ? 'text-bull' : 'text-bear')

/**
 * pass_signal 行 (卡未成交, 只按信号口径结算) 的 R/离场日/退出原因写在 signal_* 字段
 * (services/card_settlement.py _SIGNAL_KEYS), 其余行读原字段
 */
export function rowView(r: SettlementCardRow) {
  const sig = r.record_kind === 'pass_signal'
  return {
    exit_date: sig ? r.signal_exit_date : r.exit_date,
    exit_reason: sig ? r.signal_exit_reason : r.exit_reason,
    r: sig ? r.signal_r : r.r,
    excess_r: sig ? r.signal_excess_r : r.excess_r,
  }
}

/** 行 key: 同一张卡可同时有 pass_void 与 pass_signal 两条记录, key 须带 record_kind 才唯一 */
export const rowKey = (r: SettlementCardRow) => `${r.key}|${r.record_kind ?? ''}`

function Pick({ name, opts, v, set }: { name: string; opts: string[]; v: string; set: (x: string) => void }) {
  return (
    <select data-filter={name} value={v} onChange={(e) => set(e.target.value)}
      className="rounded-btn border border-border bg-elevated px-2 py-1 text-xs">
      <option value="">{name === 'rule' ? '全部规则' : '全部退出原因'}</option>
      {opts.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

export function SettlementCards() {
  const q = useQuery<SettlementCardsPayload>({ queryKey: ['opportunities', 'settlement', 'cards'], queryFn: () => api.settlementCards(), refetchInterval: 300_000 })
  const [rule, setRule] = useState('')
  const [reason, setReason] = useState('')
  const d = q.data
  if (q.isError) return <div data-cards-error className="py-4 text-sm text-danger">{(q.error as Error)?.message || '—'}</div>
  if (!d) return null
  if (!d.available || !d.rows.length) return <div data-cards-empty className="mt-4 rounded-card border border-dashed border-border py-6 text-center text-muted">—</div>
  const rows = d.rows.filter((r) => (!rule || r.rule === rule) && (!reason || rowView(r).exit_reason === reason))
  return (
    <div data-settlement-cards className="mt-6">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">逐卡明细</span>
        <Pick name="rule" opts={d.rules} v={rule} set={setRule} />
        <Pick name="exit_reason" opts={d.exit_reasons} v={reason} set={setReason} />
        <span data-cards-count className="ml-auto text-muted">{`${rows.length} / ${d.total}`}</span>
      </div>
      <div className="max-h-[480px] overflow-auto">
        <table className="w-full whitespace-nowrap">
          <thead className="text-muted">
            <tr className="border-b border-border">
              {['信号日', '票', '方向', '规则', '结局', '记录', '成交日', '离场日', '天', '退出原因', 'R', '超额R', '来源'].map((h, i) => (
                <th key={h} className={`py-1.5 pr-3 font-normal ${i >= 8 && i <= 11 && i !== 9 ? 'text-right' : 'text-left'}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => { const v = rowView(r); return (
              <tr key={rowKey(r)} data-card-row={r.card_id} className="border-b border-border/50">
                <td className="py-1 pr-3">{r.signal_day ?? '—'}</td>
                <td className="pr-3">{r.symbol ?? '—'}</td>
                <td className="pr-3">{r.side === 'short' ? '空' : '多'}</td>
                <td className="pr-3">{r.rule ?? '—'}</td>
                <td className="pr-3">{r.outcome ?? '—'}</td>
                <td className="pr-3 text-muted">{r.record_kind ?? '—'}</td>
                <td className="pr-3">{r.fill_date ?? '—'}</td>
                <td className="pr-3">{v.exit_date ?? '—'}</td>
                <td className="pr-3 text-right">{r.hold_days ?? '—'}</td>
                <td className="pr-3">{v.exit_reason ?? '—'}</td>
                <td className={`pr-3 text-right ${tone(v.r)}`}>{f2(v.r)}</td>
                <td className={`pr-3 text-right ${tone(v.excess_r)}`}>{f2(v.excess_r)}</td>
                <td className="text-muted">{r.provenance ?? '—'}</td>
              </tr>
            ) })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
