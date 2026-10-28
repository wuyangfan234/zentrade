import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, CircleDashed, Clock, ListChecks, XCircle } from 'lucide-react'
import {
  api, type SystemStatusComponent, type SystemStatusJob, type SystemStatusMarket, type SystemStatusPayload,
} from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'
import { EntryFunnelTable } from '@/components/EntryFunnelTable'
import { SectionTitle } from '@/components/data/SectionTitle'

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
  idle: { dot: 'bg-muted', label: '未到点', edge: 'border-l-border', pill: 'bg-elevated text-secondary' },
}
const IDLE_LABEL: Record<string, string> = { waiting: '等待首次运行', closed: '休市', disabled: '已停用' }

const PHASE_LABEL: Record<SystemStatusMarket['phase'], string> = {
  premarket: '盘前', session: '盘中', postmarket: '盘后', closed: '休市',
}

const JOB_LABEL: Record<string, string> = {
  daily_pipeline: '盘后管道 (16:35 ET)',
  daily_pipeline_catchup: '盘后管道补跑（开机后一次）',
  daily_pipeline_retry: '盘后管道失败自动重试（每 15 分钟检查，最多 4 次）',
  pre_market_instruments: '盘前标的维表',
  zen_paper_tick: '纸面撮合 (15 秒)',
  zen_hot_quotes: '热报价 (5 秒)',
  zen_news: '个股新闻 (盘前盘中 5 分钟 · 其余 30 分钟)',
  zen_intel: '盘面情报刷新 (每分钟唤醒 · 各源按节奏)',
  zen_qveris_estimates: 'QVeris 财报预期自动查询 (08:45 ET)',
  zen_alpaca_mirror: 'Alpaca 镜像 (15 秒)',
  zen_premarket_gap: '盘前跳空预警 (09:00 ET)',
  zen_swing_pm_check: '盘前越止损检查 (09:20/09:30/09:40 ET)',
  zen_intraday_strength: '盘中强势卡 (10:00–15:30 ET 每 30 分钟)',
  zen_intraday_strength_settle: '盘中强势卡收盘结算 (16:25 ET)',
  zen_open_radar: '开盘放量榜 + 全局渠道推送 (09:51 ET)',
  zen_open_radar_settle: '开盘放量榜收盘记录 (16:40 / 17:40 ET)',
  zen_swing_refresh: '机会板快照 (北京 20:00 起每 30 分钟)',
  zen_radar_frame: '机会页雷达帧 (15 秒探测 · 状态变化即推送)',
  zen_swing_candidates_queue: '挖掘 / AI 策略排队回放 (17:05 ET)',
  zen_weekly_strategy_evaluation: '证据周更 (周六 10:00 ET)',
  zen_weekly_review: '周报 (周日 09:00 ET)',
  zen_ops_check: '盘中盯盘 (3 分钟)',
  zen_market_mood: '盘前大盘情绪 (08:35 ET)',
  zen_corporate_actions: '公司行动',
  zen_earnings_premarket: '财报日历（盘前）',
  zen_earnings_postclose: '财报日历（盘后）',
  zen_market_mood_score: '大盘情绪记分',
  zen_market_mood_score_retry: '大盘情绪补记分',
  zen_fundamentals_sync: '美股财务同步 (18:30 ET)',
  zen_notification_outbox: '通知投递 (60 秒)',
}

const JOB_STATE: Record<string, { icon: string; label: string; cls: string }> = {
  ok: { icon: '✓', label: '已完成', cls: 'text-emerald-700 dark:text-emerald-400' },
  running: { icon: '⏳', label: '进行中', cls: 'text-accent' },
  pending: { icon: '⏳', label: '刚到点', cls: 'text-accent' },
  missing: { icon: '✗', label: '没运行', cls: 'text-rose-700 dark:text-rose-400' },
  error: { icon: '✗', label: '失败', cls: 'text-rose-700 dark:text-rose-400' },
}

const CARD = 'rounded-card border border-border bg-surface'
const WRAP = 'mx-auto max-w-6xl space-y-6 px-4 py-4 md:px-8 md:py-6'

/** "美东 10:42（新加坡 22:42）" */
export function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const f = (tz: string) => d.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
  const day = (tz: string) => d.toLocaleDateString('en-CA', { timeZone: tz })
  const next = day('Asia/Singapore') > day('America/New_York') ? '次日' : ''
  return `美东 ${f('America/New_York')}（新加坡${next} ${f('Asia/Singapore')}）`
}

/** 定时任务下次时间拆两行: 主行 "10-13 09:10 ET", 次行 "新加坡 21:10" / "新加坡次日 04:35" */
export function fmtNextRun(iso?: string | null): { et: string; sg: string } | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return { et: iso, sg: '' }
  const f = (tz: string) => d.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
  const day = (tz: string) => d.toLocaleDateString('en-CA', { timeZone: tz })
  const next = day('Asia/Singapore') > day('America/New_York') ? '次日' : ''
  return { et: `${day('America/New_York').slice(5)} ${f('America/New_York')} ET`, sg: `新加坡${next} ${f('Asia/Singapore')}` }
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
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-[16px] font-semibold leading-6 tabular-nums ${value ? tone : 'text-muted'}`}>{value}</div>
    </div>
  )
}

function Verdict({ comps, market, at, noProblems }: { comps: SystemStatusComponent[]; market?: SystemStatusMarket; at: string; noProblems: boolean }) {
  const n = (s: string) => comps.filter((c) => c.state === s).length
  const err = n('error'); const warn = n('warn')
  const kind = err ? 'error' : warn ? 'warn' : 'ok'
  const head = err
    ? `${err} 项异常${warn ? ` · ${warn} 项注意` : ''} · 其余正常或未到点`
    : warn ? `${warn} 项需要注意 · 其余正常或未到点`
      : market?.phase === 'closed' ? '休市，系统只做盘后任务，一切正常' : '一切正常'
  const Icon = err ? XCircle : warn ? AlertTriangle : CheckCircle2
  const iconCls = err ? 'text-rose-600 dark:text-rose-400' : warn ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'
  return (
    <div data-verdict={kind} className={`${CARD} border-l-[3px] ${STATE_STYLE[kind].edge} p-4`}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${iconCls}`} aria-hidden="true" />
          <div className="min-w-0">
            <div className="text-[15px] font-semibold leading-6 text-foreground">{head}</div>
            {market && (
              <div data-market className="mt-1 text-xs text-secondary">
                {`${PHASE_LABEL[market.phase]} · ${market.trading_day ? '今天是交易日' : '今天不是交易日'}`}
                {market.phase !== 'session' && ` · 下次开盘 ${market.next_open.slice(5, 10)} ${fmtTime(market.next_open)}`}
                {market.phase === 'session' && market.close && ` · 收盘 ${fmtTime(market.close)}`}
              </div>
            )}
            <div className="mt-0.5 text-xs text-muted">{`页面更新 ${fmtTime(at)} · 30 秒自动刷新`}</div>
            {noProblems && <div data-no-problems className="mt-1.5 text-xs text-emerald-700 dark:text-emerald-400">✓ 没有需要处理的项</div>}
          </div>
        </div>
        <div data-verdict-stats className="grid shrink-0 grid-cols-4 gap-2 lg:w-[22rem]">
          <Stat id="error" label="异常" value={err} tone="text-rose-700 dark:text-rose-400" />
          <Stat id="warn" label="注意" value={warn} tone="text-amber-700 dark:text-amber-400" />
          <Stat id="ok" label="正常" value={n('ok')} tone="text-emerald-700 dark:text-emerald-400" />
          <Stat id="idle" label="未到点" value={n('idle')} tone="text-foreground" />
        </div>
      </div>
    </div>
  )
}

function Source({ s }: { s?: string }) {
  if (!s) return null
  return <div data-source className="mt-auto break-all border-t border-border/60 pt-1.5 font-mono text-[11px] leading-4 text-muted">{`出处：${s}`}</div>
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
        {c.action && <><dt className="text-muted">下一步</dt><dd className="min-w-0 text-secondary">{c.action}</dd></>}
        <dt className="text-muted">最后正常</dt><dd className="min-w-0 text-secondary">{c.since ? fmtTime(c.since) : '—'}</dd>
      </dl>
      <Source s={c.source} />
    </div>
  )
}

function QuietCard({ c }: { c: SystemStatusComponent }) {
  return (
    <div data-component={c.id} className={`${CARD} flex h-full flex-col p-3`}>
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 break-words text-sm font-medium text-foreground">{c.label}</span>
        <StatePill c={c} />
      </div>
      <div className="mb-2 mt-1.5 break-words text-xs leading-5 text-secondary">{c.summary}</div>
      <Source s={c.source} />
    </div>
  )
}

function Section({ title, count, children, id, icon }: { title: string; count: number; children: React.ReactNode; id: string; icon: React.ComponentType<{ className?: string }> }) {
  if (!count) return null
  return (
    <section data-group={id}>
      <SectionTitle icon={icon}>{`${title} · ${count}`}</SectionTitle>
      <div data-card-grid className="mt-3 grid grid-cols-1 items-stretch gap-3 sm:grid-cols-2">{children}</div>
    </section>
  )
}

function JobRow({ j }: { j: SystemStatusJob }) {
  const s = j.state ? JOB_STATE[j.state] : undefined
  const next = fmtNextRun(j.next_run)
  return (
    <li data-job={j.id} className="flex items-start gap-2.5 px-4 py-2">
      <span aria-hidden="true" className={`mt-0.5 w-3 shrink-0 text-center text-xs ${s ? s.cls : 'text-muted'}`}>{s ? s.icon : '·'}</span>
      <div className="min-w-0 flex-1">
        <div className="break-words text-xs leading-5 text-foreground">{JOB_LABEL[j.id] ?? j.id}</div>
        {s && <div className={`text-[11px] leading-4 ${s.cls}`}>{`${s.label}${j.last_at ? ` ${fmtTime(j.last_at)}` : ''}`}</div>}
      </div>
      <div data-job-next className="shrink-0 text-right">
        {next
          ? <><div className="font-mono text-xs leading-5 tabular-nums text-secondary">{next.et}</div><div className="text-[11px] leading-4 text-muted">{next.sg}</div></>
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
      <SectionTitle icon={Clock}>{`今天的定时任务 · ${jobs.length}`}</SectionTitle>
      <div className={`${CARD} mt-3 overflow-hidden`}>
        <div data-jobs-summary className="flex items-center justify-between gap-2 border-b border-border px-4 py-2 text-[11px] text-muted">
          <span>按下次运行时间排序</span>
          <span className="flex gap-2 tabular-nums">
            <span className="text-emerald-700 dark:text-emerald-400">{`✓ ${cnt(['ok'])}`}</span>
            <span className="text-accent">{`⏳ ${cnt(['running', 'pending'])}`}</span>
            <span className={bad ? 'text-rose-700 dark:text-rose-400' : ''}>{`✗ ${bad}`}</span>
          </span>
        </div>
        {jobs.length === 0
          ? <div data-empty="pending" className="px-4 py-3 text-xs text-secondary">⏳ 还没有读到定时任务（数据还没到）</div>
          : <ul data-jobs-list className="divide-y divide-border/60">{sorted.map((j) => <JobRow key={j.id} j={j} />)}</ul>}
      </div>
    </section>
  )
}

export function SystemStatus() {
  const q = useQuery<SystemStatusPayload>({ queryKey: ['system-status'], queryFn: () => api.systemStatus(), refetchInterval: 30_000 })
  if (q.isLoading) {
    return (
      <div><PageHeader title="系统状态" subtitle="一眼看出哪里正常、哪里不正常" />
        <div className={WRAP}><div data-loading className={`${CARD} p-8 text-center text-sm text-muted`}>加载中…</div></div>
      </div>
    )
  }
  if (q.isError || !q.data) {
    const why = q.error instanceof Error ? q.error.message : '没有返回数据'
    return (
      <div><PageHeader title="系统状态" subtitle="一眼看出哪里正常、哪里不正常" />
        <div className={WRAP}>
          <div role="alert" data-empty="error" className={`${CARD} border-l-[3px] border-l-rose-500 p-4 text-sm`}>
            <div className="font-medium text-foreground">✗ 系统状态读取失败（异常）</div>
            <div className="mt-0.5 text-xs text-secondary">{`原因：${why} · 页面上的状态暂时不可信，后端可能没响应`}</div>
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
      <PageHeader title="系统状态" subtitle="一眼看出哪里正常、哪里不正常" />
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
            <Section id="ok" title="正常" icon={ListChecks} count={ok.length}>{ok.map((c) => <QuietCard key={c.id} c={c} />)}</Section>
            <Section id="idle" title="等待 / 休市 / 停用" icon={CircleDashed} count={idle.length}>{idle.map((c) => <QuietCard key={c.id} c={c} />)}</Section>
          </div>
          <JobsCard jobs={jobs} />
        </div>
        <EntryFunnelTable />
      </div>
    </div>
  )
}
