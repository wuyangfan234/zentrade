/**
 * 历史日回看 (2026-11-15 B 档): 选一个过去的交易日, 每块只读那天冻结的文件, 回答“那天页面知道什么”。
 * - 机会板 = 那天之前两个信号日的下单簿; 今日多空 = 写给那天的名单; 其余块 = 各自当天的文件。
 * - 「结局」列取自之后的结算, 用竖线与当天的列隔开; 当天没有文件的块写「未存档」, 不拿别的日子补。
 * - 不轮询、不出网、不推送。
 */
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { api, type AsOfOutcome, type AsOfPayload } from '@/lib/api'
import { badge } from '@/components/ui/badge'
import { md, p2, rS, SIDE_ZH } from '@/components/opportunities/terms'

const WHY_ZH: Record<string, string> = { stop: '止损', target: '目标', time: '到期', expired: '到期', hold: '到期' }
const STANCE_ZH: Record<string, string> = { up: '偏多', down: '偏空', range: '观望' }
const sgn = (v?: number | null) => (typeof v === 'number' ? (v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-secondary') : 'text-muted')

function outcomeTxt(o: AsOfOutcome | null): string {
  if (!o) return '未结算'
  if (o.status === 'no_fill') return '未成交'
  if (o.status === 'closed') return `${WHY_ZH[o.why ?? ''] ?? '平仓'} ${rS(o.r)}`
  return '未结算'
}

function Block({ title, sub, missing, children }: { title: string; sub?: string; missing?: boolean; children?: ReactNode }) {
  return (
    <section data-asof-block={title} className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2">
        <h2 className="text-[14px] font-semibold text-foreground">{title}</h2>
        {sub && <span className="text-[12px] tabular-nums text-muted">{sub}</span>}
        {missing && <span data-asof-missing className={`${badge('limit')} ml-auto`}>未存档</span>}
      </div>
      {!missing && children}
    </section>
  )
}

function Table({ head, outcomeFrom, children }: { head: string[]; outcomeFrom: number; children: ReactNode }) {
  return (
    <div className="overflow-x-auto border-t border-border/70">
      <table className="w-full min-w-[560px] text-[13px] tabular-nums">
        <thead><tr className="h-8 bg-elevated text-[12px] text-muted">
          {head.map((h, i) => (
            <th key={h} className={`whitespace-nowrap px-3 font-medium ${i === 0 ? 'text-left' : 'text-right'} ${i === outcomeFrom ? 'border-l border-border' : ''}`}>{h}</th>
          ))}
        </tr></thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  )
}

const Empty = () => <div className="border-t border-border/70 px-3 py-2 text-[13px] text-muted">无</div>

export function AsOfView({ day, onOpen }: { day: string; onOpen: (s: string) => void }) {
  const q = useQuery<AsOfPayload>({ queryKey: ['opportunities', 'asof', day], queryFn: () => api.oppAsof(day), staleTime: 300_000 })
  if (q.isLoading) return <div data-loading role="status" className="py-12 text-center text-sm text-muted">加载中</div>
  if (q.isError || !q.data) return <div data-error className="py-6 text-sm text-danger">{`读取失败 · ${(q.error as Error)?.message ?? ''}`}</div>
  const d = q.data
  const b = d.board
  const ls = d.ls_rank
  const it = d.intraday
  const rd = d.radar
  const rs = d.resonance
  const mo = d.mood
  const row = 'h-8 cursor-pointer border-t border-border/70 hover:bg-elevated'
  return (
    <div data-asof={d.asof}>
      <Block title="盘前判断" sub={mo?.generated_at ? `${md(d.asof)} 生成` : undefined} missing={!mo}>
        {mo && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border/70 px-3 py-2 text-[13px] tabular-nums">
            <span className="font-semibold text-foreground">{STANCE_ZH[mo.stance ?? ''] ?? '—'}</span>
            {mo.probs && <span className="text-secondary">{`上涨 ${mo.probs.up}% · 震荡 ${mo.probs.range}% · 下跌 ${mo.probs.down}%`}</span>}
          </div>
        )}
      </Block>
      <Block title="今日多空" sub={ls ? [`名单 ${md(ls.as_of)} 收盘`, ls.model ? `GBM ${ls.model}` : '', ls.late ? '盘中补出' : '', ls.settled_end ? `结算至 ${md(ls.settled_end)}` : '未结算'].filter(Boolean).join(' · ') : undefined} missing={!ls}>
        {ls && (
          <div className="grid gap-px border-t border-border/70 bg-border sm:grid-cols-2">
            {(['long', 'short'] as const).map((side) => (
              <div key={side} className="bg-surface">
                <Table head={[side === 'long' ? '看多' : '看空', '排名', '收盘', '结局']} outcomeFrom={3}>
                  {ls[side].map((r) => (
                    <tr key={r.symbol} data-asof-ls={`${side}:${r.symbol}`} className={row} onClick={() => onOpen(r.symbol)}>
                      <td className="px-3 text-left font-bold text-foreground">{r.symbol}</td>
                      <td className="px-3 text-right text-secondary">{r.rank ?? '—'}</td>
                      <td className="px-3 text-right text-secondary">{p2(r.close)}</td>
                      <td className={`border-l border-border px-3 text-right font-semibold ${sgn(r.r)}`}>{r.r == null ? '未结算' : `${r.exit === 'stop' ? '止损' : '到期'} ${r.r > 0 ? '+' : r.r < 0 ? '−' : ''}${Math.abs(r.r).toFixed(2)} ATR`}</td>
                    </tr>
                  ))}
                </Table>
              </div>
            ))}
          </div>
        )}
      </Block>
      <Block title="机会板" sub={b ? `信号日 ${b.signal_days.map(md).join(' · ')} 下单簿` : undefined} missing={!b}>
        {b && (b.cards.length === 0 ? <Empty /> : (
          <Table head={['代码', '方向', '信号日', '命中策略', '入场区', '止损', '目标', '结局']} outcomeFrom={7}>
            {b.cards.map((c) => (
              <tr key={c.card_id} data-asof-card={c.card_id} className={row} onClick={() => onOpen(c.symbol)}>
                <td className="px-3 text-left font-bold text-foreground">{c.symbol}</td>
                <td className={`px-3 text-right font-semibold ${c.side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[c.side]}</td>
                <td className="px-3 text-right text-secondary">{md(c.signal_day)}</td>
                <td className="max-w-[200px] truncate px-3 text-right text-secondary" title={c.rules.map((r) => r.name).join(' · ')}>{c.rules.map((r) => r.name).join(' · ') || '—'}</td>
                <td className="whitespace-nowrap px-3 text-right text-secondary">{c.entry_zone ? `${p2(c.entry_zone[0])}–${p2(c.entry_zone[1])}` : '—'}</td>
                <td className="px-3 text-right text-secondary">{p2(c.stop)}</td>
                <td className="px-3 text-right text-secondary">{p2(c.target)}</td>
                <td className={`whitespace-nowrap border-l border-border px-3 text-right font-semibold ${sgn(c.outcome?.r)}`}>{outcomeTxt(c.outcome)}</td>
              </tr>
            ))}
          </Table>
        ))}
      </Block>
      <Block title="盘中强势" sub={it ? `检查 ${it.checks} 次` : undefined} missing={!it}>
        {it && (it.cards.length === 0 ? <Empty /> : (
          <Table head={['代码', '方向', '检查点', '入场', '止损', '目标', '结局']} outcomeFrom={6}>
            {it.cards.map((c) => (
              <tr key={`${c.symbol}${c.check}`} className={row} onClick={() => onOpen(c.symbol)}>
                <td className="px-3 text-left font-bold text-foreground">{c.symbol}</td>
                <td className={`px-3 text-right font-semibold ${c.side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[c.side] ?? c.side}</td>
                <td className="px-3 text-right text-secondary">{c.check}</td>
                <td className="px-3 text-right text-secondary">{p2(c.entry)}</td>
                <td className="px-3 text-right text-secondary">{p2(c.stop)}</td>
                <td className="px-3 text-right text-secondary">{p2(c.target)}</td>
                <td className={`whitespace-nowrap border-l border-border px-3 text-right font-semibold ${sgn(c.r)}`}>{c.r == null ? '未结算' : `${WHY_ZH[c.exit_reason ?? ''] ?? '平仓'} ${rS(c.r)}`}</td>
              </tr>
            ))}
          </Table>
        ))}
      </Block>
      <Block title="开盘放量" sub={rd ? `${rd.rows.length} 只入榜 · 共 ${rd.symbols ?? '—'} 只` : undefined} missing={!rd}>
        {rd && (rd.rows.length === 0 ? <Empty /> : (
          <Table head={['代码', '量比', '跳空', '开盘 30 分', '收盘']} outcomeFrom={4}>
            {rd.rows.map((r) => (
              <tr key={r.symbol} className={row} onClick={() => onOpen(r.symbol)}>
                <td className="px-3 text-left font-bold text-foreground">{r.symbol}</td>
                <td className="px-3 text-right text-secondary">{`${r.rvol.toFixed(1)}×`}</td>
                <td className={`px-3 text-right ${sgn(r.gap_pct)}`}>{`${r.gap_pct > 0 ? '+' : ''}${r.gap_pct.toFixed(1)}%`}</td>
                <td className={`px-3 text-right ${sgn(r.or_chg_pct)}`}>{`${r.or_chg_pct > 0 ? '+' : ''}${r.or_chg_pct.toFixed(1)}%`}</td>
                <td className={`border-l border-border px-3 text-right font-semibold ${sgn(r.chg_close_pct)}`}>{r.chg_close_pct == null ? '—' : `${r.chg_close_pct > 0 ? '+' : ''}${r.chg_close_pct.toFixed(1)}%`}</td>
              </tr>
            ))}
          </Table>
        ))}
      </Block>
      <Block title="多信号共振" missing={!rs}>
        {rs && (rs.rows.length === 0 ? <Empty /> : (
          <div className="flex flex-wrap gap-2 border-t border-border/70 px-3 py-2 text-[13px] tabular-nums">
            {rs.rows.map((r) => (
              <button key={`${r.symbol}${r.side}`} type="button" onClick={() => onOpen(r.symbol)} className="rounded-btn border border-border px-2 py-0.5 hover:bg-elevated">
                <b className="text-foreground">{r.symbol}</b>
                <span className={`ml-1 ${r.side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[r.side] ?? r.side}</span>
                <span className="ml-1 text-muted">{`${r.n} 引擎`}</span>
              </button>
            ))}
          </div>
        ))}
      </Block>
    </div>
  )
}
