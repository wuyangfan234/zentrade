import { Fragment, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Filter } from 'lucide-react'
import { api, type EntryFunnelDay, type EntryFunnelPayload } from '@/lib/api'
import { SectionTitle } from '@/components/data/SectionTitle'

/** 系统状态页: 准入漏斗 (只读)。激活 → 触价 → 被拦 → 成交, 回补成交单列。
 * 0.9.1: 不再整块隐藏; 三种空态分开: 今天没卡(正常, 灰○) / 数据还没到(等待, ⏳) / 读取失败(异常, 红✗ 可重试)。
 * 0.13.0: 白底卡 + 图标分区标题; 拦截原因由一长串文字改为可换行的标签 (原因 · 次数), 默认显示前 6 项可展开;
 * 后端未翻译的英文原因码在前端补中文名, 悬停显示原码。只改展示。 */

const CODE_LABEL: Record<string, string> = {
  source_not_tradable: '该来源未开放下单',
  risk_zero: '风险额为 0',
  v4_book_only: '仅下单簿可开仓',
  not_in_watchlist: '不在自选',
  direction_conflict: '多空冲突',
  sector_side_cap: '同板块同向已满',
  sector_unknown: '板块未知',
  daily_open_cap: '当日开仓已满',
  watchlist_removed: '已移出自选',
  borrow_unavailable: '不可借券',
  explore_daily_quota: '探索额度已用完',
  legacy_card: '旧版卡',
  book_requires_v4: '下单需 v4 规则',
  premarket_beyond_stop: '盘前越过止损已取消',
  premarket_check_pending: '盘前越止损检查未完成',
}

/** "source_not_tradable:phantom_4h" → { main: '该来源未开放下单', sub: 'phantom_4h' }; 后端已给中文名的原样返回 */
export function reasonText(reason: string, label: string): { main: string; sub?: string } {
  if (label && label !== reason) return { main: label }
  const [code, ...rest] = reason.split(':')
  const zh = CODE_LABEL[code]
  if (!zh) return { main: label || reason }
  return rest.length ? { main: zh, sub: rest.join(':') } : { main: zh }
}

const PREVIEW = 6
/** 0 用弱色, 让有数的格子跳出来 */
const num = (n: number) => (n ? 'text-foreground' : 'text-muted')
const CARD = 'rounded-card border border-border bg-surface'

/** mark=false 用于手机重复行: 不带 data-reason 等测试锚点, 避免与桌面列重复计数 */
function Reasons({ d, mark = true }: { d: EntryFunnelDay; mark?: boolean }) {
  const [all, setAll] = useState(false)
  const items = [
    ...d.global.map((g) => ({ key: `g:${g.reason}`, global: true, code: g.reason, main: `全局：${g.label}`, sub: undefined as string | undefined, n: `${g.since} 起` })),
    ...d.reasons.map((r) => { const t = reasonText(r.reason, r.label); return { key: r.reason, global: false, code: r.reason, main: t.main, sub: t.sub, n: String(r.n) } }),
  ]
  if (!items.length) {
    if (d.recorded && d.active === 0) return <span className="text-muted">○ 今天没有卡（正常）</span>
    return <span className="text-secondary">{d.recorded ? '—' : '无记录'}</span>
  }
  const shown = all ? items : items.slice(0, PREVIEW)
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map((it) => (
          <span key={it.key} data-reason={mark ? it.code : undefined} title={it.code}
            className={`inline-flex max-w-full items-center gap-1 rounded px-1.5 py-px leading-5 ${it.global ? 'bg-rose-500/10 text-rose-700 dark:text-rose-400' : 'bg-elevated text-secondary'}`}>
            <span className="break-all">{it.main}{it.sub ? <span className="text-muted">{` · ${it.sub}`}</span> : null}</span>
            <span className="border-l border-border pl-1 font-mono font-semibold tabular-nums text-foreground">{it.n}</span>
          </span>
        ))}
        {items.length > PREVIEW && (
          <button type="button" data-reasons-toggle={mark ? '' : undefined} onClick={() => setAll((v) => !v)}
            className="rounded border border-dashed border-border px-1.5 py-px leading-5 text-accent hover:bg-accent/10">{all ? '收起' : `+${items.length - PREVIEW} 项`}</button>
        )}
    </div>
  )
}

export function EntryFunnelTable() {
  const q = useQuery<EntryFunnelPayload>({
    queryKey: ['entry-funnel'], queryFn: () => api.entryFunnel(10), refetchInterval: 60_000,
  })
  const days = q.data?.days
  const head = <SectionTitle icon={Filter}>准入漏斗 · 机会板卡</SectionTitle>
  if (q.isError) {
    const why = q.error instanceof Error ? q.error.message : '未知错误'
    return (
      <section data-funnel>{head}
        <div role="alert" data-empty="error" className={`${CARD} mt-3 border-l-[3px] border-l-rose-500 px-4 py-3 text-xs`}>
          <div className="text-foreground">{`✗ 准入漏斗读取失败（异常）：${why}`}</div>
          <button type="button" onClick={() => { void q.refetch() }} className="mt-1.5 rounded-btn border border-border px-2.5 py-1 text-foreground hover:bg-elevated">重试</button>
        </div>
      </section>
    )
  }
  if (!days || days.length === 0) {
    return (
      <section data-funnel>{head}
        <div data-empty="pending" className={`${CARD} mt-3 px-4 py-3 text-xs text-secondary`}>
          {q.isLoading ? '⏳ 读取中…' : '⏳ 准入漏斗数据还没到（等待第一张卡激活后才有记录）'}
        </div>
      </section>
    )
  }
  return (
    <section data-funnel>
      {head}
      <div className={`${CARD} mt-3 overflow-hidden`}>
        <div className="border-b border-border px-4 py-2 text-[11px] text-muted">激活 → 触价 → 被拦 → 成交；被拦是保护规则在起作用，本身不算异常。</div>
        <div>
          <table className="w-full table-fixed text-xs tabular-nums">
            <thead>
              <tr className="bg-elevated/50 text-left text-muted">
                <th scope="col" className="w-14 whitespace-nowrap px-4 py-1.5 font-normal md:w-16">日期</th>
                <th scope="col" className="w-12 py-1.5 pr-3 text-right font-normal">激活</th>
                <th scope="col" className="w-12 py-1.5 pr-3 text-right font-normal">触价</th>
                <th scope="col" className="w-14 py-1.5 pr-3 text-right font-normal">被拦</th>
                <th scope="col" className="w-24 py-1.5 pr-4 text-right font-normal">成交</th>
                <th scope="col" className="hidden py-1.5 pr-4 font-normal md:table-cell">拦截原因</th>
              </tr>
            </thead>
            <tbody className="[&>tr:not([data-funnel-reasons-mobile])]:border-t [&>tr]:border-border/60">
              {days.map((d) => (
                <Fragment key={d.day}>
                <tr data-funnel-day={d.day} className="align-top">
                  <td className="whitespace-nowrap px-4 py-2 font-mono text-foreground">{d.day.slice(5)}</td>
                  <td className={`py-2 pr-3 text-right font-mono ${num(d.active)}`}>{d.active}</td>
                  <td className={`py-2 pr-3 text-right font-mono ${num(d.touched)}`}>{d.touched}</td>
                  <td className={`py-2 pr-3 text-right font-mono ${num(d.blocked)}`}>{d.blocked}</td>
                  <td data-funnel-filled className={`py-2 pr-4 text-right font-mono ${num(d.filled)}`}>
                    {d.filled}
                    {d.backfill > 0 && <span className="text-warning">{` +${d.backfill} 回补`}</span>}
                  </td>
                  <td data-funnel-reasons data-empty={!d.global.length && !d.reasons.length && d.recorded && d.active === 0 ? 'none' : undefined}
                    className="hidden break-words py-2 pr-4 md:table-cell"><Reasons d={d} /></td>
                </tr>
                {/* 手机: 拦截原因单独占一整行, 不挤在第 6 列里横向滚动 */}
                {(d.global.length > 0 || d.reasons.length > 0 || (d.recorded && d.active === 0)) && (
                  <tr data-funnel-reasons-mobile className="md:hidden">
                    <td colSpan={5} className="break-words px-4 pb-2 pt-0"><Reasons d={d} mark={false} /></td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
