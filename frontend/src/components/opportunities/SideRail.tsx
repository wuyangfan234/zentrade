/** 机会页右栏 (2026-11-15 B 方案): 账户名额 (次要位置) + 集中风险。只展示, 数字全部来自后端投影。
 * 账户 = radar_view.caps (与交易环准入同源); 集中风险 = themes 段 (自选分组, 每只按不利方向亏 k%, 空单按上涨)。 */
import type { ReactNode } from 'react'
import type { OppRadarView, ThemeExposure } from '@/lib/api'
import { dayTag, sectorZh, SIDE_ARROW, usdS } from '@/components/opportunities/terms'
import { badge } from '@/components/ui/badge'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const usd0 = (v: number) => `$${Math.abs(Math.round(v)).toLocaleString('en-US')}`
const loss = (v: number) => (Math.round(v) > 0 ? `−${usd0(v)}` : '$0')

function Card({ title, tags, children, ...rest }: { title: string; tags?: ReactNode; children: ReactNode } & Record<`data-${string}`, string | undefined>) {
  return (
    <section {...rest} className="mb-3 min-w-0 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 pb-1.5 pt-2.5">
        <h2 className="shrink-0 whitespace-nowrap text-[14px] font-semibold text-foreground">{title}</h2>
        {tags}
      </div>
      {children}
    </section>
  )
}

function Line({ k, v, sub, warn, bar, ...rest }: { k: string; v: ReactNode; sub?: string; warn?: boolean; bar?: number | null }
  & Record<`data-${string}`, string | undefined>) {
  return (
    <div {...rest} className="border-t border-border/70 px-3 py-1.5 first:border-t-0">
      <div className="flex items-baseline gap-2 text-[13px] tabular-nums">
        <span className="text-secondary">{k}</span>
        <b className={`ml-auto whitespace-nowrap font-semibold ${warn ? 'text-warning' : 'text-foreground'}`}>{v}</b>
      </div>
      {fin(bar) && <div className="mt-1 h-1 overflow-hidden rounded-sm bg-elevated"><i className={`block h-full ${warn ? 'bg-warning' : 'bg-zinc-400 dark:bg-zinc-500'}`} style={{ width: `${Math.round(Math.min(Math.max(bar, 0), 1) * 100)}%` }} /></div>}
      {sub && <div className={`mt-0.5 truncate text-[11px] leading-4 ${warn ? 'text-warning' : 'text-muted'}`} title={sub}>{sub}</div>}
    </div>
  )
}

/** 账户名额: 今日还能开几笔 / 持仓 / 止损全触发 / 最紧的板块名额 */
export function AccountCard({ view, nav }: { view: OppRadarView; nav: number | null }) {
  const c = view.caps
  const today = view.clock.et.slice(0, 10)
  const left = Math.max(c.opens.cap - c.opens.used, 0)
  const openK = c.opens.day !== today ? `${dayTag(c.opens.day, today)}还能开` : '今日还能开'
  const pr = c.portfolio_risk
  // 止损全触发 = Σ 股数 × 成本到当前止损 (与持仓卡同一份台账数, 不用四舍五入后的比例反推); 没持仓时按比例
  const holds = view.blips.filter((b) => b.hold && fin(b.hold.stop_now))
  const riskUsd = holds.length ? holds.reduce((t, b) => t + b.hold!.qty * Math.max((b.side === 'short' ? -1 : 1) * (b.hold!.avg - b.hold!.stop_now!), 0), 0)
    : fin(pr.used) && fin(nav) ? pr.used * nav : null
  const capUsd = fin(pr.cap) && fin(nav) ? pr.cap * nav : null
  const slot = c.slots[0]
  const hit = (code: string) => view.blips.filter((b) => b.kind === 'blocked' && (b.codes ?? [])[0] === code).length
  return (
    <Card data-account title="账户" tags={<span className="ml-auto text-[12px] text-muted">名额</span>}>
      <Line data-acct="opens" k={openK} v={`${left} 笔`} sub={`上限 ${c.opens.cap}${hit('daily_open_cap') ? ` · 受限 ${hit('daily_open_cap')}` : ''}`} warn={left === 0 || hit('daily_open_cap') > 0} />
      <Line data-acct="positions" k="持仓" v={`${c.positions.used}${c.positions.cap !== null ? ` / ${c.positions.cap}` : ''} 只`}
        bar={c.positions.cap ? c.positions.used / c.positions.cap : null} warn={hit('max_positions') > 0} />
      <Line data-acct="risk" k="止损全触发" v={riskUsd !== null ? <span className="text-bear">{usdS(-riskUsd)}</span> : fin(pr.used) ? `${(pr.used * 100).toFixed(1)}%` : '—'}
        sub={capUsd !== null ? `上限 ${usdS(-capUsd)}` : fin(pr.cap) ? `上限 ${Math.round(pr.cap * 100)}%` : undefined}
        bar={fin(pr.used) && pr.cap ? pr.used / pr.cap : null} warn={hit('portfolio_risk') > 0} />
      {slot && (
        <Line data-acct="slot" k={`${sectorZh(slot.sector)}股${SIDE_ARROW[slot.side]?.slice(2) ?? ''}`} v={`还剩 ${slot.free} 只`}
          sub={slot.holders.length ? `已有 ${slot.holders.join(' ')}` : undefined} warn={slot.free <= 1}
          bar={slot.cap ? slot.used / slot.cap : null} />
      )}
    </Card>
  )
}

/** 集中风险: 每组一条, 实心 = 持仓, 斜纹 = 加上挂单; 数字 = 持仓部分 */
export function ConcRisk({ t }: { t: ThemeExposure }) {
  const k = t.stress_pct
  const tip = `每只按不利方向亏 ${k}%（空单按上涨） · 按现价 · 一票多组各自计入 · 只显示 · 不拦单`
  const max = Math.max(...t.rows.map((r) => Math.max(r.after_fill_loss, r.stress_loss)), 1)
  return (
    <Card data-conc title="集中风险" tags={<span className={badge('limit')} title={tip}>{`假设每只都亏 ${k}%`}</span>}>
      {t.rows.length === 0 ? <div className="px-3 pb-3 text-[13px] text-muted">无持仓</div> : (
        <div className="px-3 pb-2.5">
          {t.rows.map((r) => {
            const held = r.count > 0
            const w1 = (r.stress_loss / max) * 100
            const w2 = (Math.max(r.after_fill_loss - r.stress_loss, 0) / max) * 100
            return (
              <div key={r.theme} data-conc-row={r.theme} title={[r.symbols.join(' '), r.pending.length ? `挂单 ${r.pending.join(' ')}` : ''].filter(Boolean).join(' · ')}
                className="grid min-h-[34px] grid-cols-[96px_minmax(0,1fr)_76px] items-center gap-x-2 py-1">
                <span className="text-[13px] leading-4 text-secondary [overflow-wrap:anywhere]">{r.theme}</span>
                <span className="min-w-0">
                  <span className="flex h-2.5 overflow-hidden rounded-[3px] bg-elevated">
                    <i className="block h-full bg-zinc-400 dark:bg-zinc-500" style={{ width: `${w1}%` }} />
                    <i className="block h-full bg-[repeating-linear-gradient(135deg,#a1a1aa_0_3px,transparent_3px_6px)]" style={{ width: `${w2}%` }} />
                  </span>
                  {r.pending.length > 0 && <span data-conc-after className="mt-0.5 block whitespace-nowrap text-[11px] leading-[14px] text-secondary">{`${held ? '加挂单 ' : '仅挂单 '}${loss(r.after_fill_loss)}`}</span>}
                </span>
                <span className="text-right text-[13px] tabular-nums">
                  {held ? <b data-conc-loss className="font-semibold text-bear">{loss(r.stress_loss)}</b> : <span className="text-muted">—</span>}
                </span>
              </div>
            )
          })}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-secondary">
            <span className="inline-flex items-center gap-1"><i className="h-2 w-3 rounded-[2px] bg-zinc-400 dark:bg-zinc-500" />持仓</span>
            <span className="inline-flex items-center gap-1"><i className="h-2 w-3 rounded-[2px] bg-[repeating-linear-gradient(135deg,#a1a1aa_0_3px,transparent_3px_6px)]" />挂单</span>
            {t.hidden > 0 && <span className="text-muted">{`另 ${t.hidden} 组`}</span>}
            <span data-conc-total className="ml-auto whitespace-nowrap">{'持仓合计 '}<b className="font-semibold text-bear">{loss(t.total.stress_loss)}</b></span>
          </div>
        </div>
      )}
    </Card>
  )
}
