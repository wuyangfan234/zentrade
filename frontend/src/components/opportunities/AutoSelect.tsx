/** 自动选策略 (2026-11-18): 机会页右栏一张卡 (入选策略各自的前向成绩 + 下次评定) + 齿轮里的开关 / 候补 / 换入记录。
 * 数字全部来自后端 rule_auto_select.view (规则前向成绩的只读摘要); 这里只展示, 开关与撤回各走一个接口。
 * 超额 = 成交卡比同日同向其余成交卡多出的 R。状态: 攒样本 / 观察中 (不足 20 个信号日) / 领先 / 未定 / 落后。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type AutoSelectRow, type AutoSelectView } from '@/lib/api'
import { displayStrategyName } from '@/lib/strategyDisplay'
import { md, SIDE_ARROW, weekday } from '@/components/opportunities/terms'
import { badge } from '@/components/ui/badge'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const SCALE = 0.6
export const VERDICT_ZH: Record<string, string> = { thin: '攒样本', early: '观察中', above: '领先', unclear: '未定', below: '落后' }
const verdictCls = (v: string) => (v === 'above' ? badge('status') : v === 'below' ? badge('warn') : v === 'early' ? badge('status', 'neutral') : badge('limit'))
const ex = (v?: number | null) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}` : '—')
const exCls = (v?: number | null) => (!fin(v) || Math.abs(v) < 0.005 ? 'text-secondary' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const day = (iso?: string | null) => (iso ? `${md(iso)} ${weekday(iso)}` : '—')
/** 行内用短名: 去掉括号里的解释 (全名在悬停) */
export const shortName = (n: string) => displayStrategyName(n).replace(/（[^）]*）/g, ' ').replace(/\s{2,}/g, ' ').trim()
const COLS = 'grid-cols-[minmax(0,1fr)_44px_52px]'
const COLS_WIDE = 'grid-cols-[minmax(0,1fr)_44px_64px_52px]'
const pos = (v: number) => (Math.min(Math.max(v, -SCALE), SCALE) + SCALE) / (2 * SCALE) * 100

/** 一行的悬停说明: 样本与区间 (主层只放名字 / 超额 / 状态) */
export function rowTitle(r: AutoSelectRow, need: number): string {
  const n = `${displayStrategyName(r.name)} · ${r.days ?? 0} 个信号日 · 成交 ${r.filled ?? 0} 笔 · 命中 ${r.hits ?? 0} 次`
  if (r.verdict === 'thin') return `${n} · 满 ${need} 个信号日后评定`
  const ci = r.ci ? ` · 95% 区间 ${ex(r.ci[0])} ~ ${ex(r.ci[1])}` : ''
  return `${n} · 超额 ${ex(r.excess)} R${ci}`
}

/** 超额的区间条: 中线 = 0, 点 = 超额, 横条 = 95% 区间; 样本不足时画信号日进度 */
function Interval({ r, need }: { r: AutoSelectRow; need: number }) {
  if (!r.ci || !fin(r.excess)) {
    return <span data-auto-progress className="block h-1.5 overflow-hidden rounded-sm bg-border"><i className="block h-full bg-zinc-500 dark:bg-zinc-400" style={{ width: `${Math.min((r.days ?? 0) / need, 1) * 100}%` }} /></span>
  }
  const [a, z] = [pos(r.ci[0]), pos(r.ci[1])]
  return (
    <span data-auto-interval className="relative block h-3.5">
      <i className="absolute inset-x-0 top-1.5 h-0.5 bg-border" />
      <i className="absolute inset-y-0 left-1/2 w-px bg-muted" />
      <i className="absolute top-[5px] h-1 rounded-sm bg-secondary" style={{ left: `${a}%`, width: `${Math.max(z - a, 1)}%` }} />
      <i className={`absolute top-[3px] -ml-1 h-2 w-2 rounded-full ${fin(r.excess) && r.excess > 0 ? 'bg-bull dark:bg-red-400' : r.excess < 0 ? 'bg-bear' : 'bg-secondary'}`} style={{ left: `${pos(r.excess)}%` }} />
    </span>
  )
}

function Row({ r, need, wide }: { r: AutoSelectRow; need: number; wide?: boolean }) {
  const thin = r.verdict === 'thin'
  return (
    <div data-auto-row={r.id} data-side={r.side} data-verdict={r.verdict} title={rowTitle(r, need)}
      className={`grid min-h-[34px] items-center gap-x-2 border-t border-border/70 py-1 first:border-t-0 ${wide ? COLS_WIDE : COLS}`}>
      <span className={`min-w-0 text-[13px] text-foreground ${wide ? 'line-clamp-2 break-words leading-4' : 'truncate'}`}>{shortName(r.name)}</span>
      <span data-auto-excess className={`whitespace-nowrap text-right text-[13px] font-semibold tabular-nums ${thin ? 'font-normal text-muted' : exCls(r.excess)}`}>{thin ? `${r.days ?? 0}/${need}` : ex(r.excess)}</span>
      {wide && <Interval r={r} need={need} />}
      <span className="flex justify-end"><span className={verdictCls(r.verdict)}>{VERDICT_ZH[r.verdict] ?? '未定'}</span></span>
    </div>
  )
}

/** 最近一次评定的一句话 (只在有结果时显示) */
export function lastLine(v: AutoSelectView): string {
  const done = v.swaps.find((s) => !s.undone_at)
  if (done) return `上次调整 ${md(done.day)} · ${displayStrategyName(done.out_name)} → ${displayStrategyName(done.in_name)}`
  return v.last_eval ? `上次评定 ${md(v.last_eval.day)} · 未调整` : `已记分 ${v.days} 个信号日`
}

/** 机会页右栏: 入选策略 (自动)。点卡 = 打开齿轮。 */
export function AutoCard({ v, onOpen }: { v: AutoSelectView; onOpen: () => void }) {
  const side = (k: 'long' | 'short') => (
    <div data-auto-side={k} className="min-w-0">
      <div className={`grid items-center gap-x-2 pb-0.5 pt-1 text-[12px] ${COLS}`}>
        <span className="font-semibold text-secondary">{SIDE_ARROW[k]}</span>
        <span className="whitespace-nowrap text-right text-muted" title="超额 = 成交卡比同日同向其余成交卡多出的 R · 样本不足时写信号日进度">超额 R</span>
        <span className="text-right text-muted">状态</span>
      </div>
      {v.picks[k].map((r) => <Row key={r.id} r={r} need={v.need_days} />)}
    </div>
  )
  return (
    <section data-auto-card data-auto-on={v.enabled ? '' : undefined} className="mb-3 min-w-0 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 pb-1 pt-2.5">
        <h2 className="shrink-0 whitespace-nowrap text-[14px] font-semibold text-foreground">入选策略</h2>
        <span data-auto-state className={v.enabled ? badge('status') : badge('limit')} title={v.enabled ? '每周最后一个交易日收盘后按前向成绩评定 · 每侧每次至多换 1 条' : '已关闭自动调整 · 照常记分'}>{v.enabled ? '自动' : '手动'}</span>
        <span data-auto-scored className="ml-auto whitespace-nowrap text-[12px] tabular-nums text-muted">{`记分 ${v.scored} 条`}</span>
      </div>
      <div className="px-3">{side('long')}{side('short')}</div>
      <button type="button" data-auto-open onClick={onOpen}
        className="mt-1 flex w-full flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-border/70 px-3 py-2 text-left text-[12px] tabular-nums text-secondary hover:bg-elevated">
        <span data-auto-next className="whitespace-nowrap">{`${v.enabled ? '下次评定' : '下次记录'} ${day(v.next_eval)} 收盘后`}</span>
        <span data-auto-last className="min-w-0 truncate text-muted">{lastLine(v)}</span>
        <span className="ml-auto whitespace-nowrap text-accent dark:text-blue-400">{`候补 ${v.bench.length}`}</span>
      </button>
    </section>
  )
}

const REASON_ZH: Record<string, string> = {
  no_candidate: '无达标候补', incumbents_hold: '入选策略不低于候补', cooldown: '调整后 20 个交易日内不动',
  catalog_unavailable: '策略目录未就绪', no_scores: '当日记分未完成',
}

function Bench({ v, side }: { v: AutoSelectView; side: 'long' | 'short' }) {
  const rows = v.bench.filter((r) => r.side === side)
  return (
    <div data-auto-bench={side} className="flex max-h-[320px] min-h-0 min-w-0 flex-col overflow-hidden rounded-card border border-border">
      <div className="flex shrink-0 items-center gap-2 border-b border-border/70 px-3 py-1.5 text-[12px]">
        <span className="font-semibold text-foreground">{SIDE_ARROW[side]}</span>
        <span className="text-secondary">{`候补 ${rows.length}`}</span>
        <span className="ml-auto text-muted">按超额排序</span>
      </div>
      <div className="scroll-y-visible min-h-0 flex-1 overflow-y-auto px-3">
        {rows.length === 0 ? <div className="py-2 text-[12px] text-muted">无候补</div> : rows.map((r) => <Row key={r.id} r={r} need={v.need_days} wide />)}
      </div>
    </div>
  )
}

/** 齿轮顶部: 开关 + 规则 + 候补记分 + 换入记录 */
export function AutoPanel({ fallback }: { fallback?: AutoSelectView | null }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['opportunities', 'auto-select'], queryFn: () => api.autoSelect(), initialData: fallback ?? undefined })
  const done = () => { qc.invalidateQueries({ queryKey: ['opportunities'] }) }
  const sw = useMutation({ mutationFn: (on: boolean) => api.setAutoSelect(on), onSuccess: done })
  const undo = useMutation({ mutationFn: (id: string) => api.undoAutoSelect(id), onSuccess: done })
  const v = q.data
  if (!v) return q.isError ? <div data-auto-panel-error className="mb-3 text-xs text-danger">{`自动选策略读取失败 · ${(q.error as Error).message}`}</div> : null
  const last = v.last_eval
  const err = sw.error ?? undo.error
  return (
    <section data-auto-panel className="mb-4">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-[14px] font-semibold text-foreground">自动选策略</h3>
        <span className={v.enabled ? badge('status') : badge('limit')}>{v.enabled ? '运行中' : '已关闭'}</span>
        <button type="button" role="switch" aria-checked={v.enabled} aria-label="自动选策略" data-auto-switch disabled={sw.isPending}
          onClick={() => sw.mutate(!v.enabled)}
          className={`relative ml-auto h-6 w-11 shrink-0 rounded-full border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50 max-md:h-7 max-md:w-12 ${v.enabled ? 'border-accent bg-accent dark:border-blue-700 dark:bg-blue-700' : 'border-border bg-elevated'}`}>
          <i className={`absolute top-0.5 h-[18px] w-[18px] rounded-full bg-white shadow transition-[left] max-md:h-[22px] max-md:w-[22px] ${v.enabled ? 'left-[22px]' : 'left-0.5'}`} />
        </button>
      </div>
      <div data-auto-facts className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border text-[12px] sm:grid-cols-4">
        {([['记分策略', `${v.scored} 条`], ['已记分', `${v.days} 个信号日`], ['下次评定', `${day(v.next_eval)} 收盘后`], ['累计换入', `${v.swaps.filter((s) => !s.undone_at).length} 次`]] as const).map(([k, val]) => (
          <div key={k} className="bg-surface px-3 py-1.5">
            <div className="text-[11px] leading-4 text-muted">{k}</div>
            <div className="whitespace-nowrap text-[13px] font-semibold tabular-nums leading-5 text-foreground">{val}</div>
          </div>
        ))}
      </div>
      <div data-auto-rules className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px]">
        <span className="text-muted">换入门槛</span>
        {[`有成交信号日 ≥ ${v.need_days}`, `成交 ≥ ${v.need_filled} 笔`, '超额区间高于 0', '每侧每次 1 条', `换后 ${v.cooldown_days} 个交易日不动`].map((t) => <span key={t} className={badge('limit')}>{t}</span>)}
      </div>
      {last && (
        <div data-auto-last-eval className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-secondary">
          <span className="text-muted">{`上次评定 ${day(last.day)}`}</span>
          {(['long', 'short'] as const).map((k) => {
            const s = last.sides[k]
            if (!s) return null
            const txt = s.action === 'swap' ? `${displayStrategyName(s.out_name ?? '')} → ${displayStrategyName(s.in_name ?? '')}${s.applied ? '' : ' · 未执行'}` : REASON_ZH[s.reason ?? ''] ?? '未调整'
            return <span key={k} data-auto-eval={k} className="whitespace-nowrap">{`${SIDE_ARROW[k]} · ${txt}`}</span>
          })}
        </div>
      )}
      <div className="mt-3 grid gap-3 sm:auto-rows-fr sm:grid-cols-2">
        <Bench v={v} side="long" />
        <Bench v={v} side="short" />
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-secondary" />超额 R</span>
        <span className="inline-flex items-center gap-1"><i className="h-1 w-4 rounded-sm bg-secondary" />95% 区间</span>
        <span className="inline-flex items-center gap-1"><i className="h-3 w-px bg-muted" />0 线 · 横轴 ±0.6R</span>
        <span className="inline-flex items-center gap-1"><i className="h-1.5 w-4 rounded-sm bg-zinc-500 dark:bg-zinc-400" />信号日进度（满 {v.need_days}）</span>
      </div>
      {v.swaps.length > 0 && (
        <div data-auto-swaps className="mt-3 overflow-hidden rounded-card border border-border">
          <div className="border-b border-border/70 px-3 py-1.5 text-[12px] font-semibold text-foreground">{`换入记录 ${v.swaps.length}`}</div>
          {v.swaps.map((s) => (
            <div key={s.id} data-auto-swap={s.id} className="flex min-h-[40px] flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-border/70 px-3 py-1.5 text-[13px] tabular-nums first:border-t-0">
              <span className="whitespace-nowrap text-secondary">{day(s.day)}</span>
              <span className="whitespace-nowrap font-semibold text-foreground">{SIDE_ARROW[s.side]}</span>
              <span className="min-w-0 truncate text-foreground">{`${displayStrategyName(s.out_name)} → ${displayStrategyName(s.in_name)}`}</span>
              <span className="whitespace-nowrap text-[12px] text-secondary">{`超额 ${ex(s.out_excess)} → ${ex(s.in_excess)}`}</span>
              {s.undone_at ? <span className={`ml-auto ${badge('limit')}`}>已撤回</span> : (
                <button type="button" data-auto-undo={s.id} disabled={undo.isPending} onClick={() => undo.mutate(s.id)}
                  className="ml-auto rounded-btn border border-border px-2.5 py-0.5 text-[12px] text-secondary hover:bg-elevated disabled:opacity-40 max-md:py-1.5">撤回</button>
              )}
            </div>
          ))}
        </div>
      )}
      {err && <div data-auto-error className="mt-2 text-xs text-danger">{`操作失败 · ${err instanceof ApiError ? err.message : (err as Error).message}`}</div>}
    </section>
  )
}
