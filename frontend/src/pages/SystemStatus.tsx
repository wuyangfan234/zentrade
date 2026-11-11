import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, Clock, ListChecks, XCircle } from 'lucide-react'
import {
  api, type SystemStatusComponent, type SystemStatusJob, type SystemStatusMarket, type SystemStatusPayload,
} from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'
import { EntryFunnelTable } from '@/components/EntryFunnelTable'
import { SectionTitle } from '@/components/data/SectionTitle'
import { LOCAL_TZ, LOCAL_TZ_LABEL } from '@/lib/localTime'

/**
 * 系统状态 — 0.7.1: 首屏一句总结论 (美东时段 + 是否交易日), 红/黄/绿/灰四态;
 * 异常与注意卡展开写 原因 / 影响 / 下一步 / 最后正常时间。只读; 30 秒刷新。
 * 0.9.1: 宽屏两栏; 正常/等待项默认展开为卡片网格; 问题卡左色条; 读取失败可重试。手机单栏不变。
 * 0.13.0: 与数据页 / 持仓页统一版式 — 页宽 max-w-6xl、px-4 md:px-8; 白底卡 (rounded-card border bg-surface);
 * 分区标题 = 图标 + 小字标题 (SectionTitle); 总结论卡带四格计数; 定时任务收进卡片、按下次时间排序;
 * 准入漏斗移到底部整宽卡。只改展示, 不改接口。
 */

const STATE_STYLE: Record<string, { dot: string; label: string; edge: string; pill: string }> = {
  ok: { dot: 'bg-emerald-500', label: '正常', edge: 'border-l-emerald-500', pill: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400' },
  warn: { dot: 'bg-amber-400', label: '注意', edge: 'border-l-amber-400', pill: 'bg-amber-400/15 text-amber-700 dark:text-amber-400' },
  error: { dot: 'bg-rose-500', label: '异常', edge: 'border-l-rose-500', pill: 'bg-rose-500/10 text-rose-700 dark:text-rose-400' },
  idle: { dot: 'bg-muted', label: '未运行', edge: 'border-l-border', pill: 'bg-elevated text-secondary' },
}
const IDLE_LABEL: Record<string, string> = { waiting: '未到运行时间', closed: '休市', disabled: '已停用' }

const PHASE_LABEL: Record<SystemStatusMarket['phase'], string> = {
  premarket: '盘前', session: '盘中', postmarket: '盘后', closed: '休市',
}

export const JOB_LABEL: Record<string, string> = {
  daily_pipeline: '盘后同步 · 16:35 ET',
  daily_pipeline_catchup: '盘后同步补跑 · 启动后一次',
  daily_pipeline_retry: '盘后同步重试 · 每 15 分钟，至多 4 次',
  pre_market_instruments: '盘前标的列表',
  zen_paper_tick: '模拟撮合 · 15 秒',
  zen_hot_quotes: '实时报价 · 5 秒',
  zen_news: '个股新闻 · 盘前盘中 5 分钟，其余 30 分钟',
  zen_intel: '情报刷新 · 每分钟',
  zen_qveris_estimates: '财报预期查询 · 08:45 ET',
  zen_alpaca_mirror: 'Alpaca 镜像 · 15 秒',
  zen_premarket_gap: '盘前跳空预警 · 09:00 ET',
  zen_swing_pm_check: '盘前越止损检查 · 09:20 / 09:30 / 09:40 ET',
  zen_intraday_strength: '盘中强势检查 · 10:00–15:30 ET 每 30 分钟',
  zen_intraday_strength_settle: '盘中强势结算 · 16:25 ET',
  zen_open_radar: '开盘放量榜与推送 · 09:51 ET',
  zen_open_radar_settle: '开盘放量榜收盘记录 · 16:40 / 17:40 ET',
  zen_swing_refresh: '机会板刷新 · 北京 20:00 起每 30 分钟',
  zen_radar_frame: '机会页状态探测 · 15 秒',
  zen_swing_candidates_queue: '挖掘 / AI 策略回放 · 17:05 ET',
  zen_factor_intake: '公开因子采集 · 每 6 小时',
  zen_factor_discover: '每周因子挖掘 · 周五 17:40 ET',
  zen_factor_discover_retry: '每周因子挖掘补跑 · 周六 10:30 ET',
  zen_weekly_strategy_evaluation: '策略检验周更 · 周六 10:00 ET',
  zen_weekly_review: '周报 · 周日 09:00 ET',
  zen_ops_check: '运行巡检 · 3 分钟',
  zen_market_mood: '大盘判断 · 08:35 ET',
  zen_corporate_actions: '公司行动',
  zen_earnings_premarket: '财报日历 · 盘前',
  zen_earnings_postclose: '财报日历 · 盘后',
  zen_market_mood_score: '大盘判断记分',
  zen_ls_rank: '今日多空名单补生成 · 08:20 / 17:20 ET',
  zen_market_mood_score_retry: '大盘判断补记分',
  zen_fundamentals_sync: '财务数据同步 · 18:30 ET',
  zen_notification_outbox: '通知发送 · 60 秒',
  zen_resonance_shadow: '信号共振记录 · 盘中每 5 分钟',
  zen_resonance_settle: '信号共振结算 · 16:45 ET',
  zen_rank_lab: '排序因子复核 · 17:40 ET',
  zen_protection_boot_refresh: '启动后刷新财报与公司行动',
  reprobe_capabilities: '数据源能力复检 · 每 60 分钟',
}

const JOB_STATE: Record<string, { icon: string; label: string; cls: string }> = {
  ok: { icon: '✓', label: '已完成', cls: 'text-emerald-700 dark:text-emerald-400' },
  running: { icon: '…', label: '进行中', cls: 'text-accent' },
  pending: { icon: '…', label: '待运行', cls: 'text-accent' },
  missing: { icon: '✗', label: '未运行', cls: 'text-rose-700 dark:text-rose-400' },
  error: { icon: '✗', label: '失败', cls: 'text-rose-700 dark:text-rose-400' },
}

const CARD = 'rounded-card border border-border bg-surface'
const WRAP = 'mx-auto max-w-6xl space-y-6 px-4 py-4 md:px-8 md:py-6'

/** "10:42 ET（北京 22:42）" */
export function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const f = (tz: string) => d.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
  const day = (tz: string) => d.toLocaleDateString('en-CA', { timeZone: tz })
  const next = day(LOCAL_TZ) > day('America/New_York') ? '次日' : ''
  return `${f('America/New_York')} ET（${LOCAL_TZ_LABEL}${next} ${f(LOCAL_TZ)}）`
}

/** 定时任务下次时间拆两行: 主行 "10-13 09:10 ET", 次行 "北京 21:10" / "北京次日 04:35" */
export function fmtNextRun(iso?: string | null): { et: string; local: string } | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return { et: iso, local: '' }
  const f = (tz: string) => d.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
  const day = (tz: string) => d.toLocaleDateString('en-CA', { timeZone: tz })
  const next = day(LOCAL_TZ) > day('America/New_York') ? '次日' : ''
  return { et: `${day('America/New_York').slice(5)} ${f('America/New_York')} ET`, local: `${LOCAL_TZ_LABEL}${next} ${f(LOCAL_TZ)}` }
}

function stateLabel(c: SystemStatusComponent) {
  if (c.state === 'idle') return IDLE_LABEL[c.idle_kind ?? 'waiting'] ?? STATE_STYLE.idle.label
  return (STATE_STYLE[c.state] ?? STATE_STYLE.idle).label
}

function StatePill({ c }: { c: SystemStatusComponent }) {
  const st = STATE_STYLE[c.state] ?? STATE_STYLE.idle
  return (
    <span data-state-pill className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-px text-[11px] font-medium leading-4 ${st.pill}`}>
      <span className={`inline-block h-1.5 w-1.5 rounded-full ${st.dot}`} aria-hidden="true" />
      {stateLabel(c)}
    </span>
  )
}

function Stat({ label, value, tone, id }: { label: string; value: number; tone: string; id: string }) {
  return (
    <div data-stat={id} className="min-w-0 rounded-btn border border-border bg-base/40 px-3 py-1.5">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 tabular-nums text-[14px] font-semibold leading-6 ${value ? tone : 'text-muted'}`}>{value}</div>
    </div>
  )
}

function Verdict({ comps, market, at, noProblems }: { comps: SystemStatusComponent[]; market?: SystemStatusMarket; at: string; noProblems: boolean }) {
  const n = (s: string) => comps.filter((c) => c.state === s).length
  const err = n('error'); const warn = n('warn')
  const kind = err ? 'error' : warn ? 'warn' : 'ok'
  const head = err
    ? `${err} 项异常${warn ? ` · ${warn} 项注意` : ''}`
    : warn ? `${warn} 项注意`
      : market?.phase === 'closed' ? '休市 · 运行正常' : '运行正常'
  const Icon = err ? XCircle : warn ? AlertTriangle : CheckCircle2
  const iconCls = err ? 'text-rose-600 dark:text-rose-400' : warn ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'
  return (
    <div data-verdict={kind} className={`${CARD} border-l-[3px] ${STATE_STYLE[kind].edge} p-4`}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${iconCls}`} aria-hidden="true" />
          <div className="min-w-0">
            <div className="text-[14px] font-semibold leading-6 text-foreground">{head}</div>
            {market && (
              <div data-market className="mt-1 text-xs text-secondary">
                {`${PHASE_LABEL[market.phase]} · ${market.trading_day ? '交易日' : '非交易日'}`}
                {market.phase !== 'session' && ` · 下次开盘 ${market.next_open.slice(5, 10)} ${fmtTime(market.next_open)}`}
                {market.phase === 'session' && market.close && ` · 收盘 ${fmtTime(market.close)}`}
              </div>
            )}
            <div className="mt-0.5 text-xs text-muted">{`更新于 ${fmtTime(at)} · 每 30 秒刷新`}</div>
            {noProblems && <div data-no-problems className="mt-1.5 text-xs text-emerald-700 dark:text-emerald-400">无待处理项</div>}
          </div>
        </div>
        <div data-verdict-stats className="grid shrink-0 grid-cols-4 gap-2 lg:w-[22rem]">
          <Stat id="error" label="异常" value={err} tone="text-rose-700 dark:text-rose-400" />
          <Stat id="warn" label="注意" value={warn} tone="text-amber-700 dark:text-amber-400" />
          <Stat id="ok" label="正常" value={n('ok')} tone="text-emerald-700 dark:text-emerald-400" />
          <Stat id="idle" label="未运行" value={n('idle')} tone="text-foreground" />
        </div>
      </div>
    </div>
  )
}

/** 部件 → 能看到这项数据的页面。文件路径与接口地址不上屏, 只放在悬停里。 */
const SOURCE_LINK: Record<string, { label: string; to: string }> = {
  execution_quotes: { label: '设置 › 数据源', to: '/settings?tab=data-sources' },
  realtime_source: { label: '设置 › 数据源', to: '/settings?tab=data-sources' },
  qveris: { label: '设置 › 数据源', to: '/settings?tab=data-sources' },
  notifications: { label: '设置 › 实时监控', to: '/settings?tab=monitoring' },
  hot_quotes: { label: '持仓', to: '/paper' },
  paper_loop: { label: '持仓', to: '/paper' },
  circuit_breaker: { label: '持仓', to: '/paper' },
  alpaca_mirror: { label: '持仓', to: '/paper' },
  earnings: { label: '持仓', to: '/paper' },
  corporate_actions: { label: '持仓', to: '/paper' },
  premarket_gap: { label: '机会', to: '/' },
  intraday_strength: { label: '机会', to: '/' },
  open_radar: { label: '机会', to: '/' },
  opp_radar: { label: '机会', to: '/' },
  decision_desk: { label: '回测 › 证据', to: '/backtest?tab=evidence' },
  evidence: { label: '回测 › 证据', to: '/backtest?tab=evidence' },
  news: { label: '情报', to: '/intel' },
  factor_intake: { label: '因子 › 采集', to: '/factors?tab=discover' },
  factor_discover: { label: '因子', to: '/factors' },
  fundamentals: { label: '数据', to: '/data' },
  daily_pipeline: { label: '数据', to: '/data' },
  data_freshness: { label: '数据', to: '/data' },
  data_quality: { label: '数据', to: '/data' },
}

function Source({ c }: { c: SystemStatusComponent }) {
  const link = SOURCE_LINK[c.id]
  if (!link) return null
  return (
    <div data-source className="mt-auto border-t border-border/60 pt-1.5 text-[11px] leading-4">
      <Link to={link.to} title={c.source || undefined} className="text-accent hover:underline">{link.label}</Link>
    </div>
  )
}

function ProblemCard({ c }: { c: SystemStatusComponent }) {
  const st = STATE_STYLE[c.state]
  return (
    <div data-component={c.id} className={`${CARD} flex flex-col border-l-[3px] ${st.edge} p-3`}>
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 break-words text-sm font-medium text-foreground">{c.label}</span>
        <StatePill c={c} />
      </div>
      <dl className="mb-2 mt-2 grid grid-cols-[4.5em_1fr] gap-x-2 gap-y-1 break-words text-xs">
        <dt className="text-muted">原因</dt><dd className="min-w-0 text-foreground">{c.summary}</dd>
        {c.impact && <><dt className="text-muted">影响</dt><dd className="min-w-0 text-secondary">{c.impact}</dd></>}
        {c.action && <><dt className="text-muted">处理</dt><dd className="min-w-0 text-secondary">{c.action}</dd></>}
        <dt className="text-muted">最后正常</dt><dd className="min-w-0 text-secondary">{c.since ? fmtTime(c.since) : '—'}</dd>
      </dl>
      <Source c={c} />
    </div>
  )
}

/** 正常 / 未运行项: 一行一项 (名称 · 状态 · 摘要), 点开看摘要全文与入口。异常与注意项不走这里, 仍用卡片。 */
function QuietTable({ title, items, id, icon }: { title: string; items: SystemStatusComponent[]; id: string; icon: React.ComponentType<{ className?: string }> }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!items.length) return null
  return (
    <section data-group={id}>
      <SectionTitle icon={icon}>{`${title} · ${items.length}`}</SectionTitle>
      <div data-quiet-table className={`${CARD} mt-3 divide-y divide-border/60 overflow-hidden`}>
        {items.map((c) => {
          const on = open === c.id
          return (
            <div key={c.id} data-component={c.id}>
              <button type="button" data-quiet-row aria-expanded={on} onClick={() => setOpen(on ? null : c.id)}
                className="grid min-h-[40px] w-full cursor-pointer grid-cols-[minmax(0,9.5rem)_auto_minmax(0,1fr)_auto] items-center gap-3 px-4 py-2 text-left hover:bg-elevated/60 max-md:grid-cols-[minmax(0,1fr)_auto_auto]">
                <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{c.label}</span>
                <StatePill c={c} />
                <span data-quiet-summary className="min-w-0 truncate text-xs text-secondary max-md:hidden">{c.summary}</span>
                <ChevronDown aria-hidden="true" className={`h-3.5 w-3.5 shrink-0 text-muted transition-transform ${on ? 'rotate-180' : ''}`} />
              </button>
              {on && (
                <div data-component-detail className="space-y-1.5 border-t border-border/60 bg-base/40 px-4 py-2.5">
                  <div className="break-words text-xs leading-5 text-secondary">{c.summary}</div>
                  <Source c={c} />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

function JobRow({ j }: { j: SystemStatusJob }) {
  const s = j.state ? JOB_STATE[j.state] : undefined
  const next = fmtNextRun(j.next_run)
  // 「名称 · 频率」拆两行: 名称在上, 频率与状态在下
  const [jobName, ...schedParts] = (JOB_LABEL[j.id] ?? j.id).split(' · ')
  const jobSched = schedParts.join(' · ')
  return (
    <li data-job={j.id} className="flex items-start gap-2.5 px-4 py-2">
      <span aria-hidden="true" className={`mt-0.5 w-3 shrink-0 text-center text-xs ${s ? s.cls : 'text-muted'}`}>{s ? s.icon : '·'}</span>
      <div className="min-w-0 flex-1">
        <div data-job-name className="truncate text-xs leading-5 text-foreground" title={JOB_LABEL[j.id] ?? j.id}>{jobName}</div>
        {(jobSched || s) && (
          <div className="text-[11px] leading-4 text-muted">
            {jobSched}
            {s && <span className={s.cls} title={j.last_at ? fmtTime(j.last_at) : undefined}>{`${jobSched ? ' · ' : ''}${s.label}${j.last_at ? ` ${fmtTime(j.last_at).split('（')[0]}` : ''}`}</span>}
          </div>
        )}
      </div>
      <div data-job-next className="shrink-0 text-right">
        {next
          ? <><div className="tabular-nums text-xs leading-5 text-secondary">{next.et}</div><div className="text-[11px] leading-4 text-muted">{next.local}</div></>
          : <div className="text-xs leading-5 text-muted">—</div>}
      </div>
    </li>
  )
}

function JobsCard({ jobs }: { jobs: SystemStatusJob[] }) {
  const cnt = (s: string[]) => jobs.filter((j) => j.state && s.includes(j.state)).length
  const bad = cnt(['missing', 'error'])
  const sorted = [...jobs].sort((a, b) => (a.next_run ?? '9999').localeCompare(b.next_run ?? '9999') || a.id.localeCompare(b.id))
  return (
    <section data-group="jobs" className="min-w-0">
      <SectionTitle icon={Clock}>{`今日定时任务 · ${jobs.length}`}</SectionTitle>
      <div className={`${CARD} mt-3 overflow-hidden`}>
        <div data-jobs-summary className="flex items-center justify-between gap-2 border-b border-border px-4 py-2 text-[11px] text-muted">
          <span>按下次运行时间</span>
          <span className="flex gap-2 tabular-nums">
            <span className="text-emerald-700 dark:text-emerald-400">{`完成 ${cnt(['ok'])}`}</span>
            <span className="text-accent">{`进行中 ${cnt(['running', 'pending'])}`}</span>
            <span className={bad ? 'text-rose-700 dark:text-rose-400' : ''}>{`未完成 ${bad}`}</span>
          </span>
        </div>
        {jobs.length === 0
          ? <div data-empty="pending" className="px-4 py-3 text-xs text-secondary">暂无定时任务数据</div>
          : <ul data-jobs-list className="divide-y divide-border/60">{sorted.map((j) => <JobRow key={j.id} j={j} />)}</ul>}
      </div>
    </section>
  )
}

export function SystemStatus() {
  const q = useQuery<SystemStatusPayload>({ queryKey: ['system-status'], queryFn: () => api.systemStatus(), refetchInterval: 30_000 })
  if (q.isLoading) {
    return (
      <div><PageHeader title="系统状态" subtitle="运行检查 · 定时任务 · 准入漏斗" />
        <div className={WRAP}><div data-loading className={`${CARD} p-8 text-center text-sm text-muted`}>加载中</div></div>
      </div>
    )
  }
  if (q.isError || !q.data) {
    const why = q.error instanceof Error ? q.error.message : '无返回数据'
    return (
      <div><PageHeader title="系统状态" subtitle="运行检查 · 定时任务 · 准入漏斗" />
        <div className={WRAP}>
          <div role="alert" data-empty="error" className={`${CARD} border-l-[3px] border-l-rose-500 p-4 text-sm`}>
            <div className="font-medium text-foreground">系统状态读取失败</div>
            <div className="mt-0.5 text-xs text-secondary">{`${why} · 页面状态暂不可信`}</div>
            <button type="button" onClick={() => { void q.refetch() }} className="mt-2 rounded-btn border border-border px-2.5 py-1 text-xs text-foreground hover:bg-elevated">重试</button>
          </div>
        </div>
      </div>
    )
  }
  const { components: comps, jobs, market, at } = q.data
  const problems = comps.filter((c) => c.state === 'error' || c.state === 'warn')
  const ok = comps.filter((c) => c.state === 'ok')
  const idle = comps.filter((c) => c.state === 'idle')
  return (
    <div className="overflow-x-hidden">
      <PageHeader title="系统状态" subtitle="运行检查 · 定时任务 · 准入漏斗" />
      <div data-status-wrap className={WRAP}>
        <Verdict comps={comps} market={market} at={at} noProblems={problems.length === 0} />
        {problems.length > 0 && (
          <section data-group="problems">
            <SectionTitle icon={AlertTriangle}>{`需要处理 · ${problems.length}`}</SectionTitle>
            <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">{problems.map((c) => <ProblemCard key={c.id} c={c} />)}</div>
          </section>
        )}
        <div data-status-cols className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,360px)] lg:items-start">
          <div className="min-w-0 space-y-6">
            <QuietTable id="ok" title="正常" icon={ListChecks} items={ok} />
            <QuietTable id="idle" title="未运行" icon={CircleDashed} items={idle} />
            <EntryFunnelTable />
          </div>
          <JobsCard jobs={jobs} />
        </div>
      </div>
    </div>
  )
}
