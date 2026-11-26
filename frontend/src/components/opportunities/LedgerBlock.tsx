/** 机会页「策略账本」(P1-B1 五档): 按 在用 / 观察 / 待定 / 冷冻 / 复活 分组。
 * 数据 = 板 meta.ledger (strategy_ledger.ledger_for 在当前 regime 的切片), 与出卡同源 (ledger_for_day 板/簿同源)。
 * 待定 / 冷冻 = 不出卡: 除颜色外有图标 + 「不出卡」文字 + 虚线左边, 一眼可辨。
 * 区间: 后端切片现只带 n / excess / tier (ledger_view 未透传 ci_low / ci_high), 有就画, 没有就写「区间未返回」, 不编。 */
import type { SwingBoardsPayload } from '@/lib/api'
import { CiAxis, CiBar } from '@/components/opportunities/CiBar'
import { SIDE_ARROW } from '@/components/opportunities/terms'
import { ciScale, fR, LEDGER_TIERS, ledgerOf, ledgerRows, TIER_ICON, TIER_OFF } from '@/components/opportunities/p1'

const STATUS_NOTE: Record<string, string> = {
  disabled: '账本开关关闭 · 档位一律按在用, 分数取回放汇总',
  unavailable: '本月无复核记录 · 全部规则按待定处理, 今日不出卡',
}
const TIER_CHIP: Record<string, string> = {
  在用: 'bg-accent/10 text-accent dark:text-blue-300', 复活: 'bg-accent/10 text-accent dark:text-blue-300',
  观察: 'bg-elevated text-foreground', 待定: 'border border-dashed border-warning/70 text-warning', 冷冻: 'border border-dashed border-warning/70 text-warning',
}
const toneR = (v: number | null) => (v == null || Math.abs(v) < 0.005 ? 'text-foreground' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')

export function LedgerBlock({ swing, names }: { swing: SwingBoardsPayload | null | undefined; names: Map<string, string> }) {
  const l = ledgerOf(swing)
  const rows = ledgerRows(l, names)
  const sc = ciScale(rows.flatMap((r) => [r.excess, r.lo, r.hi]))
  const groups = LEDGER_TIERS.map((t) => ({ t, rows: rows.filter((r) => r.tier === t) })).filter((g) => g.rows.length > 0)
  const other = rows.filter((r) => !(LEDGER_TIERS as string[]).includes(r.tier))
  if (other.length) groups.push({ t: other[0].tier as never, rows: other })
  return (
    <section data-ledger="" className="mb-3 min-w-0 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2.5">
        <h2 className="shrink-0 text-[14px] font-semibold text-foreground">策略账本</h2>
        <span data-ledger-count={rows.length} className="text-[13px] font-semibold tabular-nums text-secondary">{rows.length}</span>
        {LEDGER_TIERS.map((t) => {
          const n = rows.filter((r) => r.tier === t).length
          return n ? <span key={t} data-ledger-tier-count={t} className={`rounded-[4px] px-1.5 text-[11px] font-medium leading-5 ${TIER_CHIP[t]}`}>{`${TIER_ICON[t]} ${t} ${n}`}</span> : null
        })}
        <span className="ml-auto text-right text-[12px] tabular-nums text-muted">
          {l ? `${l.month ?? ''} 复核 · ${l.regime ?? '—'}格 · 收缩 k=${l.k ?? '—'} · 超额 = 比 4h 门内随便挑每笔多出的 R` : ''}
        </span>
      </div>
      {!l ? <div data-ledger-empty="" className="px-3 pb-3 text-[13px] text-muted">机会板未返回账本切片</div> : <>
        {STATUS_NOTE[l.status] && <div data-ledger-status={l.status} className="mx-3 mb-2 rounded-btn border border-warning/50 bg-warning/10 px-2.5 py-1.5 text-[12px] font-semibold text-warning">{STATUS_NOTE[l.status]}</div>}
        {rows.length === 0 ? <div data-ledger-empty="" className="px-3 pb-3 text-[13px] text-muted">账本切片无规则</div> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] table-fixed text-xs" data-ledger-table="">
              <colgroup><col /><col className="w-[64px]" /><col className="w-[76px]" /><col className="w-[192px]" /><col className="w-[56px]" /><col className="w-[96px]" /></colgroup>
              <thead><tr className="border-b border-border text-[11px] text-secondary">
                <th className="py-1.5 pl-3 text-left font-medium">策略</th><th className="py-1.5 text-left font-medium">方向</th>
                <th className="py-1.5 pr-2 text-right font-medium">滚动超额</th>
                <th className="py-1.5 pr-2 text-left font-medium"><div className="flex items-center gap-1"><span>95% 区间</span></div><CiAxis min={sc.min} max={sc.max} width={176} /></th>
                <th className="py-1.5 pr-2 text-right font-medium">样本 n</th><th className="py-1.5 pr-3 text-left font-medium">档位</th>
              </tr></thead>
              {groups.map((g) => (
                <tbody key={g.t} data-ledger-group={g.t}>
                  <tr><td colSpan={6} className="bg-elevated/70 px-3 py-1 text-[11px] font-semibold text-secondary">
                    {`${TIER_ICON[g.t] ?? ''} ${g.t} ${g.rows.length}${TIER_OFF.has(g.t) ? ' · 不出卡' : ''}`}</td></tr>
                  {g.rows.map((r) => {
                    const off = TIER_OFF.has(r.tier)
                    return (
                      <tr key={r.key} data-ledger-row={r.key} data-tier={r.tier}
                        className={`border-b border-border/60 last:border-b-0 ${off ? 'shadow-[inset_3px_0_0_hsl(var(--warning))]' : ''}`}>
                        <td className={`truncate py-1 pl-3 pr-2 text-[13px] font-semibold ${off ? 'text-secondary line-through decoration-warning/70' : 'text-foreground'}`} title={r.rule}>{r.name}</td>
                        <td className="whitespace-nowrap py-1 text-[12px] text-secondary">{SIDE_ARROW[r.side]}</td>
                        <td className={`whitespace-nowrap py-1 pr-2 text-right text-[13px] font-semibold tabular-nums ${toneR(r.excess)}`}>{fR(r.excess)}</td>
                        <td className="py-0.5 pr-2"><CiBar v={r.excess} lo={r.lo} hi={r.hi} min={sc.min} max={sc.max} width={176} dim={off} /></td>
                        <td className="py-1 pr-2 text-right tabular-nums text-foreground">{r.n ?? '—'}</td>
                        <td className="py-1 pr-3"><span data-tier-chip={r.tier} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-[4px] px-1.5 text-[11px] font-semibold leading-5 ${TIER_CHIP[r.tier] ?? 'bg-elevated text-secondary'}`}>
                          <span aria-hidden="true">{TIER_ICON[r.tier] ?? '·'}</span>{off ? `${r.tier}·不出卡` : r.tier}</span></td>
                      </tr>
                    )
                  })}
                </tbody>
              ))}
            </table>
            <div data-ledger-legend="" className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-[11px] text-secondary">
              <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm bg-accent" />区间整体 &gt; 0</span>
              <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm" style={{ background: 'repeating-linear-gradient(45deg, hsl(var(--warning)) 0 2px, transparent 2px 4px)' }} />跨 0 的负段 · 不显著</span>
              <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm bg-bear" />区间整体 &lt; 0</span>
              <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-[2px] bg-foreground/75" />0 基准 · 全表同一刻度</span>
            </div>
          </div>
        )}
      </>}
    </section>
  )
}
