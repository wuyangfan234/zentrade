import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Wallet } from 'lucide-react'
import { api, type StopMove, type AlpacaMirrorPayload, type ClosedTrip, type LivePosition, type PortfolioLive, type SetupStatusRow } from '@/lib/api'
import { formatScheduleDatePart, formatScheduleTimePart } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { EquityCurve } from '@/components/EquityCurve'

/**
 * 持仓页 — ZenTrade 波段纸面账户 (P3)。
 *
 * 净值曲线 / 当前持仓三线 (入场/失效/目标) / 台账事件流水。
 * 全部数据来自 append-only 台账重放, 账面即真相。
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

function SideTag({ side }: { side?: 'long' | 'short' }) {
  if (side !== 'short') return null
  return <span data-side="short" className="ml-1.5 rounded bg-rose-500/10 px-1 text-[10px] font-medium text-rose-700 dark:bg-rose-900/40 dark:text-rose-300">空</span>
}

const KIND_LABEL: Record<string, string> = {
  fill: '开仓',
  partial_close: '部分止盈',
  close: '平仓',
  stop_move: '止损上移',
  corp_action: '除权',
  adjust: '冲正',
  opportunity: '机会',
  entry_intent: '待下一报价撮合',
  exit_intent: '待次交易日开盘退出',
}

/** 台账事件中文名; 止损调整按持仓方向区分: 多头止损上移, 空头止损下移 */
function eventLabel(e: LedgerEvent): string {
  if (e.kind === 'stop_move') return e.position_side === 'short' ? '止损下移' : '止损上移'
  return KIND_LABEL[e.kind] ?? e.kind
}

/** R 倍数保留 2 位 */
function fmtR(r: number | null | undefined): string {
  return r != null && Number.isFinite(r) ? `${r.toFixed(2)}R` : '—'
}

function fmt(n: number | undefined | null, digits = 2): string {
  if (n === undefined || n === null) return '—'
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 本地时间 HH:MM:SS */
function fmtClock(ts: number): string {
  const d = new Date(ts)
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
}

const SETUP_NAME: Record<string, string> = { breakout: '突破', pullback: '回踩', 'strategy:swing': '机会板' }

/** 持仓现价刷新间隔 (2026-10-02 产品决策: 5 秒)。 */
export const LIVE_REFRESH_MS = 5_000

function scheduleStamp(iso: string | null | undefined): string {
  return iso ? `${formatScheduleDatePart(iso)} ${formatScheduleTimePart(iso)}` : '—'
}

function signed(n: number | null | undefined, digits = 2, prefix = ''): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '-' : ''
  return `${sign}${prefix}${fmt(Math.abs(n), digits)}`
}

function pct(n: number | null | undefined, digits = 2): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—'
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

function tone(n: number | null | undefined): string {
  if (!n) return 'text-secondary'
  return n > 0 ? 'text-bull' : 'text-bear'
}

const PRICE_SOURCE: Record<string, string> = { tickflow: '实时', iex: 'IEX', last_close: '最近收盘', cost: '成本价' }

/** 0.18.0: 初始→当前止损 · 调整 N 次; 有调整时可展开逐次记录 */
export function StopTrail({ initial, current, moves }: { initial?: number | null; current?: number | null; moves?: StopMove[] }) {
  const [open, setOpen] = useState(false)
  const list = moves ?? []
  const d = (n?: number | null) => (n ? `$${fmt(n)}` : '—')
  return (
    <span data-stop-trail className="col-span-2 sm:col-span-4">
      <span>{`止损 ${d(initial)}→${d(current)} · 调整 ${list.length} 次`}</span>
      {list.length > 0 && (
        <button type="button" data-stop-toggle onClick={() => setOpen(!open)} className="ml-2 text-[11px] text-muted underline">
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

function PositionRow({ p }: { p: LivePosition }) {
  const src = PRICE_SOURCE[p.price_source] ?? p.price_source
  const age = p.price_age_s != null ? ` ${Math.round(p.price_age_s)}s` : ''
  return (
    <div data-position={p.symbol} className="rounded-lg border border-border p-3 text-xs">
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium">{p.symbol}<SideTag side={p.side} /><span data-setup title={p.setup_type ?? undefined} className="ml-2 text-[11px] text-muted">{p.setup_label ?? SETUP_NAME[p.setup_type ?? ''] ?? p.setup_type ?? ''}</span></span>
        <span className={tone(p.unrealized_pnl)}>{`${signed(p.unrealized_pnl, 2, '$')}（${pct(p.unrealized_pct)}）`}</span>
      </div>
      <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-secondary sm:grid-cols-4">
        <span>{`${p.side === 'short' ? '卖空' : '入场'} $${fmt(p.entry_price)}`}</span>
        <span>{`现价 $${fmt(p.last_price)}`}<span className="ml-1 text-[10px] text-muted">{`${src}${age}`}</span></span>
        <span data-qty>{p.accounting === 'contract'
          ? `${p.side === 'short' ? '空' : '多'} ${p.qty} 股 · 名义 $${fmt(p.notional ?? p.qty * p.last_price, 0)}`
          : p.side === 'short' ? `空 ${p.qty} 股 · 回补负债 $${fmt(Math.abs(p.market_value), 0)}` : `${p.qty} 股 · 市值 $${fmt(p.market_value, 0)}`}</span>
        <span>{`第 ${p.days_held ?? '—'}/${p.max_holding_days ?? 7} 天`}</span>
        <span>{`止损 ${p.stop_price ? `$${fmt(p.stop_price)}` : '—'}`}<span className="ml-1 text-muted">{pct(p.stop_distance_pct)}</span></span>
        <span>{`目标 ${p.target_zone?.length ? `$${fmt(p.target_zone[p.side === 'short' ? 1 : 0])}` : '—'}`}<span className="ml-1 text-muted">{pct(p.target_distance_pct)}</span></span>
        <span title="浮动盈亏，R = 以止损距离为 1 份风险">{`当前 ${p.r_now != null ? `${p.r_now.toFixed(2)}R` : '—'}`}</span>
        <span>{`入场日 ${p.entry_date ?? '—'}${p.partial_taken ? ' · 已减半' : ''}`}</span>
        <StopTrail initial={p.stop_initial} current={p.stop_price} moves={p.stop_moves} />
        {p.accounting === 'contract' && p.risk_pct != null && (
          <span data-risk title="开仓时这笔承担的风险占净值的比例">{`风险 ${(p.risk_pct * 100).toFixed(2)}%${p.entry_tier === 'trial' ? ' · 试用' : ''}`}</span>
        )}
      </div>
    </div>
  )
}

function ClosedRow({ c }: { c: ClosedTrip }) {
  return (
    <div data-closed={c.symbol} className="flex flex-wrap items-baseline justify-between gap-2 rounded border border-border/60 px-3 py-1.5 text-xs">
      <span className="text-foreground">
        {c.symbol}
        <SideTag side={c.side} />
        <span className="ml-2 text-muted">{`${c.entry_date ?? '—'} → ${c.exit_date ?? '—'} · ${c.days_held ?? '—'} 天`}</span>
      </span>
      <span className="text-secondary">{`$${fmt(c.entry_price)} → $${fmt(c.exit_price)}`}</span>
      <span className={tone(c.pnl)}>{`${signed(c.pnl, 2, '$')}（${pct(c.return_pct)}）`}</span>
      <span className="text-muted">{`${fmtR(c.r_multiple)} · ${c.exit_reason_label}${c.partial_exits ? ` · 分 ${c.partial_exits + 1} 次${c.side === 'short' ? '回补' : '卖出'}` : ''} · 费 $${fmt(c.fees)}`}</span>
    </div>
  )
}

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

const MIRROR_STATUS: Record<string, string> = {
  new: '待提交', submitted: '已提交', filled: '已成交', rejected: '被拒', skipped_not_mirrored: '未镜像（开启前的持仓）',
  skipped_zero_qty: '数量为 0', blocked_daily_cap: '超过每日上限', expired_out_of_session: '超时未提交',
}

/** Alpaca 纸面账户镜像: 内部成交原样发到 Alpaca 纸面账户, 对比真实成交价。 */
function AlpacaMirrorPanel() {
  const qc = useQueryClient()
  const q = useQuery<AlpacaMirrorPayload>({ queryKey: ['alpaca-mirror'], queryFn: () => api.alpacaMirror(), refetchInterval: 30_000 })
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setAlpacaMirror(enabled, '持仓页手动切换'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alpaca-mirror'] }),
  })
  if (!q.data) return null
  const s = q.data.summary
  const recent = [...q.data.orders].reverse().slice(0, 10)
  return (
    <div data-mirror className="mt-6 rounded-lg border border-border p-3 text-xs text-secondary">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-foreground">{`Alpaca 纸面镜像 · ${s.enabled ? '运行中' : '已停用'}`}</span>
        <button
          type="button"
          disabled={toggle.isPending}
          onClick={() => { if (window.confirm(s.enabled ? '停用镜像下单？（内部纸面台账不受影响）' : '恢复镜像下单？')) toggle.mutate(!s.enabled) }}
          className="rounded border border-border px-2 py-0.5 text-[11px] hover:bg-elevated disabled:opacity-50"
        >{s.enabled ? '一键停用' : '恢复'}</button>
      </div>
      <div className="mt-1">{`已镜像 ${s.orders} 笔 · 已成交 ${s.filled} 笔 · 平均执行偏差 ${s.avg_deviation_bps != null ? `${s.avg_deviation_bps} 基点` : '—'}（1 基点 = 0.01%，正数 = 比内部撮合差）`}</div>
      {s.last_error && <div className="mt-1 text-warning">{`最近错误：${s.last_error}`}</div>}
      <div className="mt-1 text-muted">{s.rules}</div>
      {recent.length > 0 && (
        <div className="mt-2 space-y-0.5">
          {recent.map((o) => (
            <div key={o.cid} className="flex flex-wrap justify-between gap-2">
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

export function Portfolio() {
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

  if (isLoading) {
    return <div><PageHeader title="持仓" subtitle="波段纸面账户 · 以成交台账为准" /><div className="p-8 text-secondary">加载中…</div></div>
  }

  // 首次加载失败: 只给错误卡, 不能用 $— 与“暂无台账事件”冒充空账本
  if (isError && !data) {
    return (
      <div>
        <PageHeader title="持仓" subtitle="波段纸面账户 · 以成交台账为准" />
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

  const positions = data?.positions ?? []
  // 机会卡状态流水不进台账事件区 (后端已过滤, 这里兜底旧后端)
  const events = [...(data?.events ?? [])].filter((e) => e.kind !== 'opportunity').reverse()
  const navDaily = data?.nav_daily ?? []

  return (
    <div>
      <PageHeader title="持仓" subtitle="波段纸面账户 · 以成交台账为准" />
      <div className="mx-auto max-w-3xl p-6">

      {isError && (
        <div role="alert" className="mb-4 rounded-lg border border-warning/40 px-3 py-2 text-xs text-warning">
          {`刷新失败，当前显示的是 ${fmtClock(dataUpdatedAt)} 的数据`}
        </div>
      )}

      <div className="mb-4 rounded-lg border border-border p-3 text-xs text-secondary space-y-1">
        <div>纸面运行 · {trader.isError ? '状态读取失败' : trader.data?.scheduled ? (trader.data.in_session ? '交易时段检查中' : '等待美股常规时段') : '调度未就绪'} · 无真实下单</div>
        <div>最近检查 {trader.data?.last_run_at ? scheduleStamp(trader.data.last_run_at) : '尚无本次启动后的盘中检查'} · 通知 {trader.data?.notifications?.label || '状态待查询'}</div>
        {trader.data?.last_error && <div className="text-danger">{trader.data.last_error}</div>}
        {trader.data && (
          <>
            {protections?.earnings_auto === 'connected'
              ? <div>{`财报保护：已接入（来源 ${earningsCal?.source ?? '—'}，更新于 ${earningsCal?.fetched_at ? `${formatScheduleDatePart(earningsCal.fetched_at)} ${formatScheduleTimePart(earningsCal.fetched_at)}` : '—'}）`}</div>
              : <div className="text-danger">财报日历不可用，已暂停新开仓</div>}
            {protections?.corporate_actions_auto === 'connected'
              ? <div>公司行动保护：已接入</div>
              : protections?.corporate_actions_auto === 'stale'
                // 过期 = 最近一个交易日 08:00 ET 的定时刷新没成功 (按 NYSE 交易日算, 周末/休市不过期); 仍按后端口径暂停新开仓
                ? <div className="text-danger">{`公司行动数据已过期（更新于 ${scheduleStamp(corpCal?.fetched_at)}，${corpCal?.due_at ? `${scheduleStamp(corpCal.due_at)} 的定时刷新未成功` : '交易日 08:00 ET 自动刷新'}），已暂停新开仓`}</div>
                : <div className="text-danger">公司行动数据不可用，已暂停新开仓</div>}
          </>
        )}
        <SetupDegradation />
        <div className="text-warning">当前结果仅作纸面验证，不等同可实盘策略。</div>
      </div>
      {/* 净值卡 */}
      <div data-nav-cards className="grid grid-cols-3 gap-2 sm:gap-3">
        <div className="min-w-0 rounded-lg border border-border p-3 sm:p-4">
          <div className="text-xs text-secondary">净值</div>
          <div className="mt-1 truncate text-[16px] leading-6 font-semibold tabular-nums text-foreground sm:text-xl">${fmt(data?.nav, 0)}</div>
        </div>
        <div className="min-w-0 rounded-lg border border-border p-3 sm:p-4">
          <div className="text-xs text-secondary">现金</div>
          <div className="mt-1 truncate text-[16px] leading-6 font-semibold tabular-nums text-foreground sm:text-xl">${fmt(data?.cash, 0)}</div>
        </div>
        <div className="min-w-0 rounded-lg border border-border p-3 sm:p-4">
          <div className="text-xs text-secondary">组合风险</div>
          <div className="mt-1 truncate text-[16px] leading-6 font-semibold tabular-nums text-danger sm:text-xl">${fmt(data?.open_risk, 0)}</div>
        </div>
      </div>

      {/* 净值曲线: 日终净值折线 (红色统一), 虚线为本金基线 */}
      {navDaily.length > 1 && (() => {
        const first = navDaily[0].nav
        return (
          <div data-equity-card className="mt-4 rounded-lg border border-border p-4">
            <div className="mb-3 flex items-baseline justify-between text-xs">
              <span className="text-secondary">净值曲线 · 日终</span>
              <span className="tabular-nums text-muted">
                {`近 ${navDaily.length} 个交易日 · ${navDaily[0].date.slice(5)} → ${navDaily[navDaily.length - 1].date.slice(5)}`}
              </span>
            </div>
            <EquityCurve points={navDaily} principal={first} />
          </div>
        )
      })()}

      {/* 账户汇总 (5 秒刷新) */}
      {live.data?.summary && (() => {
        const sm = live.data.summary
        return (
          <div data-summary className="mt-4 flex flex-wrap gap-x-5 gap-y-1 rounded-lg border border-border px-4 py-3 text-xs text-secondary">
            <span>{'总收益 '}<span className={tone(sm.total_return_pct)}>{pct(sm.total_return_pct)}</span>{`（本金 $${fmt(sm.initial_equity, 0)}）`}</span>
            <span className={tone(sm.realized_pnl)}>{`已实现 ${signed(sm.realized_pnl, 2, '$')}`}</span>
            <span className={tone(sm.unrealized_pnl)}>{`未实现 ${signed(sm.unrealized_pnl, 2, '$')}`}</span>
            {(sm.by_scope ?? []).length > 0 ? (sm.by_scope ?? []).map((g) => (
              <span key={g.key} data-scope={g.key}>{`${g.label} 已平仓 ${g.closed_trades} 笔 · 胜率 ${Math.round(g.win_rate * 100)}% · 平均 ${g.avg_r != null ? `${g.avg_r}R` : '—'}`}</span>
            )) : (
              <span>{`已平仓 ${sm.closed_trades} 笔 · 胜率 ${sm.win_rate != null ? `${Math.round(sm.win_rate * 100)}%` : '—'} · 平均 ${sm.avg_r != null ? `${sm.avg_r}R` : '—'}`}</span>
            )}
            {sm.margin_used ? <span data-margin>{`保证金 $${fmt(sm.margin_used, 0)} · 可开 $${fmt(sm.buying_power, 0)}`}</span> : null}
            <span className="text-muted">{`每 ${sm.refresh_seconds} 秒刷新`}</span>
          </div>
        )
      })()}

      {/* 当前持仓 */}
      <div className="mt-6">
        <div className="text-xs uppercase tracking-wider text-muted">当前持仓 ({live.data?.positions.length ?? positions.length})</div>
        {live.isError && <div className="mt-2 text-xs text-warning">实时持仓刷新失败，显示上次结果</div>}
        {(live.data?.positions.length ?? positions.length) === 0 ? (
          <div className="mt-3">
            <EmptyState icon={Wallet} title="空仓" hint="波段账户当前无持仓。机会满足条件并完成纸面撮合后，这里会显示持仓。" />
          </div>
        ) : live.data ? (
          <div className="mt-3 space-y-2">
            {live.data.positions.map((p) => <PositionRow key={p.symbol} p={p} />)}
          </div>
        ) : (
          <div className="mt-3 space-y-2">
            {positions.map((p) => (
              <div key={p.symbol} className="rounded-lg border border-border p-3">
                <div className="flex justify-between text-sm">
                  <span className="font-medium">{p.symbol}{p.qty < 0 && <SideTag side="short" />}</span>
                  <span className="text-secondary">{Math.abs(p.qty)} 股 @ ${fmt(p.avg_cost)}</span>
                </div>
                <div className="mt-1 flex justify-between text-xs text-muted">
                  <span>入场 {p.entry_date}</span>
                  <span>止损 {p.stop_price ? `$${fmt(p.stop_price)}` : '—'}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 已平仓 */}
      <div className="mt-6">
        <div className="text-xs uppercase tracking-wider text-muted">已平仓 ({live.data?.closed.length ?? 0})</div>
        {(live.data?.closed.length ?? 0) === 0 ? (
          <div className="mt-2 text-xs text-muted">尚无完整平仓的回合。</div>
        ) : (
          <div className="mt-2 space-y-1">
            {live.data!.closed.map((c) => <ClosedRow key={`${c.symbol}-${c.entry_date}-${c.exit_date}`} c={c} />)}
          </div>
        )}
      </div>

      <AlpacaMirrorPanel />

      {/* 台账事件流水 */}
      <div className="mt-6">
        <div className="text-xs uppercase tracking-wider text-muted">台账事件 ({events.length})</div>
        {events.length === 0 && (
          <div className="mt-3">
            <EmptyState icon={Wallet} title="暂无台账事件" hint="纸面交易发生后，这里会记录成交、退出与风险调整。空仓不代表系统故障。" />
          </div>
        )}
        <div className="mt-2 space-y-1">
          {events.slice(0, 30).map((e) => (
            <div key={e.seq} className="flex justify-between rounded border border-border/60 px-3 py-1.5 text-xs">
              <span className="text-secondary">
                <span className="text-muted">#{e.seq}</span> {eventLabel(e)}
                {e.symbol ? ` ${e.symbol}` : ''}
              </span>
              <span className="text-muted">
                {e.qty ? `${e.qty}股 ` : ''}{e.price ? `@$${fmt(e.price)}` : ''}
                {e.r_multiple != null ? ` ${fmtR(e.r_multiple)}` : ''}
                {e.reason ? ` · ${e.reason}` : ''}
              </span>
            </div>
          ))}
        </div>
      </div>
      </div>
    </div>
  )
}
