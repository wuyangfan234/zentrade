import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Leaf } from 'lucide-react'
import { api, type DeskEventRadar, type DeskOverviewCard, type DeskOverviewPayload, type DeskPositionAction, type DeskSource, type DeskSourcesPayload, type DeskWatchRow, type EvidencePayload, type NewsSummary, type V3Payload, type V3SourceRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { NewsTag } from '@/components/StockNews'
import { EmptyState } from '@/components/EmptyState'
import { EvidenceSettlement } from './EvidenceSettlement'
import { SwingSettlement } from './SwingSettlement'

/**
 * 机会页 — 全系统决策台 (2026-10-02; 2026-10-05 按“能不能出手、卡在哪”重排)。
 *
 * 顶部状态条 = 全池状态; ① 可执行 = 来源当前可交易且仍在入场窗的卡 (跨日, 叠加当前许可/
 * 组合约束/账户现金); ② 最新扫描的新卡; ③ 只观察 (默认折叠, 按来源分组)。自选页 = 当前自选一票一行。
 * 页面只读: 展示用的“可执行/卡在哪”不下单, 交易仍由交易环按同一规则逐帧判定。零机会日是合法产出,
 * 但接口失败不是零机会。
 */

interface Card {
  id: string
  symbol: string
  setup_type: string
  created_date: string
  entry_zone: [number, number]
  invalidation: number
  target_zone: [number, number]
  max_holding_days: number
  risk_per_share: number
  reward_risk: number
  evidence: Record<string, unknown> & { decision?: Decision }
  narrative: string
  state: string
  state_reason: string
  side?: 'long' | 'short'
}

interface Decision {
  tab?: 'pool' | 'watchlist'
  sources?: string[]
  tradable_sources?: string[]
  tier?: 'proven' | 'explore' | 'negative'
  score?: number
  resonance?: number
  risk_pct?: number
  primary_source?: string
  direction_conflict?: boolean
  sides?: string[]
}

interface TodayPayload {
  as_of: string | null
  gate: 'green' | 'yellow' | 'unavailable' | null
  cards: Card[]
  watch: Card[]
  scanned?: number
  note?: string
}

const SETUP_LABEL: Record<string, string> = {
  breakout: 'S1 突破',
  pullback: 'S2 回踩',
}

const TIER_LABEL: Record<string, string> = { proven: '证据为正', explore: '探索单', negative: '证据为负' }

// 事件类来源没进后端 names 映射时的中文兜底 (与 services/event_sources.py EVENT_SOURCES 一致; 只用于显示)
const EVENT_SOURCE_NAMES: Record<string, string> = { earnings_beat_drift: '财报超预期后漂移', insider_cluster_buy: '内部人集中买入' }
export const sourceName = (names: Record<string, string>, id: string): string => names[id] ?? EVENT_SOURCE_NAMES[id] ?? id

function setupLabel(setup: string, names: Record<string, string>): string {
  if (setup.startsWith('strategy:')) {
    const id = setup.slice('strategy:'.length)
    return names[id] ?? id
  }
  return SETUP_LABEL[setup] ?? setup
}

const STATE_LABEL: Record<string, string> = {
  open: '待入场',
  active: '监控中',
  watch: '观察',
  filled: '已入场',
  closed: '已平仓',
  expired: '已失效',
  rejected: '已否决',
}

function fmt(n: number | undefined | null, digits = 2): string {
  if (n === undefined || n === null) return '—'
  return n.toFixed(digits)
}

const RR_SUSPECT = 10

const VERDICT_LABEL: Record<string, string> = {
  ok: '现金够', downsized: '现金不足 · 缩量成交', cash: '现金不足', max_positions: '持仓数已满',
  portfolio_risk: '组合风险超限', zero_size: '仓位为零',
}

function ExecBlock({ x }: { x: DeskOverviewCard }) {
  const cap = x.capacity
  return (
    <div data-exec className="mt-2 space-y-1 rounded border border-border/60 bg-elevated/40 px-2.5 py-1.5 text-xs">
      <div className="text-foreground">{x.explain}</div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-secondary">
        <span>{`提案日 ${x.created_date}`}</span>
        {x.window.last_day && <span>{`入场窗至 ${x.window.last_day}（剩 ${x.window.remaining} 个交易日）`}</span>}
        {cap && <span>{`计划 ${cap.planned_qty} 股 · 约需 $${Math.round(cap.need_cash).toLocaleString()} · ${VERDICT_LABEL[cap.verdict] ?? cap.verdict}${cap.verdict === 'downsized' && cap.downsized_qty ? `（约 ${cap.downsized_qty} 股）` : ''}`}</span>}
        {x.in_watchlist && <span className="text-sky-700 dark:text-sky-300">自选</span>}
        {x.held && <span>已持有</span>}
      </div>
      {x.blockers.length > 0 && (
        <div data-blockers className="flex flex-wrap gap-1">
          {x.blockers.map((b) => <span key={b} className="rounded bg-amber-500/10 px-1.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">{`卡在：${b}`}</span>)}
        </div>
      )}
    </div>
  )
}

function CardView({ card, names = {}, news, exec }: { card: Card; names?: Record<string, string>; news?: NewsSummary; exec?: DeskOverviewCard }) {
  const isWatch = !['open', 'active', 'filled'].includes(card.state)
  const d = card.evidence?.decision
  const sources = d?.sources ?? []
  const disagreement = d?.direction_conflict || new Set(d?.sides ?? []).size > 1 || (card.state_reason ?? '').includes('多空来源冲突')
  return (
    <div data-card={card.symbol} className={`rounded-lg border p-4 ${isWatch ? 'border-dashed border-border bg-elevated/40' : 'border-emerald-600/40 bg-emerald-500/[0.06] dark:border-emerald-700/50 dark:bg-emerald-950/20'}`}>
      <div className="flex items-baseline justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-lg font-semibold">{card.symbol}</span>
          <span
            data-side={card.side ?? 'long'}
            className={`rounded px-1.5 text-[11px] font-medium ${card.side === 'short' ? 'bg-rose-500/10 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300' : 'bg-emerald-500/10 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'}`}
          >
            {card.side === 'short' ? '做空' : '做多'}
          </span>
          <span className="text-xs text-secondary">{setupLabel(card.setup_type, names)}</span>
          <span className="text-xs text-muted">{STATE_LABEL[card.state] ?? card.state}</span>
          {d?.tier && <span className={`rounded px-1.5 text-[10px] ${d.tier === 'proven' ? 'bg-emerald-500/10 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300' : d.tier === 'explore' ? 'bg-amber-500/10 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' : 'bg-elevated text-secondary'}`}>{TIER_LABEL[d.tier] ?? d.tier}</span>}
        </div>
        <span className="text-xs text-secondary">
          RR {fmt(card.reward_risk, 1)}
          {card.reward_risk > RR_SUSPECT && <span data-rr-suspect className="ml-1.5 rounded bg-rose-500/10 px-1 text-[10px] text-rose-700 dark:bg-rose-900/40 dark:text-rose-300" title="RR 异常高通常是价位退化（分拆/并购钉价/复权），先核实再看">价位异常待核</span>}
        </span>
      </div>
      {exec && <ExecBlock x={exec} />}
      {sources.length > 0 && (
        <div className="mt-1 text-xs text-secondary">
          {`来源：${sources.map((s) => names[s] ?? SETUP_LABEL[s] ?? s).join('、')}`}
          {disagreement ? <span className="ml-2 inline-block whitespace-nowrap text-warning">多空分歧 · 只观察</span> : sources.length > 1 && <span className="ml-2 text-emerald-700 dark:text-emerald-400">{`${d?.resonance ?? sources.length} 个来源共振`}</span>}
          {d?.score != null && <span className="ml-2">{`综合分 ${d.score.toFixed(2)}`}</span>}
          {d?.risk_pct != null && d.risk_pct > 0 && <span className="ml-2">{`单笔风险 ${(d.risk_pct * 100).toFixed(2)}%`}</span>}
        </div>
      )}
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <div className="text-secondary">{card.side === 'short' ? '卖空区' : '入场区'} <span className="text-foreground">{fmt(card.entry_zone[0])} ~ {fmt(card.entry_zone[1])}</span></div>
        <div className="text-secondary">{card.side === 'short' ? '失效点（收盘站上确认，下一交易日开盘回补）' : '失效点'} <span className="text-rose-700 dark:text-rose-400">{fmt(card.invalidation)}</span></div>
        <div className="text-secondary">目标区 <span className="text-emerald-700 dark:text-emerald-400">{fmt(card.target_zone[0])} ~ {fmt(card.target_zone[1])}</span></div>
        <div className="text-secondary">最长持仓 <span className="text-foreground">{card.max_holding_days} 交易日</span></div>
      </div>
      {news && news.top.length > 0 && (
        <div data-card-catalyst className="mt-3 space-y-1 rounded border border-border/60 bg-elevated/40 px-2.5 py-1.5">
          <div className="text-[10px] text-muted">近期催化（48 小时 · AI 判读，仅展示，不影响入选与排序）</div>
          {news.top.map(v => (
            <div key={v.id} className="flex flex-wrap items-center gap-1.5 text-xs">
              <NewsTag item={v} />
              <span className="text-foreground">{v.headline}</span>
            </div>
          ))}
        </div>
      )}
      {card.state_reason && <p className="mt-2 text-xs text-secondary">{card.state === 'watch' ? (exec ? '扫描时观察原因：' : '创建时观察原因：') : ''}{card.state_reason}</p>}
      {card.narrative && (
        <p className="mt-3 text-sm text-foreground/90 leading-relaxed">{card.narrative}</p>
      )}
      <details className="mt-2 text-xs text-muted">
        <summary className="cursor-pointer">证据链</summary>
        <pre className="mt-1 overflow-x-auto text-secondary">{JSON.stringify(card.evidence, null, 2)}</pre>
      </details>
    </div>
  )
}

const STATUS_LABEL: Record<string, string> = { enabled: '启用', observe: '只观察', disabled: '停用' }

function pctR(n: number | null | undefined): string {
  if (n === undefined || n === null) return '—'
  return `${n > 0 ? '+' : ''}${n.toFixed(3)}R`
}

function fmtR(n: number | null | undefined): string {
  return n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(3)}R`
}

/** 新闻催化来源: 固定只观察 (无状态下拉), 只展示影子证据进度 (2026-10-02 产品决策)。 */
function NewsShadowRow({ s }: { s: DeskSource }) {
  const ev = s.news_shadow
  if (!ev) return null
  const line = (label: string, c: typeof ev.bearish_veto) =>
    `${label}：有 ${c.n_with} 例 平均 ${fmtR(c.mean_with)} · 无 ${c.n_without} 例 平均 ${fmtR(c.mean_without)}` +
    (c.diff_ci[0] != null ? ` · 差值区间 [${fmtR(c.diff_ci[0])}, ${fmtR(c.diff_ci[1])}]` : '') +
    (c.months_compared ? ` · ${c.months_compared} 个月${c.months_consistent ? '方向一致' : '方向不一'}` : '')
  return (
    <div data-source={s.id} className="rounded border border-dashed border-border px-3 py-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm text-foreground">{s.name}<span className="ml-2 text-[11px] text-muted">{s.id}</span></span>
        <span className="rounded bg-elevated px-1.5 py-0.5 text-[11px] text-secondary" title="新闻催化不参与开仓；达标后需你拍板才会启用">
          只观察 · 锁定
        </span>
      </div>
      <div className="mt-1 text-secondary">{ev.verdict}</div>
      <div className="mt-0.5 text-muted">
        {`已记录 ${ev.snapshots} 个候选快照（已结算 ${ev.resolved}，待结算 ${ev.pending}）${ev.since ? ` · 自 ${ev.since} 起` : ''}`}
      </div>
      <div className="mt-0.5 text-muted">{line('高重要度利空', ev.bearish_veto)}</div>
      <div className="mt-0.5 text-muted">{line('高重要度利好', ev.bullish_boost)}</div>
      <div className="mt-0.5 text-[11px] text-muted">{`门槛：${ev.rule}`}</div>
    </div>
  )
}

function SourceRow({ s, onStatus }: { s: DeskSource; onStatus: (id: string, v: DeskSource['status']) => void }) {
  if (s.news_shadow) return <NewsShadowRow s={s} />
  const e = s.evidence
  const b = s.by_bucket ?? {}
  return (
    <div data-source={s.id} className="rounded border border-border/60 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm text-foreground">{s.name}<span className="ml-2 text-[11px] text-muted">{s.id}</span></span>
        <label className="text-secondary">
          状态{' '}
          <select
            aria-label={`${s.name} 状态`}
            value={s.status}
            onChange={(ev) => onStatus(s.id, ev.target.value as DeskSource['status'])}
            className="rounded border border-border bg-transparent px-1 py-0.5"
          >
            {(['enabled', 'observe', 'disabled'] as const).map((v) => <option key={v} value={v}>{STATUS_LABEL[v]}</option>)}
          </select>
        </label>
      </div>
      <div className="mt-1 text-secondary">
        {e?.n ? `${e.n} 笔 · 平均 ${pctR(e.mean)} · 置信下界 ${pctR(e.ci_low)} · 胜率 ${e.win_rate != null ? `${Math.round(e.win_rate * 100)}%` : '—'}` : (e?.error ? `评估失败：${e.error}` : '尚无统一算法评估')}
      </div>
      <div className="mt-0.5 text-muted">
        {`权重 ${s.weight != null ? s.weight.toFixed(3) : '—'} · ${TIER_LABEL[s.tier ?? ''] ?? s.tier ?? '—'}${s.manual?.cap != null ? ` · 人工上限 ${s.manual.cap}` : ''} · ${s.reason ?? ''}`}
      </div>
      {(b.top100 || b.top1000 || b.watchlist) && (
        <div className="mt-0.5 text-muted">
          {`成交额前100：${b.top100?.n ?? 0} 笔 ${pctR(b.top100?.mean)} · 101–1000：${b.top1000?.n ?? 0} 笔 ${pctR(b.top1000?.mean)} · 自选：${b.watchlist?.n ?? 0} 笔 ${pctR(b.watchlist?.mean)}`}
        </div>
      )}
    </div>
  )
}

const VERSION_LABEL: Record<string, string> = { match: '版本一致', changed: '策略已改，证据过期', unknown: '版本未核', unverified: '无版本戳' }

function EvidenceCompare({ names }: { names: Record<string, string> }) {
  const qc = useQueryClient()
  const q = useQuery<EvidencePayload>({ queryKey: ['desk', 'evidence'], queryFn: () => api.deskEvidence(), refetchInterval: 60_000 })
  const run = useMutation({ mutationFn: () => api.runDeskEvidenceV2(), onSuccess: () => qc.invalidateQueries({ queryKey: ['desk', 'evidence'] }) })
  const sw = useMutation({
    mutationFn: (mode: 'v1' | 'v2') => api.setDeskEvidenceMode(mode, '机会页确认切换'),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['desk'] }); qc.invalidateQueries({ queryKey: ['opportunities'] }) },
  })
  const run3 = useMutation({ mutationFn: () => api.runDeskEvidenceV3(), onSuccess: () => qc.invalidateQueries({ queryKey: ['desk', 'evidence'] }) })
  const sw3 = useMutation({
    mutationFn: (mode: 'v1' | 'v3') => api.setDeskEvidenceMode(mode, '机会页确认切换'),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['desk'] }); qc.invalidateQueries({ queryKey: ['opportunities'] }) },
  })
  if (q.isLoading) return null
  if (q.isError || !q.data) return <div className="rounded border border-amber-500/40 p-3 text-xs text-amber-800 dark:text-amber-300">新旧证据对照读取失败</div>
  const d = q.data
  const rows = Object.entries(d.comparison)
  return (
    <>
    {d.v3 && (
      <SourceHealth
        v3={d.v3}
        mode={d.mode}
        names={names}
        running={!!d.v3_job?.running || run3.isPending}
        busy={sw3.isPending}
        onRun={() => run3.mutate()}
        onSwitch={(to) => {
          const ok = d.v3!.sources.filter((r) => r.tier === 'proven' || r.forward?.promoted).map((r) => r.name ?? sourceName(names, r.source))
          const msg = to === 'v3'
            ? `确认把可开仓判定切换到新尺子 v3.1？切换后可开仓来源：${ok.length ? ok.join('、') : '无（系统进入纯观察，前向确认通过后自动恢复）'}。已有持仓按原规则管理。`
            : '确认切回旧算法 v1？'
          if (window.confirm(msg)) sw3.mutate(to)
        }}
      />
    )}
    <details data-evidence-compare className="rounded-lg border border-border p-3 text-xs text-secondary" open={d.permission_changes.length > 0}>
      <summary className="cursor-pointer text-sm text-foreground">
        {`v1/v2 对照：现行 ${modeLabel(d.mode)}`}
        {d.v2 ? ` · 新算法 v2 已算 ${rows.filter(([, r]) => r.v2).length} 个来源${d.permission_changes.length ? `，其中 ${d.permission_changes.length} 个可开仓会变` : '，可开仓不变'}` : ' · 新算法 v2 尚未计算'}
      </summary>
      <div className="mt-2 space-y-1.5">
        <div>旧算法 v1：日线近似，目标近端全部卖出，成交日按信号日聚类。新算法 v2：目标近端卖一半、余仓保本，远端清仓；成交日开盘回落成交时不把成交前的高点算作触及目标；使用策略页保存的参数；按实际成交日聚类；带策略版本指纹。两者都是日线近似，不代表实际盈利。</div>
        <div className="flex flex-wrap items-center gap-2">
          <span>{`v1 生成 ${d.v1?.generated_at ?? '—'} · v2 生成 ${d.v2?.generated_at ?? '—'}`}</span>
          <button type="button" disabled={run.isPending || d.v2_job.running} onClick={() => run.mutate()} className="rounded border border-border px-2 py-0.5 hover:bg-elevated disabled:opacity-50">
            {d.v2_job.running ? 'v2 计算中…' : '重新计算 v2（非交易时段，约十几分钟）'}
          </button>
          {d.v2 && d.mode !== 'v4' && (
            <button
              type="button"
              data-switch-evidence
              disabled={sw.isPending}
              onClick={() => {
                const to = d.mode === 'v2' ? 'v1' : 'v2'
                const msg = to === 'v2'
                  ? `确认把可开仓判定切换到新算法 v2？${d.permission_changes.length ? `以下来源的可开仓会改变：${d.permission_changes.map((s) => sourceName(names, s)).join('、')}。` : '当前没有来源的可开仓会改变。'}已有持仓按原规则管理。`
                  : '确认切回旧算法 v1？'
                if (window.confirm(msg)) sw.mutate(to)
              }}
              className="rounded border border-emerald-600/60 px-2 py-0.5 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300 disabled:opacity-50"
            >{d.mode === 'v2' ? '切回旧算法 v1' : '确认切换到新算法 v2'}</button>
          )}
          {(run.isError || sw.isError) && <span className="text-rose-700 dark:text-rose-300">{`操作失败：${((run.error ?? sw.error) as Error)?.message ?? ''}`}</span>}
        </div>
        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="text-left text-muted"><tr><th className="py-1 pr-2">来源</th><th className="pr-2">旧算法 v1</th><th className="pr-2">新算法 v2</th><th className="pr-2">可开仓</th><th>版本</th></tr></thead>
              <tbody>
                {rows.map(([sid, r]) => (
                  <tr key={sid} data-evidence-row={sid} className={`border-t border-border/40 ${r.changes_permission ? 'text-amber-800 dark:text-amber-300' : ''}`}>
                    <td className="py-1 pr-2 text-foreground">{sourceName(names, sid)}</td>
                    <td className="pr-2">{r.v1 ? `${r.v1.n} 笔 ${pctR(r.v1.mean)} 下界 ${pctR(r.v1.ci_low)}` : '—'}</td>
                    <td className="pr-2">{r.v2 ? `${r.v2.n} 笔 ${pctR(r.v2.mean)} 下界 ${pctR(r.v2.ci_low)}${r.v2.censored ? ` · 未走完 ${r.v2.censored}` : ''}` : '—'}</td>
                    <td className="pr-2">{r.v2 ? `${TIER_LABEL[r.tier_v1 ?? ''] ?? '—'} → ${TIER_LABEL[r.tier_v2 ?? ''] ?? '—'}${r.changes_permission ? ' · 会变' : ''}` : '—'}</td>
                    <td>{r.v2 ? VERSION_LABEL[r.version] ?? r.version : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
    </>
  )
}

const URGENCY_CLS: Record<string, string> = {
  high: 'border-rose-500/50 bg-rose-500/5',
  mid: 'border-amber-500/50 bg-amber-500/5',
  low: 'border-border',
}

function signed(n: number | null | undefined, d = 2, suffix = ''): string {
  if (n == null) return '—'
  return `${n >= 0 ? '+' : ''}${n.toFixed(d)}${suffix}`
}

/** 持仓 + 下一步动作。 */
function PositionsPanel({ rows }: { rows: DeskPositionAction[] }) {
  if (!rows.length) return null
  return (
    <div data-positions className="mb-4 space-y-1.5">
      <div className="text-xs uppercase tracking-wider text-muted">{`持仓（${rows.length}）`}</div>
      {rows.map((r) => (
        <div key={r.symbol} data-position={r.symbol} className={`rounded-lg border p-2.5 text-xs ${URGENCY_CLS[r.urgency] ?? URGENCY_CLS.low}`}>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span className="text-sm font-semibold text-foreground">{r.symbol}</span>
            <span className="text-secondary">{`${r.side === 'short' ? '做空' : '做多'} ${r.qty} 股 · ${r.source_name ?? r.source ?? '—'}`}</span>
            <span className="text-secondary">{`第 ${r.held_days ?? '—'}/${r.max_days ?? '—'} 天`}</span>
            <span data-r-now title="浮动盈亏，以止损距离为 1R" className={r.r_now == null || r.r_now === 0 ? 'text-muted' : r.r_now > 0 ? 'text-bull' : 'text-bear'}>{`浮动 ${signed(r.r_now)}R`}</span>
            <span className="text-secondary">{`距止损 ${signed(r.stop_dist_pct, 1, '%')} · 距目标 ${signed(r.target_dist_pct, 1, '%')}`}</span>
            {r.mark_source === 'close' && <span className="text-muted">按最近收盘估值</span>}
          </div>
          <div className={`mt-1 ${r.urgency === 'high' ? 'font-medium text-rose-800 dark:text-rose-200' : 'text-secondary'}`}>{r.action}</div>
        </div>
      ))}
    </div>
  )
}

/** 事件雷达: 盘前跳空 + 未来 7 天财报 (持仓/在窗卡/自选)。 */
function EventRadar({ ev }: { ev: DeskEventRadar | undefined }) {
  if (!ev) return null
  const pm = ev.premarket
  const alerts = pm?.alerts ?? []
  if (!alerts.length && !ev.earnings.length) return null
  return (
    <div data-event-radar className="mb-4 rounded-lg border border-border p-3 text-xs text-secondary space-y-1">
      <div className="text-sm font-medium text-foreground">事件雷达</div>
      {alerts.length > 0 && (
        <div data-premarket>
          {`盘前跳空（${pm?.date ?? '—'}，Hyperliquid 盘前价对比前收，仅提示）：`}
          {alerts.map((a) => `${a.symbol} ${signed(a.gap_pct, 1, '%')}${a.roles?.length ? `（${a.roles.join('/')}）` : ''}`).join('；')}
        </div>
      )}
      {ev.earnings.length > 0 && (
        <div data-earnings>
          {'未来 7 天财报：'}
          {ev.earnings.map((e) => `${e.symbol} ${e.date.slice(5)}${e.timing === 'bmo' ? ' 盘前' : e.timing === 'amc' ? ' 盘后' : ''}（${e.roles.join('/')}）`).join('；')}
          <span className="text-muted">{' · 财报前一交易日不新开仓，持仓按规则减半'}</span>
        </div>
      )}
    </div>
  )
}

const V3_TIER: Record<string, string> = { proven: '可开仓', explore: '探索', negative: '只展示' }

function V3Row({ r, names }: { r: V3SourceRow; names: Record<string, string> }) {
  const f = r.forward
  const years = Object.entries(r.by_year ?? {})
  return (
    <details data-v3-row={r.source} className="rounded border border-border/60 p-2">
      <summary className="cursor-pointer">
        <span className="text-foreground">{r.name ?? sourceName(names, r.source)}</span>
        {r.kind === 'event' && <span className="ml-1 rounded bg-sky-500/10 px-1 text-sky-700 dark:text-sky-300">事件</span>}
        <span className="ml-2">{`历史 ${r.n} 笔 ${signed(r.mean, 3)}R · 比随机 ${signed(r.excess?.mean, 3)}R · 2025 起 ${signed(r.holdout?.mean, 3)}R`}</span>
        <span className={`ml-2 ${r.tier === 'proven' || f?.promoted ? 'text-emerald-700 dark:text-emerald-300' : 'text-muted'}`}>
          {f?.promoted ? '前向确认通过 · 可开仓' : V3_TIER[r.tier] ?? r.tier}
        </span>
      </summary>
      <div className="mt-1.5 space-y-0.5 pl-1">
        <div>{r.reason}</div>
        <div>{`三套成绩：历史回放 ${r.n} 笔 ${signed(r.mean, 3)}R（随机挑选 ${signed(r.baseline, 3)}R） · 影子前向 ${r.shadow ? `${r.shadow.closed}/${r.shadow.snapshots} 已结算${r.shadow.mean_r != null ? ` ${signed(r.shadow.mean_r, 3)}R` : ''}${r.shadow.excess != null ? `，比随机 ${signed(r.shadow.excess, 3)}R` : ''}` : '尚无'} · 真实纸面 ${r.real ? `开 ${r.real.opened} 平 ${r.real.closed}${r.real.mean_r != null ? ` ${signed(r.real.mean_r, 2)}R` : ''}` : '0 笔'}`}</div>
        <div>{`超额区间 ${signed(r.excess?.ci_low, 3)} ~ ${signed(r.excess?.ci_high, 3)}R，单侧 p=${r.excess?.p ?? '—'}${r.holm_pass ? '（过多重检验）' : '（未过多重检验）'} · 发现期 ${signed(r.discovery?.mean, 3)}R / 留出期 ${signed(r.holdout?.mean, 3)}R（${r.holdout?.n ?? 0} 笔）`}</div>
        {years.length > 0 && <div>{`逐年超额：${years.map(([y, v]) => `${y} ${signed(v.mean_excess, 3)}`).join(' · ')}`}</div>}
        {r.concentration?.drop_top_20 != null && <div>{`去掉最大 20 笔赢家后平均 ${signed(r.concentration.drop_top_20, 3)}R${r.delisted_trades ? ` · 含已退市股票 ${r.delisted_trades} 笔` : ''}`}</div>}
        {f?.candidate && <div data-forward className="text-sky-800 dark:text-sky-300">{`前向确认进度：${f.progress_text}`}</div>}
      </div>
    </details>
  )
}

/** 来源体检板 (v3.1): 每个来源三套成绩 + 与随机挑选对比 + 前向确认进度。 */
function SourceHealth({ v3, mode, names, running, onRun, onSwitch, busy }: {
  v3: V3Payload; mode: string; names: Record<string, string>; running: boolean; onRun: () => void; onSwitch: (to: 'v1' | 'v3') => void; busy: boolean
}) {
  const ok = v3.sources.filter((r) => r.tier === 'proven' || r.forward?.promoted)
  return (
    <div data-source-health className="rounded-lg border border-border p-3 text-xs text-secondary space-y-2">
      <div className="text-sm font-medium text-foreground">{`来源体检（新尺子 v3.1）· 可开仓 ${ok.length}/${v3.sources.length}`}</div>
      <div>
        {`同一股票池、同一天、同一套 7 日规则下随机挑股票作为“零点”；2025 年起为留出期；${v3.with_delisted === false ? '本次未含已退市股票（研究数据未就绪）' : '含已退市股票'}；同时检验 ${v3.hypotheses_tested ?? v3.sources.length} 个来源，按多重检验校正。`}
        {`生成于 ${v3.generated_at ?? '—'}，窗口 ${v3.window?.join(' ~ ')}。`}
      </div>
      <div>{`前向确认：${v3.forward_rule.start} 起影子账本里信号产生时未来未知的样本，满 ${v3.forward_rule.min_n} 笔、${v3.forward_rule.min_months} 个月且显著跑赢随机，自动转为可开仓。候选：${v3.forward_rule.candidates.map((s) => sourceName(names, s)).join('、') || '无'}。`}</div>
      <div className="flex flex-wrap items-center gap-2">
        <span>{`现行算法：${modeLabel(mode)}`}</span>
        <button type="button" disabled={running} onClick={onRun} className="rounded border border-border px-2 py-0.5 hover:bg-elevated disabled:opacity-50">
          {running ? 'v3 计算中…' : '重新计算 v3（非交易时段）'}
        </button>
        {mode !== 'v4' && <button
          type="button"
          data-switch-v3
          disabled={busy}
          onClick={() => onSwitch(mode === 'v3' ? 'v1' : 'v3')}
          className="rounded border border-emerald-600/60 px-2 py-0.5 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300 disabled:opacity-50"
        >{mode === 'v3' ? '切回旧算法 v1' : '切换到新尺子 v3.1'}</button>}
      </div>
      <div className="space-y-1">{v3.sources.map((r) => <V3Row key={r.source} r={r} names={names} />)}</div>
    </div>
  )
}

function SourcesPanel() {
  const qc = useQueryClient()
  const q = useQuery<DeskSourcesPayload>({ queryKey: ['desk', 'sources'], queryFn: () => api.deskSources(), refetchInterval: 300_000 })
  const set = useMutation({
    mutationFn: ({ id, status }: { id: string; status: DeskSource['status'] }) => api.setDeskSourceStatus(id, status, '机会页手动调整'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['desk', 'sources'] }),
  })
  if (q.isLoading) return <div className="p-4 text-secondary">加载中…</div>
  if (q.isError || !q.data) return <div className="p-4 text-warning">来源列表读取失败</div>
  const ev = q.data.evaluation ?? {}
  const srcNames = Object.fromEntries(q.data.sources.map((s) => [s.id, s.name]))
  return (
    <div className="space-y-2">
      <EvidenceCompare names={srcNames} />
      <div className="rounded-lg border border-border p-3 text-xs text-secondary space-y-0.5">
        <div>{`统一算法评估：${ev.window?.join(' ~ ') ?? '—'} · ${ev.universe ?? ''} · 生成于 ${ev.generated_at ?? '—'}`}</div>
        {Object.values(q.data.rules ?? {}).map((r) => <div key={r}>{r}</div>)}
      </div>
      {q.data.sources.map((s) => (
        <SourceRow
          key={s.id}
          s={s}
          onStatus={(id, status) => {
            if (window.confirm(`确认把「${s.name}」改为「${STATUS_LABEL[status]}」？`)) set.mutate({ id, status })
          }}
        />
      ))}
    </div>
  )
}

function fmtMoney(n: number | null | undefined): string {
  if (n == null) return '—'
  return `$${Math.round(n).toLocaleString()}`
}

function StatusBar({ q }: { q: { data?: DeskOverviewPayload; isLoading: boolean; isError: boolean; error: unknown } }) {
  if (q.isLoading) return <div data-status-bar className="mb-4 rounded-lg border border-border p-3 text-xs text-secondary">总览加载中…</div>
  if (q.isError || !q.data) {
    return (
      <div data-status-bar data-error className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-300">
        {`“能不能出手”总览读取失败${q.error instanceof Error ? `（${q.error.message}）` : ''}：下方卡片仍可看，但跨日可执行卡与账户容量暂不可见，不代表没有机会。`}
      </div>
    )
  }
  const st = q.data.status
  const acc = st.account
  const cb = st.circuit_breaker as { block_new_entries?: boolean; risk_multiplier?: number } | null
  const crowd = Object.entries(st.crowding ?? {})
  return (
    <div data-status-bar className="mb-4 space-y-1.5 rounded-lg border border-border p-3 text-xs text-secondary">
      <div className="text-sm font-medium text-foreground">全池状态</div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        <span>
          {'可交易来源 '}
          <span className="text-foreground">{`${st.tradable_sources.length}/${st.evaluated_sources}`}</span>
          {st.tradable_sources.length > 0 && `（${st.tradable_sources.map((s) => s.name).join('、')}）`}
        </span>
        <span>{'在窗可执行卡 '}<span className="text-foreground">{st.executable}</span>{st.executable > 0 ? `（无阻断 ${st.executable_clear}）` : ''}</span>
        <span>{`大盘 ${st.gate === 'green' ? '绿灯' : st.gate === 'yellow' ? '黄灯（减量）' : st.gate === 'unavailable' ? '数据不足（不开新仓）' : '—'}`}</span>
        <span>{`熔断 ${cb == null ? '未评估（本次运行尚未进入交易时段）' : cb.block_new_entries ? '当日停开新仓' : (cb.risk_multiplier ?? 1) < 1 ? `半风险 ×${cb.risk_multiplier}` : '允许开仓'}`}</span>
        <span>{st.in_session ? `报价 ${st.last_quote_at ? st.last_quote_at.slice(11, 19) + ' ET' : '尚无'}` : '非交易时段'}</span>
      </div>
      {acc && (
        <div data-account className="flex flex-wrap gap-x-4 gap-y-1">
          <span>{'现金 '}<span className="text-foreground">{fmtMoney(acc.buying_power)}</span>{` / 净值 ${fmtMoney(acc.nav)}`}</span>
          <span title="当前持仓数 / 持仓上限">{`持仓 ${acc.positions}/${acc.max_positions}`}</span>
          <span>{`组合风险 ${acc.open_risk_pct ?? '—'}% / ${acc.max_portfolio_risk_pct}%`}</span>
          <span className="text-muted">{`估值：${acc.marks_origin?.includes('close') ? '含最近收盘价' : '实时报价'}${acc.unmarked.length ? `；${acc.unmarked.join('、')} 无价按成本` : ''}`}</span>
        </div>
      )}
      {crowd.length > 0 && (
        <div data-crowding className="text-amber-800 dark:text-amber-300">
          {`同主题扎堆（最近一次扫描的可交易候选）：${crowd.map(([k, v]) => `${k} ${v.length} 只（${v.slice(0, 6).join('、')}${v.length > 6 ? '…' : ''}）`).join('；')} · 行业上限会拦掉其中多数`}
        </div>
      )}
      {q.data.evidence && (
        <div data-evidence-brief className="text-secondary">
          {`算法 ${q.data.evidence.mode === 'v3' ? '新尺子 v3.1（随机基线 + 留出期 + 退市股 + 多重检验）' : modeLabel(q.data.evidence.mode)}`}
          {q.data.evidence.forward.length > 0 && ` · 前向确认：${q.data.evidence.forward.map((f) => `${sourceName(q.data!.names, f.source)} ${f.n}/${q.data!.evidence!.forward_min_n ?? 300} 笔${f.promoted ? ' 已通过' : ''}`).join('，')}`}
        </div>
      )}
      <div className="text-[11px] text-muted">{`最近扫描 ${st.latest_scan ?? '—'} · “可执行”仅表示按现行规则未被规则/约束挡住，是否成交由交易时段内的报价与保护逐帧判定`}</div>
    </div>
  )
}

const STAGE_LABEL: Record<string, string> = {
  pool: '未进流动性池', no_signal: '无来源命中', card_build: '成卡未通过', card: '已成卡', pending: '待下次扫描', unrecorded: '未记录',
}

function hitText(h: DeskWatchRow['hits'][number], names: Record<string, string>): string {
  const n = names[h.source] ?? SETUP_LABEL[h.source] ?? sourceName(names, h.source)
  const rank = h.rank != null ? ` 第${h.rank}${h.total ? `/${h.total}` : ''}名` : ''
  const via = h.via === 'watchlist' ? '·自选通道' : h.via === 'pool_top' ? '·全池前10' : ''
  return `${n}${rank}${via}`
}

function WatchlistTable({ data, names }: { data: DeskOverviewPayload['watchlist']; names: Record<string, string> }) {
  if (!data.ok) return <div className="rounded border border-amber-500/40 p-3 text-xs text-amber-800 dark:text-amber-300">自选名单读取失败，无法列出逐票状态。</div>
  if (data.count === 0) return <EmptyState icon={Leaf} title="自选为空" hint="在自选页添加股票后，这里逐票显示被哪些来源选中、卡在哪一步。" />
  return (
    <div data-watchlist-table className="space-y-1">
      <div className="text-xs text-muted">
        {`当前自选 ${data.count} 只 · 原因来自 ${data.scan_day ?? '—'} 盘后扫描`}
        {data.removed_since_scan.length > 0 && ` · 扫描后已移出：${data.removed_since_scan.join('、')}（其自选通道卡不再新开）`}
      </div>
      <div className="overflow-x-auto rounded border border-border/60">
        <table className="w-full text-xs">
          <thead className="bg-elevated/60 text-left text-secondary">
            <tr><th className="px-2 py-1.5">代码</th><th className="px-2 py-1.5">卡 / 持仓</th><th className="px-2 py-1.5">来源命中</th><th className="px-2 py-1.5">卡在哪</th></tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.symbol} data-watch-row={r.symbol} className="border-t border-border/40 align-top">
                <td className="px-2 py-1.5 font-medium text-foreground">{r.symbol}</td>
                <td className="px-2 py-1.5 text-secondary">
                  {r.held && <div className="text-foreground">已持有</div>}
                  {r.card ? <div>{`${r.card.state_label ?? r.card.state} · ${r.card.created_date}${r.card.tradable ? '' : ' · 来源不可交易'}`}</div> : !r.held && <div className="text-muted">无进行中卡</div>}
                </td>
                <td className="px-2 py-1.5 text-secondary">{r.hits.length ? r.hits.map((h) => hitText(h, names)).join('；') : <span className="text-muted">—</span>}</td>
                <td className="px-2 py-1.5 text-secondary">
                  <span className="mr-1 rounded bg-elevated px-1 text-[10px]">{STAGE_LABEL[r.stage ?? ''] ?? r.stage ?? '—'}</span>
                  {r.card && r.stage === 'card' ? (r.card.state_reason || r.reason) : r.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function primarySource(c: Card): string {
  const d = c.evidence?.decision
  return d?.primary_source ?? (c.setup_type.startsWith('strategy:') ? c.setup_type.slice(9) : c.setup_type)
}

function WatchGroups({ watch, names, newsBy }: { watch: Card[]; names: Record<string, string>; newsBy: Record<string, NewsSummary> }) {
  const groups = new Map<string, Card[]>()
  for (const c of watch) {
    const k = primarySource(c)
    groups.set(k, [...(groups.get(k) ?? []), c])
  }
  const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length)
  return (
    <div data-watch-groups className="space-y-1.5">
      <div className="pt-4 text-xs uppercase tracking-wider text-muted">{`只观察（${watch.length}）· 证据不足 / 组合约束 / 只观察来源，只展示不开仓`}</div>
      {ordered.map(([src, cs]) => {
        const tier = cs[0].evidence?.decision?.tier
        return (
          <details key={src} data-watch-group={src} className="rounded border border-dashed border-border/70 px-3 py-1.5 text-sm">
            <summary className="cursor-pointer text-secondary">{`${names[src] ?? SETUP_LABEL[src] ?? src} ×${cs.length}${tier ? ` · ${TIER_LABEL[tier] ?? tier}` : ''} · ${cs.slice(0, 8).map((c) => c.symbol).join(' ')}${cs.length > 8 ? ' …' : ''}`}</summary>
            <div className="mt-2 space-y-3 pb-1">{cs.map((c) => <CardView key={c.id} card={c} names={names} news={newsBy[c.symbol]} />)}</div>
          </details>
        )
      })}
    </div>
  )
}

const NEWS_BATCH = 100

/** 许可算法的显示名。v4 = 自选机会 (规则集 + 同票基线前向确认)。 */
export function modeLabel(mode?: string | null): string {
  return mode === 'v4' ? '自选 v4（规则集 + 前向确认）' : mode === 'v3' ? '新尺子 v3.1' : mode === 'v2' ? '新算法 v2' : '旧算法 v1'
}

export function EvidenceDesk() {
  const [tab, setTab] = useState<'pool' | 'watchlist' | 'sources' | 'settle'>('pool')
  const today = useQuery<TodayPayload>({
    queryKey: ['opportunities', 'today'],
    queryFn: () => api.opportunitiesToday() as unknown as Promise<TodayPayload>,
    refetchInterval: 60_000,
  })
  const data = today.data
  const overview = useQuery<DeskOverviewPayload>({
    queryKey: ['opportunities', 'desk-overview'],
    queryFn: () => api.deskOverview(),
    refetchInterval: 60_000,
  })
  const sources = useQuery<DeskSourcesPayload>({ queryKey: ['desk', 'sources'], queryFn: () => api.deskSources(), refetchInterval: 300_000 })
  // 结构不对 (旧后端/代理错误页) 按“总览不可用”处理, 不让整页崩
  const ov = overview.data && overview.data.status && Array.isArray(overview.data.executable) && overview.data.watchlist
    ? overview.data : undefined
  const overviewState = { data: ov, isLoading: overview.isLoading, isError: overview.isError || (!overview.isLoading && !ov), error: overview.error }
  const executable = useMemo(() => ov?.executable ?? [], [ov])
  const cardSymbols = useMemo(
    () => Array.from(new Set([...(data?.cards ?? []), ...(data?.watch ?? []), ...executable].map(c => c.symbol))).sort(),
    [data?.cards, data?.watch, executable],
  )
  const newsQ = useQuery({
    queryKey: QK.newsSummary(cardSymbols.join(',')),
    // 后端单次最多 100 只: 分批请求再合并, 不让第 101 只之后的卡静默丢新闻
    queryFn: async () => {
      const batches: string[][] = []
      for (let i = 0; i < cardSymbols.length; i += NEWS_BATCH) batches.push(cardSymbols.slice(i, i + NEWS_BATCH))
      const parts = await Promise.all(batches.map((b) => api.newsSummary(b)))
      return { summaries: Object.assign({}, ...parts.map((p) => p.summaries ?? {})) as Record<string, NewsSummary> }
    },
    enabled: cardSymbols.length > 0,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  })
  const newsBy = newsQ.data?.summaries ?? {}
  const names = useMemo(
    () => ({ ...(ov?.names ?? {}), ...Object.fromEntries((sources.data?.sources ?? []).map((s) => [s.id, s.name])) }),
    [sources.data, ov],
  )

  if (today.isLoading) {
    return <div className="p-8 text-secondary">加载中…</div>
  }

  const tabOf = (c: Card) => (c.evidence?.decision?.tab === 'watchlist' ? 'watchlist' : 'pool')
  const execIds = new Set(executable.map((x) => x.id))
  const allCards = data?.cards ?? []
  const live = allCards.filter(c => ['open', 'active', 'filled'].includes(c.state) && !execIds.has(c.id))
  const terminal = allCards.filter(c => !['open', 'active', 'filled'].includes(c.state))
  const watchAll = (data?.watch ?? []).filter((c) => !execIds.has(c.id))
  const view = tab === 'sources' || tab === 'settle' ? 'pool' : tab
  const exec = view === 'watchlist' ? executable.filter((x) => x.in_watchlist) : executable
  const cards = live.filter(c => tabOf(c) === view)
  const watch = watchAll.filter(c => tabOf(c) === view)
  const done = terminal.filter(c => tabOf(c) === view)
  const failed = today.isError
  const empty = !failed && exec.length === 0 && cards.length === 0
  const wlCount = ov?.watchlist.count

  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-3 text-xs text-secondary">
        {data?.as_of ? `${data.as_of} · ${data.gate === 'green' ? 'SPY 200 日线上' : data.gate === 'yellow' ? 'SPY 200 日线下' : 'SPY 数据不足'}${data?.scanned ? ` · ${data.scanned} 只` : ''}` : ''}
      </div>

      <StatusBar q={overviewState} />
      {ov?.positions && <PositionsPanel rows={ov.positions} />}
      <EventRadar ev={ov?.events} />

      <div role="tablist" className="mb-4 flex gap-1 border-b border-border text-sm">
        {([['pool', `全股票池 (${executable.length})`], ['watchlist', `自选股 (${wlCount ?? '—'})`], ['sources', '来源管理'], ['settle', '卡结局']] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`px-3 py-1.5 ${tab === id ? 'border-b-2 border-emerald-500 text-foreground' : 'text-secondary hover:text-foreground'}`}
          >{label}</button>
        ))}
      </div>

      {tab === 'settle' ? (
        <div className="space-y-4">
          <SwingSettlement />
          <details data-legacy-settle className="text-xs">
            <summary className="cursor-pointer text-muted">旧卡结局（宽卡 7 日，只作参考）</summary>
            <div className="mt-2"><EvidenceSettlement /></div>
          </details>
        </div>
      ) : tab === 'sources' ? <SourcesPanel /> : (
        <>
          {failed && (
            <div data-load-error className="mb-3 rounded-lg border border-rose-500/40 bg-rose-500/5 p-3 text-sm text-rose-700 dark:text-rose-300">
              {`机会数据读取失败${today.error instanceof Error ? `（${today.error.message}）` : ''}${data ? `，以下为 ${new Date(today.dataUpdatedAt).toLocaleTimeString()} 上次成功的数据` : '，这不是“零机会”'}`}
            </div>
          )}
          {view === 'watchlist' && ov && <div className="mb-4"><WatchlistTable data={ov.watchlist} names={names} /></div>}
          {empty && (
            <EmptyState
              icon={Leaf}
              title="暂无可入场机会"
              hint={data?.note ?? (view === 'watchlist' ? '今天没有自选股处在可执行状态，逐票原因见上表。零机会是合法产出。' : '零机会是合法产出: 无即是有，不为出手而出手。')}
            />
          )}

          <div className="space-y-3">
            {exec.length > 0 && <div data-exec-section className="text-xs uppercase tracking-wider text-muted">{`① 可执行（来源当前可交易 · 仍在入场窗）· ${exec.length}`}</div>}
            {exec.map((x) => <CardView key={x.id} card={x as unknown as Card} names={names} news={newsBy[x.symbol]} exec={x} />)}
            {cards.length > 0 && <div className="pt-2 text-xs uppercase tracking-wider text-muted">② 最新扫描的新卡</div>}
            {cards.map((c) => <CardView key={c.id} card={c} names={names} news={newsBy[c.symbol]} />)}
          </div>
          {watch.length > 0 && <WatchGroups watch={watch} names={names} newsBy={newsBy} />}
          {newsQ.isError && <div className="mt-3 text-xs text-muted">新闻摘要读取失败：卡片上的“近期催化”暂不显示，不代表没有新闻。</div>}
          {done.length > 0 && (
            <details data-terminal-history className="mt-5 text-sm text-muted">
              <summary className="cursor-pointer">已结束提案（{done.length}）· 不再参与入场</summary>
              <div className="mt-3 space-y-3">{done.map(c => <CardView key={c.id} card={c} names={names} />)}</div>
            </details>
          )}
        </>
      )}
    </div>
  )
}
