/** 公式挖掘: 自动写排序公式 -> 候补席 -> 规则前向记分 -> 自动选策略。
 * 数字全部来自后端 formula_miner.view; 这里只展示, 开关与立即挖掘各走一个接口。
 * 席位按“与在席公式选出的票差多少”分配, 回放超额只作参考 (不参与排序), 好坏由前向成绩判定。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type FormulaMinerRow, type FormulaMinerRun, type FormulaMinerView } from '@/lib/api'
import { VERDICT_ZH } from '@/components/opportunities/AutoSelect'
import { md } from '@/components/opportunities/terms'
import { badge } from '@/components/ui/badge'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
/** 四舍五入后为 0 的不带符号 (不出现 −0.00) */
const ex = (v?: number | null) => (!fin(v) ? '—' : Math.abs(v) < 0.005 ? '0.00' : `${v > 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}`)
const exCls = (v?: number | null) => (!fin(v) || Math.abs(v) < 0.005 ? 'text-secondary' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const REASON_ZH: Record<string, string> = { below: '前向落后', timeout: '到期未领先', removed: '已手动移除' }
const SKIP_ZH: Record<string, string> = { bench_full: '候补席已满', no_pool: '候选池未导出', disabled: '已关闭', running: '运行中' }
const COLS = 'grid-cols-[minmax(0,1.3fr)_minmax(0,2fr)_64px_84px_64px] max-md:gap-x-2 max-md:grid-cols-[minmax(0,1fr)_44px_44px_56px]'

/** 前向一栏: 样本不足写进度, 够了写超额 */
export function forwardText(r: FormulaMinerRow, need: number): string {
  return r.forward.verdict === 'thin' ? `${r.forward.days ?? 0}/${need}` : ex(r.forward.excess)
}

export function rowTitle(r: FormulaMinerRow, need: number): string {
  const f = r.forward
  const head = `${r.name} · 入席 ${md(r.admitted_day)} · 已坐 ${r.sat_days} 个交易日`
  const fw = f.verdict === 'thin'
    ? `前向 ${f.days ?? 0} 个信号日 · 成交 ${f.filled ?? 0} 笔 · 满 ${need} 个信号日后评定`
    : `前向超额 ${ex(f.excess)} R${f.ci ? ` · 95% 区间 ${ex(f.ci[0])} ~ ${ex(f.ci[1])}` : ''} · 成交 ${f.filled ?? 0} 笔`
  const h = r.replay?.holdout
  return `${head} · ${fw}${h && fin(h.excess) ? ` · 回放留出段 ${ex(h.excess)} R / ${h.n ?? 0} 笔` : ''}`
}

/** 上次运行的一行摘要 */
export function runLine(run: FormulaMinerRun | null): string {
  if (!run) return '尚未运行'
  const d = md(run.day)
  if (run.error) return `${d} · 运行失败`
  if (run.skipped) return `${d} · ${SKIP_ZH[run.skipped] ?? '未运行'}`
  const f = run.funnel
  if (!f) return `${d} · 未运行`
  return `${d} · 生成 ${f.generated} · 新公式 ${f.new} · 可用 ${f.eligible} · 入席 ${f.admitted}`
}

function Row({ r, need }: { r: FormulaMinerRow; need: number }) {
  const thin = r.forward.verdict === 'thin'
  const state = r.status === 'selected' ? '入选' : r.status === 'retired' ? REASON_ZH[r.reason ?? ''] ?? '已退席' : VERDICT_ZH[r.forward.verdict] ?? '未定'
  const cls = r.status === 'selected' || r.forward.verdict === 'above' ? badge('status') : r.status === 'retired' || r.forward.verdict === 'below' ? badge('warn') : badge('limit')
  return (
    <div data-miner-row={r.id} data-status={r.status} data-verdict={r.forward.verdict} title={rowTitle(r, need)}
      className={`grid min-h-[36px] items-center gap-x-3 border-t border-border/70 px-3 py-1 text-[13px] first:border-t-0 ${COLS}`}>
      <span className="min-w-0 truncate text-foreground">{r.name}</span>
      <code className="min-w-0 truncate font-mono text-[12px] text-secondary max-md:hidden" title={r.formula}>{r.formula}</code>
      <span data-miner-replay className={`whitespace-nowrap text-right tabular-nums ${exCls(r.replay?.mining?.excess)}`}>{ex(r.replay?.mining?.excess)}</span>
      <span data-miner-forward className={`whitespace-nowrap text-right tabular-nums ${thin ? 'text-muted' : `font-semibold ${exCls(r.forward.excess)}`}`}>{forwardText(r, need)}</span>
      <span className="flex justify-end"><span className={cls}>{state}</span></span>
    </div>
  )
}

function Table({ title, rows, need, empty }: { title: string; rows: FormulaMinerRow[]; need: number; empty: string }) {
  return (
    <div className="overflow-hidden rounded-card border border-border">
      <div className={`grid items-center gap-x-3 border-b border-border/70 bg-elevated/40 px-3 py-1.5 text-[12px] text-muted ${COLS}`}>
        <span className="font-semibold text-foreground">{title}</span>
        <span className="max-md:hidden">公式</span>
        <span className="text-right" title="挖掘段卡回放 · 比同日随机多的 R · 只作参考 · 不参与席位排序">回放 R</span>
        <span className="text-right" title="上线后成交卡比同日同向其余成交卡多出的 R · 样本不足时写信号日进度">前向 R</span>
        <span className="text-right">状态</span>
      </div>
      {rows.length === 0 ? <div className="px-3 py-2 text-[12px] text-muted">{empty}</div> : rows.map((r) => <Row key={r.id} r={r} need={need} />)}
    </div>
  )
}

export function FormulaMinerPanel() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['factor-lab', 'miner'], queryFn: () => api.formulaMiner(), refetchInterval: (query) => (query.state.data?.running ? 5000 : false) })
  const done = (v?: FormulaMinerView) => { if (v) qc.setQueryData(['factor-lab', 'miner'], v); else qc.invalidateQueries({ queryKey: ['factor-lab', 'miner'] }) }
  const sw = useMutation({ mutationFn: (on: boolean) => api.setFormulaMiner(on), onSuccess: (v) => done(v) })
  const run = useMutation({ mutationFn: () => api.runFormulaMiner(), onSuccess: () => done() })
  const v = q.data
  if (!v) return q.isError ? <div data-miner-error className="mb-3 text-xs text-danger">{`公式挖掘读取失败 · ${(q.error as Error).message}`}</div> : null
  const err = sw.error ?? run.error
  const live = [...v.selected, ...v.bench]
  const facts: [string, string][] = [
    ['候补席', `${v.bench.length} / ${v.caps.bench}`], ['已试公式', `${v.totals.tried} 条`],
    ['入选中', `${v.totals.selected} 条`], ['已退席', `${v.totals.retired} 条`],
  ]
  return (
    <section data-miner-panel className="mb-4 rounded-card border border-border bg-surface p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-[14px] font-semibold text-foreground">公式挖掘</h3>
        <span data-miner-state className={v.enabled ? badge('status') : badge('limit')}>{v.running ? '挖掘中' : v.enabled ? '运行中' : '已关闭'}</span>
        <span data-miner-last className="min-w-0 truncate text-[12px] tabular-nums text-muted">{`上次 ${runLine(v.last_run)}`}</span>
        <button type="button" data-miner-run disabled={run.isPending || v.running || !v.pool_ready} onClick={() => run.mutate()}
          title={v.pool_ready ? '交易日 09:00–17:00（美东）不运行' : '候选池未导出'}
          className="ml-auto rounded-btn border border-border px-2.5 py-1 text-[12px] text-secondary hover:bg-elevated disabled:opacity-40 max-md:py-1.5">立即挖掘</button>
        <button type="button" role="switch" aria-checked={v.enabled} aria-label="公式挖掘" data-miner-switch disabled={sw.isPending}
          onClick={() => sw.mutate(!v.enabled)}
          className={`relative h-6 w-11 shrink-0 rounded-full border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50 max-md:h-7 max-md:w-12 ${v.enabled ? 'border-accent bg-accent dark:border-blue-700 dark:bg-blue-700' : 'border-border bg-elevated'}`}>
          <i className={`absolute top-0.5 h-[18px] w-[18px] rounded-full bg-white shadow transition-[left] max-md:h-[22px] max-md:w-[22px] ${v.enabled ? 'left-[22px]' : 'left-0.5'}`} />
        </button>
      </div>
      <div data-miner-facts className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border text-[12px] sm:grid-cols-4">
        {facts.map(([k, val]) => (
          <div key={k} className="bg-surface px-3 py-1.5">
            <div className="text-[11px] leading-4 text-muted">{k}</div>
            <div className="whitespace-nowrap text-[13px] font-semibold tabular-nums leading-5 text-foreground">{val}</div>
          </div>
        ))}
      </div>
      <div data-miner-rules className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px]">
        <span className="text-muted">规则</span>
        {['席位给选股不同的公式', '回放不参与排序', `前向满 ${v.caps.need_days} 个信号日评定`, `${v.caps.tenure_days} 个交易日未领先退席`, `每轮至多入席 ${v.caps.per_run} 条`].map((t) => <span key={t} className={badge('limit')}>{t}</span>)}
      </div>
      <div className="mt-3 grid gap-3">
        <Table title={`在席 ${live.length}`} rows={live} need={v.caps.need_days} empty="无在席公式" />
        {v.retired.length > 0 && <Table title={`已退席 ${v.totals.retired}`} rows={v.retired} need={v.caps.need_days} empty="" />}
      </div>
      {err && <div data-miner-op-error className="mt-2 text-xs text-danger">{`操作失败 · ${err instanceof ApiError ? err.message : (err as Error).message}`}</div>}
    </section>
  )
}
