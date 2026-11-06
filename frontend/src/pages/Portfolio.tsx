import { useMemo, useState, useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Wallet } from 'lucide-react'
import { api, type StopMove, type AlpacaMirrorPayload, type ClosedTrip, type LivePosition, type PortfolioLive, type SetupStatusRow } from '@/lib/api'
import { formatScheduleDatePart, formatScheduleTimePart } from '@/lib/format'
import { etToday } from '@/lib/etTime'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { EquityCurve } from '@/components/EquityCurve'

/**
 * 持仓页 — ZenTrade 波段纸面账户 (P3)。0.53.0 起为「账本表格」版式:
 *
 * 状态胶囊一行 → 六格总账 → 净值曲线 → 当前持仓表 (一行一只, 点开看细节) →
 * 已平仓表 (按规则筛选) → 记录 (台账事件 / Alpaca 镜像)。
 * 全部数据来自 append-only 台账重放, 账面即真相; 本页只做展示, 不改任何计算口径。
 */

interface Position {
  symbol: string
  qty: number  // 负数 = 空头
  avg_cost: number
  side?: 'long' | 'short'
  entry_date: string
  card_id?: string
  stop_price?: number
}

interface LedgerEvent {
  seq: number
  at: string
  kind: string
  symbol?: string
  qty?: number
  price?: number
  fee?: number
  reason?: string
  r_multiple?: number
  position_side?: 'long' | 'short'
  side?: string
  stop_price?: number
  signal_price?: number
  action_type?: string
  rate?: number
}

interface NavPoint {
  date: string
  nav: number
}

interface Overview {
  cash: number
  nav: number
  positions: Position[]
  open_risk: number
  events: LedgerEvent[]
  nav_daily: NavPoint[]
}

type Side = 'long' | 'short'

/* ── 格式化 ─────────────────────────────────────────── */

function fmt(n: number | undefined | null, digits = 2): string {
  if (n === undefined || n === null) return '—'
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function money(n: number | undefined | null, digits = 0): string {
  return n === undefined || n === null || !Number.isFinite(n) ? '—' : `$${fmt(n, digits)}`
}

function signed(n: number | null | undefined, digits = 2, prefix = ''): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—'
  const v = Number(n.toFixed(digits))
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  return `${sign}${prefix}${fmt(Math.abs(v), digits)}`
}

function pct(n: number | null | undefined, digits = 2): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—'
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

/** R 倍数保留 2 位, 正数带 + */
function fmtR(r: number | null | undefined): string {
  return r != null && Number.isFinite(r) ? `${r > 0 ? '+' : ''}${r.toFixed(2)}R` : '—'
}

function tone(n: number | null | undefined): string {
  if (!n) return 'text-secondary'
  return n > 0 ? 'text-bull' : 'text-bear'
}

/** 本地时间 HH:MM:SS */
function fmtClock(ts: number): string {
  const d = new Date(ts)
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
}

function scheduleStamp(iso: string | null | undefined): string {
  return iso ? `${formatScheduleDatePart(iso)} ${formatScheduleTimePart(iso)}` : '—'
}

const WEEKDAY = new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', weekday: 'short' })

/** 台账时间戳 → 美东日期键 / MM-DD 周X / HH:MM */
function etDay(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? etToday(d) : iso.slice(0, 10)
}
function etDayLabel(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? `${formatScheduleDatePart(iso)} ${WEEKDAY.format(d)}` : iso.slice(5, 10)
}
function etClock(iso: string): string {
  const t = formatScheduleTimePart(iso)
  return t === '—' ? iso.slice(11, 16) : t.replace(' ET', '')
}

/* ── 词表 ─────────────────────────────────────────── */

const SETUP_NAME: Record<string, string> = { breakout: '突破', pullback: '回踩', 'strategy:swing': '机会板' }

/** 出场原因: 按原因码给短词 (多空通用); 映射不到回退后端文案 */
const EXIT_REASON: Record<string, string> = {
  invalidation: '止损', trailing_stop: '移动止损', time_stop: '到期', target_zone_upper: '到达目标',
  target_zone_lower: '目标减半', earnings_reduce: '财报前减半', corp_action: '公司行动', manual: '人工',
  watchlist_removed: '移出自选',
}
function reasonLabel(code?: string | null, fallback?: string | null): string {
  if (!code) return fallback ?? ''
  if (EXIT_REASON[code]) return EXIT_REASON[code]
  const m = /^trailing_(-?[\d.]+)R$/.exec(code)
  if (m) return `浮盈 ${m[1]}R`
  if (code === 'breakeven_after_partial') return '减半后保本'
  return fallback ?? code
}

const KIND_LABEL: Record<string, string> = {
  fill: '开仓', partial_close: '减仓', close: '平仓', stop_move: '止损上移', corp_action: '公司行动',
  adjust: '冲正', opportunity: '机会', entry_intent: '挂单', exit_intent: '待开盘平仓',
}

/** 台账事件中文名; 止损调整按持仓方向区分: 多头止损上移, 空头止损下移 */
function eventLabel(e: LedgerEvent): string {
  if (e.kind === 'stop_move') return e.position_side === 'short' ? '止损下移' : '止损上移'
  return KIND_LABEL[e.kind] ?? e.kind
}

const PRICE_SOURCE: Record<string, string> = { tickflow: '实时', iex: 'IEX', last_close: '最近收盘', cost: '成本价' }

/** 持仓现价刷新间隔 (2026-10-02 产品决策: 5 秒)。 */
export const LIVE_REFRESH_MS = 5_000

/** 提醒阈值: 距止损 / 距目标小于该百分比 (相对现价) 标「近止损 / 近目标」 */
const NEAR_PCT = 1.5
/** 已平仓亏损超过计划风险该倍数标琥珀「亏超」 */
const OVER_LOSS_R = -1.5
const CLOSED_PAGE = 20
const EVENT_PAGE = 20

/* ── 响应式: 桌面表格 / 手机卡片 (无 matchMedia 的环境按桌面渲染) ── */

const DESKTOP_MQ = '(min-width: 768px)'
function useDesktop(): boolean {
  return useSyncExternalStore(
    (cb) => {
      if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
      const m = window.matchMedia(DESKTOP_MQ)
      m.addEventListener('change', cb)
      return () => m.removeEventListener('change', cb)
    },
    () => typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia(DESKTOP_MQ).matches,
    () => true,
  )
}

/* ── 小部件 ─────────────────────────────────────────── */

function SideTag({ side }: { side?: Side }) {
  const short = side === 'short'
  return (
    <span data-side={short ? 'short' : 'long'} title={short ? '做空' : '做多'}
      className={cn('inline-flex h-[18px] w-5 shrink-0 items-center justify-center rounded border border-muted/60 text-[11px] font-semibold text-secondary',
        short && 'bg-elevated')}>{short ? '空' : '多'}</span>
  )
}

type TagTone = 'warn' | 'bull' | 'plain'
function Tag({ tone: t = 'plain', dot = true, children, title }: { tone?: TagTone; dot?: boolean; children: React.ReactNode; title?: string }) {
  return (
    <span data-tag={t} title={title}
      className={cn('inline-flex h-5 items-center gap-1 whitespace-nowrap rounded border px-1.5 text-[11px] leading-none',
        t === 'warn' ? 'border-warning/50 bg-warning/10 text-warning' : t === 'bull' ? 'border-border text-foreground' : 'border-border text-secondary')}>
      {dot && <i className={cn('inline-block h-1.5 w-1.5 rounded-full', t === 'warn' ? 'bg-warning' : t === 'bull' ? 'bg-bull' : 'bg-muted')} />}
      {children}
    </span>
  )
}

function Chip({ danger, children, title, ...rest }: { danger?: boolean; children: React.ReactNode; title?: string } & Record<`data-${string}`, string | undefined>) {
  return (
    <span {...rest} title={title}
      className={cn('inline-flex h-6 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-[11px]',
        danger ? 'border-danger/50 bg-danger/10 text-danger' : 'border-border bg-surface text-secondary')}>
      {children}
    </span>
  )
}

function DaysDots({ days, max }: { days?: number | null; max?: number | null }) {
  const m = Math.max(1, Math.min(max ?? 7, 10))
  const d = days ?? 0
  return (
    <span className="inline-flex items-center gap-[3px] align-middle" aria-hidden>
      {Array.from({ length: m }, (_, i) => (
        <i key={i} className={cn('inline-block h-2 w-2 rounded-[2px]', i < d ? (d >= m ? 'bg-warning' : 'bg-secondary') : 'bg-border')} />
      ))}
    </span>
  )
}

function Seg<T extends string>({ items, value, onChange, attr }: { items: [T, string][]; value: T; onChange: (v: T) => void; attr: string }) {
  return (
    <div className="inline-flex overflow-hidden rounded-btn border border-border">
      {items.map(([k, label]) => (
        <button key={k} type="button" {...{ [attr]: k }} aria-pressed={k === value} onClick={() => onChange(k)}
          className={cn('h-8 whitespace-nowrap border-r border-border px-3.5 text-[13px] last:border-r-0',
            k === value ? 'bg-accent/15 font-semibold text-foreground shadow-[inset_0_-2px_0_hsl(var(--accent))]' : 'text-secondary hover:bg-elevated')}>
          {label}
        </button>
      ))}
    </div>
  )
}

function Pager({ page, pages, from, to, total, onPage }: { page: number; pages: number; from: number; to: number; total: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null
  return (
    <div data-pager className="flex items-center gap-2 border-t border-border px-3 py-2 text-xs text-muted">
      <span className="tabular-nums">{`${from}–${to} / ${total}`}</span>
      <span className="ml-auto" />
      <button type="button" disabled={page <= 0} onClick={() => onPage(page - 1)}
        className="h-7 whitespace-nowrap rounded-btn border border-border px-2.5 text-secondary hover:bg-elevated disabled:opacity-40">上一页</button>
      <button type="button" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}
        className="h-7 whitespace-nowrap rounded-btn border border-border px-2.5 text-secondary hover:bg-elevated disabled:opacity-40">下一页</button>
    </div>
  )
}

function SectionHead({ title, count, right }: { title: string; count?: number | string; right?: React.ReactNode }) {
  return (
    <div className="mb-2.5 mt-6 flex min-h-[32px] flex-wrap items-center gap-x-3 gap-y-2">
      <h2 className="text-[13px] font-semibold text-foreground">{title}</h2>
      {count != null && <span className="text-xs tabular-nums text-muted">{count}</span>}
      {right}
    </div>
  )
}

/** 0.18.0: 初始→当前止损 · 调整 N 次; 有调整时可展开逐次记录 */
export function StopTrail({ initial, current, moves }: { initial?: number | null; current?: number | null; moves?: StopMove[] }) {
  const [open, setOpen] = useState(false)
  const list = moves ?? []
  const d = (n?: number | null) => (n ? `$${fmt(n)}` : '—')
  return (
    <span data-stop-trail className="block">
      <span>{`止损 ${d(initial)}→${d(current)} · 调整 ${list.length} 次`}</span>
      {list.length > 0 && (
        <button type="button" data-stop-toggle onClick={(e) => { e.stopPropagation(); setOpen(!open) }} className="ml-2 text-[11px] text-muted underline">
          {open ? '收起' : '明细'}
        </button>
      )}
      {open && (
        <span className="mt-0.5 block text-[11px] text-muted">
          {list.map((m, i) => (
            <span key={m.seq ?? i} data-stop-move className="block">{`${m.trade_date ?? '—'} ${d(m.from)}→${d(m.stop_price)} ${m.reason ?? ''}`.trim()}</span>
          ))}
        </span>
      )}
    </span>
  )
}

/* ── 持仓派生 (只读展示) ─────────────────────────────── */

interface Derived {
  p: LivePosition
  target: number | null
  stopDist: number | null
  targetDist: number | null
  atStop: number | null
  atTarget: number | null
  tags: { t: string; tone: TagTone }[]
  rank: number
}

function derive(p: LivePosition, pendingExit: Map<string, string>): Derived {
  const short = p.side === 'short'
  const sgn = short ? -1 : 1
  const target = p.target_zone?.length ? p.target_zone[short ? 1 : 0] : null
  const stopDist = p.stop_distance_pct != null ? Math.abs(p.stop_distance_pct) : null
  const targetDist = p.target_distance_pct != null ? Math.abs(p.target_distance_pct) : null
  const atStop = p.stop_price ? sgn * (p.stop_price - p.avg_cost) * p.qty : null
  const atTarget = target ? sgn * (target - p.avg_cost) * p.qty : null
  const tags: { t: string; tone: TagTone }[] = []
  let rank = 3
  const exitReason = pendingExit.get(p.symbol)
  if (exitReason !== undefined) {
    tags.push({ t: `待开盘平仓${exitReason ? ` · ${reasonLabel(exitReason)}` : ''}`, tone: 'warn' }); rank = 0
  }
  if (p.days_held != null && p.max_holding_days != null && p.days_held >= p.max_holding_days) {
    tags.push({ t: p.days_held > p.max_holding_days ? '已到期' : '今日到期', tone: 'warn' }); rank = Math.min(rank, 0)
  }
  if (stopDist != null && stopDist < NEAR_PCT) { tags.push({ t: '近止损', tone: 'warn' }); rank = Math.min(rank, 1) }
  if (targetDist != null && targetDist < NEAR_PCT) { tags.push({ t: '近目标', tone: 'bull' }); rank = Math.min(rank, 2) }
  if (p.partial_taken) tags.push({ t: '已减半', tone: 'plain' })
  if (atStop != null && atStop > -0.5) tags.push(atStop >= 50 ? { t: `锁盈 +$${fmt(atStop, 0)}`, tone: 'bull' } : { t: '保本', tone: 'plain' })
  return { p, target, stopDist, targetDist, atStop, atTarget, tags, rank }
}

function Tags({ d }: { d: Derived }) {
  if (!d.tags.length) return null
  return <span className="inline-flex flex-wrap gap-1">{d.tags.map((x) => <Tag key={x.t} tone={x.tone}>{x.t}</Tag>)}</span>
}

function setupName(p: { setup_label?: string | null; setup_type?: string | null }): string {
  return p.setup_label ?? SETUP_NAME[p.setup_type ?? ''] ?? p.setup_type ?? ''
}

function priceSource(p: LivePosition): string {
  const src = PRICE_SOURCE[p.price_source] ?? p.price_source
  const age = p.price_age_s != null ? ` ${Math.round(p.price_age_s)}s` : ''
  return `${src}${age}`
}

function PositionDetail({ d }: { d: Derived }) {
  const p = d.p
  const exposure = p.accounting === 'contract'
    ? `名义 $${fmt(p.notional ?? p.qty * p.last_price, 0)}`
    : p.side === 'short' ? `回补负债 $${fmt(Math.abs(p.market_value), 0)}` : `市值 $${fmt(p.market_value, 0)}`
  const item = 'min-w-0'
  const k = 'block text-[11px] text-muted'
  return (
    <div data-position-detail={p.symbol} className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs text-secondary md:grid-cols-4">
      <div className={item}><span className={k}>开仓日</span>{p.entry_date ?? '—'}</div>
      <div className={item}><span className={k}>仓位</span>{`${p.side === 'short' ? '空' : '多'} ${p.qty} 股 · ${exposure}`}</div>
      <div className={item}><span className={k}>计划风险</span>
        {p.accounting === 'contract' && p.risk_pct != null
          ? <span data-risk title="开仓时这笔承担的风险占净值的比例">{`${(p.risk_pct * 100).toFixed(2)}%${p.entry_tier === 'trial' ? ' · 试用' : ''}`}</span>
          : '—'}
      </div>
      <div className={item}><span className={k}>价格来源</span>{priceSource(p)}</div>
      <div className={item}><span className={k}>打到止损</span><span className={tone(d.atStop)}>{signed(d.atStop, 0, '$')}</span></div>
      <div className={item}><span className={k}>到目标</span><span className={tone(d.atTarget)}>{signed(d.atTarget, 0, '$')}</span></div>
      <div className={cn(item, 'col-span-2')}><span className={k}>止损轨迹</span>
        <StopTrail initial={p.stop_initial} current={p.stop_price} moves={p.stop_moves} />
      </div>
    </div>
  )
}

function PositionsTable({ rows, open, onToggle }: { rows: Derived[]; open: string | null; onToggle: (s: string) => void }) {
  const th = 'px-3 font-medium whitespace-nowrap'
  const sub = 'block text-[11px] leading-4 text-muted'
  return (
    <div className="overflow-x-auto rounded-card border border-border bg-surface">
      <table className="w-full min-w-[980px] text-[13px] tabular-nums">
        <thead><tr className="h-8 bg-elevated text-[11.5px] text-muted">
          <th className={cn(th, 'pl-9 text-left')}>代码</th>
          <th className={cn(th, 'text-left')}>持有天数</th>
          <th className={cn(th, 'text-right')}>股数</th>
          <th className={cn(th, 'text-right')}>成本</th>
          <th className={cn(th, 'text-right')}>现价</th>
          <th className={cn(th, 'text-right')} title="距 = 相对现价">止损</th>
          <th className={cn(th, 'text-right')} title="距 = 相对现价 · 做空取先触及的目标上沿">目标</th>
          <th className={cn(th, 'text-right')}>浮动盈亏</th>
          <th className={cn(th, 'text-right')} title="浮动盈亏，R = 以止损距离为 1 份风险">R</th>
          <th className={cn(th, 'text-left')}>状态</th>
        </tr></thead>
        <tbody>
          {rows.map((d) => {
            const p = d.p
            const isOpen = open === p.symbol
            return [
              <tr key={p.symbol} data-position={p.symbol} role="button" tabIndex={0} aria-expanded={isOpen}
                onClick={() => onToggle(p.symbol)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(p.symbol) } }}
                className={cn('h-12 cursor-pointer border-t border-border/70 first:border-t-0 hover:bg-elevated/60', isOpen && 'bg-elevated/60')}>
                <td className="px-3">
                  <span className="flex items-center gap-2">
                    <span className="w-3 text-[11px] text-secondary">{isOpen ? '▾' : '▸'}</span>
                    <SideTag side={p.side} />
                    <span className="min-w-0">
                      <span className="block font-semibold text-foreground">{p.symbol}</span>
                      <span data-setup title={p.setup_type ?? undefined} className="block truncate text-[11px] leading-4 text-muted">{setupName(p)}</span>
                    </span>
                  </span>
                </td>
                <td className="whitespace-nowrap px-3"><DaysDots days={p.days_held} max={p.max_holding_days} /><span className="ml-1.5 text-secondary">{`${p.days_held ?? '—'}/${p.max_holding_days ?? 7}`}</span></td>
                <td className="px-3 text-right text-secondary">{p.qty}</td>
                <td className="px-3 text-right text-secondary">{money(p.entry_price, 2)}</td>
                <td className="px-3 text-right">{money(p.last_price, 2)}<span className={sub}>{priceSource(p)}</span></td>
                <td className="px-3 text-right">{money(p.stop_price, 2)}<span className={cn(sub, d.stopDist != null && d.stopDist < NEAR_PCT && 'text-warning')}>{d.stopDist != null ? `距 ${d.stopDist.toFixed(1)}%` : '—'}</span></td>
                <td className="px-3 text-right">{money(d.target, 2)}<span className={sub}>{d.targetDist != null ? `距 ${d.targetDist.toFixed(1)}%` : '—'}</span></td>
                <td className="px-3 text-right"><span className={cn('font-semibold', tone(p.unrealized_pnl))}>{signed(p.unrealized_pnl, 2, '$')}</span><span className={cn(sub, tone(p.unrealized_pct))}>{pct(p.unrealized_pct)}</span></td>
                <td className={cn('px-3 text-right', tone(p.r_now))}>{fmtR(p.r_now)}</td>
                <td className="px-3"><Tags d={d} /></td>
              </tr>,
              isOpen && (
                <tr key={`${p.symbol}-detail`} className="bg-elevated/60">
                  <td colSpan={10} className="px-3 pb-3.5 pl-9 pt-1"><PositionDetail d={d} /></td>
                </tr>
              ),
            ]
          })}
        </tbody>
      </table>
    </div>
  )
}

function PositionsCards({ rows, open, onToggle }: { rows: Derived[]; open: string | null; onToggle: (s: string) => void }) {
  const k = 'block text-[11px] text-muted'
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      {rows.map((d) => {
        const p = d.p
        const isOpen = open === p.symbol
        return (
          <div key={p.symbol} data-position={p.symbol} role="button" tabIndex={0} aria-expanded={isOpen}
            onClick={() => onToggle(p.symbol)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(p.symbol) } }}
            className="border-t border-border/70 p-3 first:border-t-0">
            <div className="flex items-center gap-2">
              <SideTag side={p.side} />
              <span className="min-w-[44px] font-semibold text-foreground">{p.symbol}</span>
              <DaysDots days={p.days_held} max={p.max_holding_days} />
              <span className="text-xs text-secondary">{`${p.days_held ?? '—'}/${p.max_holding_days ?? 7} 天`}</span>
              <span className={cn('ml-auto font-semibold tabular-nums', tone(p.unrealized_pnl))}>{signed(p.unrealized_pnl, 2, '$')}</span>
            </div>
            <span data-setup title={p.setup_type ?? undefined} className="mt-0.5 block text-[11px] text-muted">{setupName(p)}</span>
            <div className="mt-2 grid grid-cols-3 gap-x-2 gap-y-1.5 text-xs tabular-nums text-foreground">
              <div><span className={k}>成本</span>{money(p.entry_price, 2)}</div>
              <div><span className={k}>现价</span>{money(p.last_price, 2)}</div>
              <div><span className={k}>浮动</span><span className={tone(p.unrealized_pct)}>{pct(p.unrealized_pct)}</span></div>
              <div><span className={k}>止损 · 距</span>{money(p.stop_price, 2)} <span className={d.stopDist != null && d.stopDist < NEAR_PCT ? 'text-warning' : 'text-muted'}>{d.stopDist != null ? `${d.stopDist.toFixed(1)}%` : ''}</span></div>
              <div><span className={k}>目标 · 距</span>{money(d.target, 2)} <span className="text-muted">{d.targetDist != null ? `${d.targetDist.toFixed(1)}%` : ''}</span></div>
              <div><span className={k}>R</span><span className={tone(p.r_now)}>{fmtR(p.r_now)}</span></div>
            </div>
            {d.tags.length > 0 && <div className="mt-2"><Tags d={d} /></div>}
            {isOpen && <div className="mt-3 border-t border-dashed border-border pt-3"><PositionDetail d={d} /></div>}
          </div>
        )
      })}
    </div>
  )
}

/* ── 已平仓 ─────────────────────────────────────────── */

type Scope = 'all' | 'swing' | 'legacy'
/** 回合口径与后端 portfolio._scope_of 一致: 机会板卡 id 含 -book-swing-, 其余为旧规则 */
function scopeOf(c: ClosedTrip): Exclude<Scope, 'all'> {
  return String(c.card_id ?? '').includes('-book-swing-') ? 'swing' : 'legacy'
}

function closedStats(list: ClosedTrip[]) {
  const rs = list.map((c) => c.r_multiple).filter((r): r is number => typeof r === 'number' && Number.isFinite(r))
  return {
    n: list.length,
    win: list.length ? Math.round(list.filter((c) => (c.pnl ?? 0) > 0).length / list.length * 100) : null,
    avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
    pnl: list.reduce((a, c) => a + (c.pnl ?? 0), 0),
  }
}

function ClosedReason({ c }: { c: ClosedTrip }) {
  const over = typeof c.r_multiple === 'number' && c.r_multiple < OVER_LOSS_R
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span className="text-secondary">{reasonLabel(c.exit_reason, c.exit_reason_label)}</span>
      {c.partial_exits > 0 && <Tag dot={false}>{`分 ${c.partial_exits + 1} 次`}</Tag>}
      {over && <Tag tone="warn" title="实际亏损超过计划风险的 1.5 倍 (跳空或收盘才判止损)">亏超 1.5R</Tag>}
    </span>
  )
}

type LiveSummary = NonNullable<PortfolioLive['summary']>

function ClosedSection({ closed, desktop, summary }: { closed: ClosedTrip[]; desktop: boolean; summary: LiveSummary | null }) {
  const [scope, setScope] = useState<Scope>('all')
  const [page, setPage] = useState(0)
  const list = scope === 'all' ? closed : closed.filter((c) => scopeOf(c) === scope)
  // 胜率 / 平均 R 以后端 summary 为准 (全部 = 总口径, 分口径 = by_scope); 后端缺字段时才按列表现算
  const local = closedStats(list)
  const grp = scope === 'all' ? null : summary?.by_scope?.find((g) => g.key === scope)
  const st = {
    win: scope === 'all'
      ? (summary?.win_rate != null ? Math.round(summary.win_rate * 100) : local.win)
      : (grp ? Math.round(grp.win_rate * 100) : local.win),
    avgR: scope === 'all' ? (summary?.avg_r ?? local.avgR) : (grp ? grp.avg_r : local.avgR),
    pnl: local.pnl,
  }
  const count = (s: Scope) => (s === 'all' ? closed.length : closed.filter((c) => scopeOf(c) === s).length)
  const pages = Math.max(1, Math.ceil(list.length / CLOSED_PAGE))
  const cur = Math.min(page, pages - 1)
  const shown = list.slice(cur * CLOSED_PAGE, (cur + 1) * CLOSED_PAGE)
  const key = (c: ClosedTrip) => `${c.symbol}-${c.entry_date}-${c.exit_date}`
  const dates = (c: ClosedTrip) => `${(c.entry_date ?? '—').slice(5)} → ${(c.exit_date ?? '—').slice(5)}`
  return (
    <section data-closed-section>
      <SectionHead title="已平仓" right={closed.length > 0 && (
        <>
          <Seg<Scope> attr="data-scope" value={scope} onChange={(s) => { setScope(s); setPage(0) }}
            items={[['all', `全部 ${count('all')}`], ['swing', `机会板 ${count('swing')}`], ['legacy', `旧规则 ${count('legacy')}`]]} />
          <span data-closed-stats className="ml-auto text-xs tabular-nums text-secondary">
            {`胜率 ${st.win ?? '—'}% · 平均 `}<span className={tone(st.avgR)}>{fmtR(st.avgR)}</span>{' · 合计 '}<span className={tone(st.pnl)}>{signed(st.pnl, 0, '$')}</span>
          </span>
        </>
      )} />
      {closed.length === 0 ? (
        <div className="rounded-card border border-border bg-surface px-4 py-3 text-xs text-muted">尚无完整平仓的回合。</div>
      ) : desktop ? (
        <div className="overflow-x-auto rounded-card border border-border bg-surface">
          <table className="w-full min-w-[900px] text-[13px] tabular-nums">
            <thead><tr className="h-8 bg-elevated text-[11.5px] text-muted">
              {['代码', '开仓 → 平仓', '天数', '成本', '平仓价', '盈亏', '收益率', 'R', '出场原因', '规则'].map((h, i) => (
                <th key={h} title={h === '平仓价' ? '分批平仓时为均价' : undefined}
                  className={cn('whitespace-nowrap px-3 font-medium', i === 0 || i === 1 || i >= 8 ? 'text-left' : 'text-right', h === 'R' && 'pr-6')}>{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {shown.map((c) => (
                <tr key={key(c)} data-closed={c.symbol} className="h-10 border-t border-border/70 first:border-t-0">
                  <td className="px-3"><span className="flex items-center gap-2"><SideTag side={c.side} /><span className="font-semibold text-foreground">{c.symbol}</span></span></td>
                  <td className="whitespace-nowrap px-3 text-secondary">{dates(c)}</td>
                  <td className="px-3 text-right text-secondary">{`${c.days_held ?? '—'} 天`}</td>
                  <td className="px-3 text-right text-secondary">{money(c.entry_price, 2)}</td>
                  <td className="px-3 text-right text-secondary">{money(c.exit_price, 2)}</td>
                  <td className="px-3 text-right" title={`手续费 $${fmt(c.fees)}`}><span className={cn('font-semibold', tone(c.pnl))}>{signed(c.pnl, 2, '$')}</span></td>
                  <td className={cn('px-3 text-right', tone(c.return_pct))}>{pct(c.return_pct)}</td>
                  <td className={cn('px-3 pr-6 text-right', tone(c.r_multiple))}>{fmtR(c.r_multiple)}</td>
                  <td className="px-3"><ClosedReason c={c} /></td>
                  <td className="whitespace-nowrap px-3 text-secondary">{scopeOf(c) === 'swing' ? '机会板' : '旧规则'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager page={cur} pages={pages} from={cur * CLOSED_PAGE + 1} to={cur * CLOSED_PAGE + shown.length} total={list.length} onPage={setPage} />
        </div>
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-surface">
          {shown.map((c) => (
            <div key={key(c)} data-closed={c.symbol} className="border-t border-border/70 px-3 py-2.5 text-xs first:border-t-0">
              <div className="flex items-center gap-2">
                <SideTag side={c.side} />
                <span className="min-w-[44px] text-[13px] font-semibold text-foreground">{c.symbol}</span>
                <span className="text-secondary">{`${dates(c)} · ${c.days_held ?? '—'} 天`}</span>
                <span className={cn('ml-auto text-[13px] font-semibold tabular-nums', tone(c.pnl))}>{signed(c.pnl, 2, '$')}</span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-secondary">
                <span className="tabular-nums">{`${money(c.entry_price, 2)} → ${money(c.exit_price, 2)}`}</span>
                <ClosedReason c={c} />
                <span className={cn('ml-auto tabular-nums', tone(c.r_multiple))}>{fmtR(c.r_multiple)}</span>
              </div>
            </div>
          ))}
          <Pager page={cur} pages={pages} from={cur * CLOSED_PAGE + 1} to={cur * CLOSED_PAGE + shown.length} total={list.length} onPage={setPage} />
        </div>
      )}
    </section>
  )
}

/* ── 台账事件 ─────────────────────────────────────────── */

interface EventRow { e: LedgerEvent; from?: number }

function eventDetail({ e, from }: EventRow): string {
  switch (e.kind) {
    case 'stop_move': return `${from != null ? `$${fmt(from)} → ` : '→ '}$${fmt(e.stop_price)}`
    case 'entry_intent': return e.price ? `@ $${fmt(e.price)}` : ''
    case 'fill': return `${e.side === 'short' ? '空' : '多'} ${e.qty ?? '—'} 股 @ $${fmt(e.price)}`
    case 'partial_close':
    case 'close': return `${e.qty ?? '—'} 股 @ $${fmt(e.price)}`
    case 'exit_intent': return e.signal_price ? `收盘 $${fmt(e.signal_price)}` : ''
    case 'corp_action': return e.action_type === 'cash_dividend' ? `现金分红 $${fmt(e.rate)}/股` : (e.action_type ?? '')
    default: return [e.qty ? `${e.qty} 股` : '', e.price ? `@ $${fmt(e.price)}` : ''].filter(Boolean).join(' ')
  }
}

function EventResult({ e }: { e: LedgerEvent }) {
  const why = e.reason ? reasonLabel(e.reason) : ''
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {e.kind === 'close' && e.r_multiple != null && <span className={tone(e.r_multiple)}>{fmtR(e.r_multiple)}</span>}
      {why && <span className="text-muted">{why}</span>}
    </span>
  )
}

function LedgerPane({ rows, desktop }: { rows: EventRow[]; desktop: boolean }) {
  const [page, setPage] = useState(0)
  if (rows.length === 0) {
    return <EmptyState icon={Wallet} title="暂无台账事件" hint="纸面交易发生后，这里会记录成交、退出与风险调整。空仓不代表系统故障。" />
  }
  const pages = Math.max(1, Math.ceil(rows.length / EVENT_PAGE))
  const cur = Math.min(page, pages - 1)
  const shown = rows.slice(cur * EVENT_PAGE, (cur + 1) * EVENT_PAGE)
  const groups: { day: string; label: string; items: EventRow[] }[] = []
  for (const r of shown) {
    const day = etDay(r.e.at)
    const last = groups[groups.length - 1]
    if (last && last.day === day) last.items.push(r)
    else groups.push({ day, label: etDayLabel(r.e.at), items: [r] })
  }
  const pager = <Pager page={cur} pages={pages} from={cur * EVENT_PAGE + 1} to={cur * EVENT_PAGE + shown.length} total={rows.length} onPage={setPage} />
  if (!desktop) {
    return (
      <div className="overflow-hidden rounded-card border border-border bg-surface text-xs">
        {groups.map((g) => (
          <div key={g.day}>
            <div className="bg-elevated px-3 py-1.5 font-semibold text-foreground">{g.label}</div>
            {g.items.map((r) => (
              <div key={r.e.seq} data-event={r.e.seq} className="border-t border-border/70 px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="tabular-nums text-secondary">{etClock(r.e.at)}</span>
                  <span className="text-foreground">{eventLabel(r.e)}</span>
                  <span className="font-semibold text-foreground">{r.e.symbol ?? ''}</span>
                  <span className="ml-auto"><EventResult e={r.e} /></span>
                </div>
                <div className="mt-0.5 tabular-nums text-secondary">{eventDetail(r)}</div>
              </div>
            ))}
          </div>
        ))}
        {pager}
      </div>
    )
  }
  return (
    <div className="overflow-x-auto rounded-card border border-border bg-surface">
      <table className="w-full min-w-[720px] text-[13px] tabular-nums">
        <thead><tr className="h-8 bg-elevated text-[11.5px] text-muted">
          {['时间 ET', '事件', '代码', '明细', '结果'].map((h) => <th key={h} className="whitespace-nowrap px-3 text-left font-medium">{h}</th>)}
        </tr></thead>
        <tbody>
          {groups.map((g) => [
            <tr key={`d-${g.day}`} className="border-t border-border/70 bg-elevated/60 first:border-t-0">
              <td colSpan={5} className="px-3 py-1.5 text-xs font-semibold text-foreground">{g.label}</td>
            </tr>,
            ...g.items.map((r) => (
              <tr key={r.e.seq} data-event={r.e.seq} className="h-9 border-t border-border/50">
                <td className="whitespace-nowrap px-3 text-secondary">{etClock(r.e.at)}</td>
                <td className="whitespace-nowrap px-3 text-foreground">{eventLabel(r.e)}</td>
                <td className="whitespace-nowrap px-3 font-semibold text-foreground">{r.e.symbol ?? ''}</td>
                <td className="whitespace-nowrap px-3 text-secondary">{eventDetail(r)}</td>
                <td className="px-3"><EventResult e={r.e} /></td>
              </tr>
            )),
          ])}
        </tbody>
      </table>
      {pager}
    </div>
  )
}

/* ── Alpaca 镜像 ─────────────────────────────────────── */

const MIRROR_STATUS: Record<string, string> = {
  new: '待提交', submitted: '已提交', filled: '已成交', rejected: '被拒', skipped_not_mirrored: '未镜像（开启前的持仓）',
  skipped_zero_qty: '数量为 0', blocked_daily_cap: '超过每日上限', expired_out_of_session: '超时未提交',
}

/** Alpaca 纸面账户镜像: 内部成交原样发到 Alpaca 纸面账户, 对比真实成交价。 */
function AlpacaMirrorPanel({ data }: { data?: AlpacaMirrorPayload }) {
  const qc = useQueryClient()
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setAlpacaMirror(enabled, '持仓页手动切换'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alpaca-mirror'] }),
  })
  if (!data) return <div className="rounded-card border border-border bg-surface px-4 py-3 text-xs text-muted">镜像状态读取中</div>
  const s = data.summary
  const recent = [...data.orders].reverse().slice(0, 10)
  const k = 'mr-1.5 text-[11px] text-muted'
  return (
    <div data-mirror className="rounded-card border border-border bg-surface px-4 py-3 text-xs text-secondary">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <span className="text-foreground">{`Alpaca 纸面镜像 · ${s.enabled ? '运行中' : '已停用'}`}</span>
        <span><span className={k}>已镜像</span>{`${s.orders} 笔`}</span>
        <span><span className={k}>已成交</span>{`${s.filled} 笔`}</span>
        <span title="1 基点 = 0.01%，正数 = 比内部撮合差"><span className={k}>平均偏差</span>{s.avg_deviation_bps != null ? `${s.avg_deviation_bps} 基点` : '—'}</span>
        <span><span className={k}>每日上限</span>{`${s.max_orders_per_day} 次`}</span>
        <button
          type="button"
          disabled={toggle.isPending}
          onClick={() => { if (window.confirm(s.enabled ? '停用镜像下单？（内部纸面台账不受影响）' : '恢复镜像下单？')) toggle.mutate(!s.enabled) }}
          className="ml-auto h-7 whitespace-nowrap rounded-btn border border-border px-2.5 text-[11px] hover:bg-elevated disabled:opacity-50"
        >{s.enabled ? '一键停用' : '恢复'}</button>
      </div>
      {s.last_error && <div className="mt-2 text-warning">{`最近错误：${s.last_error}`}</div>}
      <div className="mt-2 text-[11px] leading-5 text-muted">{s.rules}</div>
      {recent.length > 0 && (
        <div className="mt-2 space-y-0.5 border-t border-border pt-2">
          {recent.map((o) => (
            <div key={o.cid} className="flex flex-wrap justify-between gap-2 tabular-nums">
              <span>{`${o.side === 'buy' ? '买' : '卖'} ${o.symbol} ${o.qty ?? o.ledger_qty} 股`}</span>
              <span>{`内部 $${fmt(o.ledger_price)} → Alpaca ${o.filled_price ? `$${fmt(o.filled_price)}` : '—'}`}</span>
              <span>{o.deviation_bps != null ? `${o.deviation_bps > 0 ? '+' : ''}${o.deviation_bps} 基点` : ''}</span>
              <span className="text-muted">{MIRROR_STATUS[o.status] ?? o.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ── 形态降级 ─────────────────────────────────────────── */

/** 形态降级 (四周负期望) 状态与人工恢复入口; 只观察期间持仓管理照常。 */
function SetupDegradation() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['setup-status'], queryFn: () => api.setupStatus(), refetchInterval: 60_000 })
  const restore = useMutation({
    mutationFn: (setup: string) => api.restoreSetup(setup, '持仓页人工恢复'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['setup-status'] }),
  })
  const degraded = (status.data?.setups ?? []).filter((s: SetupStatusRow) => s.status === 'degraded')
  if (status.isError) return <div className="text-warning">形态降级状态读取失败</div>
  if (degraded.length === 0) return null
  return (
    <>
      {degraded.map((s: SetupStatusRow) => {
        const name = SETUP_NAME[s.setup] ?? s.setup
        const avg = s.evidence?.avg_r
        return (
          <div key={s.setup} className="flex flex-wrap items-center gap-2 text-danger">
            <span>{`${name} 已降级为只观察（停新开仓）：近 4 周闭合 ${s.evidence?.samples ?? '—'} 笔，平均 ${typeof avg === 'number' ? `${avg.toFixed(2)}R` : '—'}`}</span>
            <button
              type="button"
              disabled={restore.isPending}
              className="rounded border border-border px-2 py-0.5 text-[11px] text-secondary hover:bg-elevated disabled:opacity-50"
              onClick={() => {
                if (window.confirm(`确认恢复「${name}」开新仓？降级依据：近 4 周平均 R 为负。`)) restore.mutate(s.setup)
              }}
            >
              恢复开仓
            </button>
          </div>
        )
      })}
    </>
  )
}

/* ── 总账六格 ─────────────────────────────────────────── */

function Kpi({ k, label, value, sub, valueClass, title }: { k: string; label: string; value: string; sub: React.ReactNode; valueClass: string; title?: string }) {
  return (
    <div data-kpi={k} title={title} className="min-w-0 bg-surface px-4 py-3 max-md:px-3 max-md:py-2.5">
      <div className="text-xs text-muted">{label}</div>
      <div className={cn('mt-0.5 truncate text-[22px] font-bold leading-7 tabular-nums max-md:text-[18px] max-md:leading-6', valueClass)}>{value}</div>
      <div className="mt-0.5 truncate text-xs text-secondary max-md:whitespace-normal">{sub}</div>
    </div>
  )
}

/* ── 页面 ─────────────────────────────────────────── */

type Tab = 'ledger' | 'mirror'

export function Portfolio() {
  const desktop = useDesktop()
  const [openSym, setOpenSym] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('ledger')
  const trader = useQuery({ queryKey: ['paper-trading-status'], queryFn: () => api.tradingStatus(), refetchInterval: 15_000 })
  const { data, isLoading, isError, error, refetch, isFetching, dataUpdatedAt } = useQuery<Overview>({
    queryKey: ['portfolio', 'overview'],
    queryFn: () => api.portfolioOverview() as unknown as Promise<Overview>,
    refetchInterval: 60_000,
  })
  const live = useQuery<PortfolioLive>({
    queryKey: ['portfolio', 'live'],
    queryFn: () => api.portfolioLive(),
    refetchInterval: LIVE_REFRESH_MS,
  })
  const mirror = useQuery<AlpacaMirrorPayload>({ queryKey: ['alpaca-mirror'], queryFn: () => api.alpacaMirror(), refetchInterval: 30_000 })

  // 台账事件: 机会卡状态流水不进 (后端已过滤, 这里兜底旧后端); 按时间顺序补出「止损从哪移来」与待开盘平仓
  const ledger = useMemo(() => {
    const chrono = (data?.events ?? []).filter((e) => e.kind !== 'opportunity')
    const stop = new Map<string, number>()
    const pending = new Map<string, string>()
    const rows: EventRow[] = []
    for (const e of chrono) {
      const sym = e.symbol ?? ''
      let from: number | undefined
      if (e.kind === 'fill') {
        if (e.stop_price != null) stop.set(sym, e.stop_price)
        pending.delete(sym)
      } else if (e.kind === 'stop_move') {
        from = stop.get(sym)
        if (e.stop_price != null) stop.set(sym, e.stop_price)
      } else if (e.kind === 'exit_intent') {
        pending.set(sym, e.reason ?? '')
      } else if (e.kind === 'close') {
        pending.delete(sym)
        stop.delete(sym)
      }
      rows.push({ e, from })
    }
    return { rows: rows.reverse(), pending }
  }, [data?.events])

  const derived = useMemo(() => {
    const rows = (live.data?.positions ?? []).map((p) => derive(p, ledger.pending))
    return rows.map((d, i) => ({ d, i })).sort((a, b) => a.d.rank - b.d.rank || a.i - b.i).map((x) => x.d)
  }, [live.data?.positions, ledger.pending])

  if (isLoading) {
    return <div><PageHeader title="持仓" subtitle="纸面账户 · 以成交台账为准" /><div className="p-8 text-secondary">加载中…</div></div>
  }

  // 首次加载失败: 只给错误卡, 不能用 $— 与“暂无台账事件”冒充空账本
  if (isError && !data) {
    return (
      <div>
        <PageHeader title="持仓" subtitle="纸面账户 · 以成交台账为准" />
        <div className="mx-auto max-w-3xl p-6">
        <div role="alert" className="rounded-lg border border-danger/40 p-4 text-sm">
          <div className="font-medium text-danger">持仓数据加载失败</div>
          <div className="mt-1 text-xs text-secondary">{error?.message || '未知错误'}</div>
          <button
            type="button"
            disabled={isFetching}
            onClick={() => void refetch()}
            className="mt-3 rounded border border-border px-3 py-1 text-xs hover:bg-elevated disabled:opacity-50"
          >重试</button>
        </div>
        </div>
      </div>
    )
  }

  const protections = trader.data?.protections
  const earningsCal = protections?.earnings_calendar
  const corpCal = protections?.corporate_actions
  const sm = live.data?.summary ?? null
  const positions = data?.positions ?? []
  const navDaily = data?.nav_daily ?? []
  const nav = sm?.nav ?? data?.nav
  const principal = sm?.initial_equity ?? (navDaily.length ? navDaily[0].nav : null)
  const total = nav != null && principal ? nav - principal : null
  const openRisk = sm?.open_risk ?? data?.open_risk
  const livePositions = live.data?.positions
  const nPos = livePositions?.length ?? positions.length

  // 状态胶囊: 正常时一行短词; 保护数据不可用 / 过期时下方给完整红字
  const session = trader.isError ? '状态读取失败' : trader.data?.scheduled ? (trader.data.in_session ? '交易时段检查中' : '等待常规时段') : '调度未就绪'
  const sources = new Set((livePositions ?? []).map((p) => PRICE_SOURCE[p.price_source] ?? p.price_source))
  const priceChip = sources.size === 0 ? null : sources.size === 1 ? `价格 ${[...sources][0]}` : '价格 实时与收盘混合'
  const alerts: string[] = []
  let earnOk = false
  let corpOk = false
  if (trader.data) {
    if (protections?.earnings_auto === 'connected') earnOk = true
    else alerts.push('财报日历不可用，已暂停新开仓')
    if (protections?.corporate_actions_auto === 'connected') corpOk = true
    else if (protections?.corporate_actions_auto === 'stale') {
      // 过期 = 最近一个交易日 08:00 ET 的定时刷新没成功 (按 NYSE 交易日算, 周末/休市不过期); 仍按后端口径暂停新开仓
      alerts.push(`公司行动数据已过期（更新于 ${scheduleStamp(corpCal?.fetched_at)}，${corpCal?.due_at ? `${scheduleStamp(corpCal.due_at)} 的定时刷新未成功` : '交易日 08:00 ET 自动刷新'}），已暂停新开仓`)
    } else alerts.push('公司行动数据不可用，已暂停新开仓')
  }
  const earnTitle = `来源 ${earningsCal?.source ?? '—'} · 更新于 ${earningsCal?.fetched_at ? `${formatScheduleDatePart(earningsCal.fetched_at)} ${formatScheduleTimePart(earningsCal.fetched_at)}` : '—'}`
  const lastRun = trader.data?.last_run_at ? scheduleStamp(trader.data.last_run_at) : '尚无本次启动后的盘中检查'
  const ms = mirror.data?.summary

  return (
    <div>
      <PageHeader title="持仓" subtitle="纸面账户 · 以成交台账为准" />
      <div className="mx-auto max-w-6xl px-4 pb-24 pt-4 md:px-8 md:pt-5">

      {isError && (
        <div role="alert" className="mb-4 rounded-lg border border-warning/40 px-3 py-2 text-xs text-warning">
          {`刷新失败，当前显示的是 ${fmtClock(dataUpdatedAt)} 的数据`}
        </div>
      )}

      <div data-status className="flex flex-wrap gap-1.5">
        <Chip data-chip="paper" title="当前结果仅作纸面验证，不等同可实盘策略"><i className="h-1.5 w-1.5 rounded-full bg-accent" />纸面账户 · 无真实下单</Chip>
        <Chip data-chip="session" danger={trader.isError} title={`最近检查 ${lastRun}`}><i className="h-1.5 w-1.5 rounded-full bg-muted" />{session}</Chip>
        {priceChip && <Chip data-chip="price" title={`每 ${sm?.refresh_seconds ?? LIVE_REFRESH_MS / 1000} 秒刷新`}>{priceChip}</Chip>}
        {trader.data && <Chip data-chip="earnings" danger={!earnOk} title={earnTitle}>{earnOk ? '财报保护 ✓' : '财报保护 ✕'}</Chip>}
        {trader.data && <Chip data-chip="corp" danger={!corpOk} title={corpCal?.fetched_at ? `更新于 ${scheduleStamp(corpCal.fetched_at)}` : undefined}>{corpOk ? '公司行动保护 ✓' : '公司行动保护 ✕'}</Chip>}
        <Chip data-chip="notify">{`通知 ${trader.data?.notifications?.label || '状态待查询'}`}</Chip>
      </div>
      {(alerts.length > 0 || trader.data?.last_error) && (
        <div data-alerts className="mt-2 space-y-1 rounded-card border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">
          {trader.data?.last_error && <div className="text-danger">{trader.data.last_error}</div>}
          {alerts.map((a) => <div key={a} className="text-danger">{a}</div>)}
        </div>
      )}
      <div className="mt-2 space-y-1 text-xs empty:hidden"><SetupDegradation /></div>

      {/* 总账六格: 手机 2 列, 平板 3 列, 桌面 6 列 */}
      <div data-nav-cards className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border md:grid-cols-3 xl:grid-cols-6">
        <Kpi k="nav" label="净值" value={money(nav)} valueClass="text-foreground"
          sub={`现金 ${money(sm?.cash ?? data?.cash)}`} />
        <Kpi k="total" label="累计收益" value={signed(total, 0, '$')} valueClass={tone(total)}
          sub={<><span className={tone(sm?.total_return_pct)}>{pct(sm?.total_return_pct ?? (total != null && principal ? total / principal * 100 : null))}</span>{` · 本金 ${money(principal)}`}</>} />
        <Kpi k="unrealized" label="浮动盈亏" value={signed(sm?.unrealized_pnl, 0, '$')} valueClass={tone(sm?.unrealized_pnl)}
          sub={`持仓 ${nPos} 笔`} />
        <Kpi k="realized" label="已实现" value={signed(sm?.realized_pnl, 0, '$')} valueClass={tone(sm?.realized_pnl)}
          title="含已平仓回合、仍持有回合已减仓部分、分红与手续费"
          sub={`平仓 ${sm?.closed_trades ?? live.data?.closed.length ?? 0} 笔${(livePositions ?? []).some((p) => p.partial_taken) ? ' · 含持仓减仓' : ''}`} />
        <Kpi k="risk" label="组合风险" value={money(openRisk)} valueClass="text-warning"
          title="全部持仓打到当前止损时 相对成本还会亏的金额 (止损已过成本的计 0)"
          sub={`打到止损 · 净值 ${openRisk != null && nav ? `${(openRisk / nav * 100).toFixed(2)}%` : '—'}`} />
        <Kpi k="buying" label="可用资金" value={money(sm?.buying_power)} valueClass="text-foreground"
          sub={sm?.margin_used ? <span data-margin>{`保证金 ${money(sm.margin_used)}`}</span> : '保证金 —'} />
      </div>

      {/* 净值曲线: 日终净值折线 (红色统一), 虚线为本金基线 */}
      {navDaily.length > 1 && (() => {
        const first = navDaily[0].nav
        return (
          <div data-equity-card className="mt-4 rounded-card border border-border bg-surface px-4 pb-3 pt-3">
            <div className="mb-2 flex items-baseline justify-between text-xs">
              <span className="text-secondary">净值曲线 · 日终</span>
              <span className="tabular-nums text-muted">
                {`近 ${navDaily.length} 个交易日 · ${navDaily[0].date.slice(5)} → ${navDaily[navDaily.length - 1].date.slice(5)}`}
              </span>
            </div>
            <EquityCurve points={navDaily} principal={first} />
          </div>
        )
      })()}

      {/* 当前持仓: 一行一只, 点开看止损轨迹与风险 */}
      <SectionHead title="当前持仓" count={nPos}
        right={nPos > 1 && <span className="ml-auto text-xs text-muted">排序 到期 → 近止损 → 近目标</span>} />
      {live.isError && <div className="mb-2 text-xs text-warning">实时持仓刷新失败，显示上次结果</div>}
      {nPos === 0 ? (
        <div className="rounded-card border border-border bg-surface">
          <EmptyState icon={Wallet} title="空仓" hint="波段账户当前无持仓。机会满足条件并完成纸面撮合后，这里会显示持仓。" />
        </div>
      ) : live.data ? (
        desktop
          ? <PositionsTable rows={derived} open={openSym} onToggle={(s) => setOpenSym(openSym === s ? null : s)} />
          : <PositionsCards rows={derived} open={openSym} onToggle={(s) => setOpenSym(openSym === s ? null : s)} />
      ) : (
        <div className="overflow-hidden rounded-card border border-border bg-surface text-xs">
          {positions.map((p) => (
            <div key={p.symbol} className="flex items-center gap-3 border-t border-border/70 px-3 py-2.5 first:border-t-0">
              <SideTag side={p.qty < 0 ? 'short' : 'long'} />
              <span className="font-semibold text-foreground">{p.symbol}</span>
              <span className="text-secondary">{`${Math.abs(p.qty)} 股 @ $${fmt(p.avg_cost)}`}</span>
              <span className="ml-auto text-muted">{`入场 ${p.entry_date} · 止损 ${p.stop_price ? `$${fmt(p.stop_price)}` : '—'}`}</span>
            </div>
          ))}
        </div>
      )}

      <ClosedSection closed={live.data?.closed ?? []} desktop={desktop} summary={sm} />

      {/* 记录: 台账事件 / Alpaca 镜像 */}
      <SectionHead title="记录" right={
        <Seg<Tab> attr="data-tab" value={tab} onChange={setTab}
          items={[['ledger', `台账 ${ledger.rows.length}`], ['mirror', `Alpaca 镜像${ms ? ` · ${ms.last_error ? '异常' : ms.enabled ? '运行中' : '已停用'}` : ''}`]]} />
      } />
      {tab === 'ledger' ? <LedgerPane rows={ledger.rows} desktop={desktop} /> : <AlpacaMirrorPanel data={mirror.data} />}
      </div>
    </div>
  )
}
