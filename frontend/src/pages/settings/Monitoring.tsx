import { useState, useCallback, useEffect, createContext, useContext } from 'react'
import { useQueryClient, useQuery } from '@tanstack/react-query'
import {
  Activity,
  Wifi,
  Zap,
} from 'lucide-react'
import {
  usePreferences,
  useQuoteStatus,
  useQuoteInterval,
  useCapabilities,
} from '@/lib/useSharedQueries'
import { useUpdateQuoteInterval, useToggleRealtimeQuotes } from '@/lib/useSharedMutations'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCardFlash, cardFlashCls } from '@/lib/useCardFlash'

// 卡片定位锚点: highlight=<anchor> 时该卡片滚动到视口中央并闪烁高亮。
// 其他页面用 /settings?tab=monitoring&highlight=<anchor> 精确引导用户到某张卡片。
// 推送通道与推送日志已移到「系统设置」(2026-11-13): /settings?tab=system&highlight=push
const HighlightContext = createContext('')

// 页面 → 显示名
const PAGE_LABELS: Record<string, string> = {
  watchlist: '自选页',
  opportunities: '机会页',
}
const PAGE_DESC: Record<string, string> = {
  watchlist: '收到行情推送时刷新自选页数据',
  opportunities: '状态变化（成交、受限、名额、平仓）时即时刷新 · 关闭后每 30 / 60 秒刷新',
}

// ===== 导出为 Panel 组件 (由 Settings.tsx 嵌入) =====

export function SettingsMonitoringPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const { data: caps } = useCapabilities()
  const { data: quoteStatus } = useQuoteStatus()
  const { data: intervalData } = useQuoteInterval()
  const updateInterval = useUpdateQuoteInterval()
  const toggleQuote = useToggleRealtimeQuotes()
  // 实时模式以 quote_status 为准 (数据源无关): full_market=全市场 / none=不可用
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  // 分时图实时刷新间隔 (秒), 与后端 [3,60] clamp 对齐; 默认 6
  const intradayInterval = prefs?.minute_intraday_refresh_interval ?? 6
  // 滑块本地草稿: 拖动时即时反馈, 停顿 2s 后落库 (与行情轮询滑块一致)
  const [intradayIntervalDraft, setIntradayIntervalDraft] = useState(intradayInterval)
  // 盘中分钟增量 (Expert 专有): 间隔 (秒), 与后端 [3,120] clamp 对齐; 默认 6
  const minuteRefreshInterval = prefs?.minute_refresh_interval ?? 6
  const [minuteRefreshIntervalDraft, setMinuteRefreshIntervalDraft] = useState(minuteRefreshInterval)
  // 盘中增量服务状态 (15s 轮询; 无服务时 available=false)
  const refreshStatus = useQuery({
    queryKey: ['minute-refresh-status'],
    queryFn: api.minuteRefreshStatus,
    refetchInterval: 15000,
  })
  const refreshPages = prefs?.sse_refresh_pages ?? {}
  // 全量分钟 = intraday.universe 能力 (TickFlow Expert 专有): 标的池单请求拉全市场当日分钟,
  // 修复轮的 intraday.batch 与其同档, 见后端 minute_refresh 服务
  const hasFullMinuteCap = !!caps?.capabilities?.['intraday.universe']
  const rs = refreshStatus.data
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  // 管道/数据修正运行期间实时行情被临时暂停 — 此时禁止开启
  const isPaused = quoteStatus?.paused ?? false
  const interval = intervalData?.interval ?? 6
  const minInterval = intervalData?.min_interval ?? 6
  const maxInterval = intervalData?.max_interval ?? 60
  const [intervalDraft, setIntervalDraft] = useState(interval)

  const save = useCallback(async (cfg: Record<string, unknown>) => {
    try {
      await api.updateRealtimeMonitorConfig(cfg)
      qc.invalidateQueries({ queryKey: QK.preferences })
    } catch (e) {
      // 忽略 — Toast 已在 request 层处理
    }
  }, [qc])

  const handleToggleQuote = useCallback(async (enabled: boolean) => {
    await toggleQuote.mutateAsync(enabled)
    qc.invalidateQueries({ queryKey: QK.preferences })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }, [toggleQuote, qc])

  useEffect(() => {
    setIntervalDraft(interval)
  }, [interval])

  useEffect(() => {
    if (intervalDraft === interval) return
    const t = window.setTimeout(() => {
      updateInterval.mutate(intervalDraft)
    }, 2000)
    return () => window.clearTimeout(t)
  }, [intervalDraft, interval, updateInterval])

  // 分时刷新间隔: 服务端值变化时同步本地草稿
  useEffect(() => {
    setIntradayIntervalDraft(intradayInterval)
  }, [intradayInterval])

  // 分时刷新间隔: 草稿与已保存值不同时, 2s 防抖落库
  useEffect(() => {
    if (intradayIntervalDraft === intradayInterval) return
    const t = window.setTimeout(() => {
      save({ minute_intraday_refresh_interval: intradayIntervalDraft })
    }, 2000)
    return () => window.clearTimeout(t)
  }, [intradayIntervalDraft, intradayInterval, save])

  // 盘中增量间隔: 服务端值变化时同步本地草稿
  useEffect(() => {
    setMinuteRefreshIntervalDraft(minuteRefreshInterval)
  }, [minuteRefreshInterval])

  // 盘中增量间隔: 草稿与已保存值不同时, 2s 防抖落库
  useEffect(() => {
    if (minuteRefreshIntervalDraft === minuteRefreshInterval) return
    const t = window.setTimeout(() => {
      save({ minute_refresh_interval: minuteRefreshIntervalDraft })
    }, 2000)
    return () => window.clearTimeout(t)
  }, [minuteRefreshIntervalDraft, minuteRefreshInterval, save])

  return (
    <HighlightContext.Provider value={highlight ?? ''}>
    <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-6 max-w-5xl">
      {/* ========== 左列 ========== */}
      <div className="space-y-6">
        {/* 行情状态 — 开关 + 间隔 */}
        <Card icon={Activity} title="行情轮询" anchor="quotes">
          <ToggleRow
            label="实时行情"
            desc={
              isPaused ? '数据同步运行中，已临时暂停'
              : isRunning && isTrading ? '运行中'
              : isRunning ? '运行中（非交易时段）'
              : '已关闭'
            }
            checked={realtimeEnabled}
            onChange={handleToggleQuote}
            disabled={isPaused}
          />

          <div className="mt-3 pt-3 border-t border-border">
            <div className="flex items-center justify-between gap-4 py-1">
              <div className="min-w-0">
                <div className="text-sm text-foreground">轮询间隔</div>
                <div className="text-[11px] text-muted">
                  每轮拉取全市场行情的时间间隔
                </div>
              </div>
              <span className="text-[11px] font-mono text-foreground shrink-0 tabular-nums">
                {intervalDraft < 1 ? intervalDraft.toFixed(1) : intervalDraft.toFixed(0)}s
              </span>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <input
                type="range"
                min={minInterval}
                max={maxInterval}
                step={minInterval < 1 ? 0.1 : minInterval < 3 ? 0.5 : 1}
                value={intervalDraft}
                onChange={(e) => setIntervalDraft(parseFloat(e.target.value))}
                className="flex-1 h-1 accent-accent cursor-pointer"
              />
              <span className="text-[11px] text-muted shrink-0">
                {intervalDraft !== interval ? '2秒后保存' : `${minInterval}s — ${maxInterval}s`}
              </span>
            </div>
          </div>
        </Card>

        <Card icon={Wifi} title="页面实时刷新">
          <p className="text-xs text-secondary mb-4">
            实时刷新的页面 · 不影响行情轮询与监控
          </p>
          <div className="space-y-2">
            {Object.entries(PAGE_LABELS).map(([key, label]) => (
              <ToggleRow
                key={key}
                label={label}
                desc={PAGE_DESC[key] ?? `实时刷新${label}数据`}
                checked={refreshPages[key] !== false}
                onChange={(v) => save({ sse_refresh_pages: { ...refreshPages, [key]: v } })}
              />
            ))}
          </div>
        </Card>

        {/* 自选列表分时图实时刷新 (默认关闭, 开启后盘中按设定间隔轮询刷新分时数据) */}
        <Card icon={Activity} title="分时图刷新" anchor="intraday-refresh">
          <ToggleRow
            label="自选/策略分时图实时刷新"
            desc={`开启后自选与策略列表的分时图盘中每 ${intradayInterval} 秒刷新（需分钟 K 批量数据与实时行情）· 关闭后仅在打开页面时更新，可手动刷新`}
            checked={prefs?.minute_intraday_refresh ?? false}
            onChange={(v) => save({ minute_intraday_refresh: v })}
          />
          <div className="mt-3 pt-3 border-t border-border">
            <div className="flex items-center justify-between gap-4 py-1">
              <div className="min-w-0">
                <div className="text-sm text-foreground">刷新间隔</div>
                <div className="text-[11px] text-muted">
                  间隔越短越及时，消耗的数据源配额越多
                </div>
              </div>
              <span className="text-[11px] font-mono text-foreground shrink-0 tabular-nums">
                {intradayIntervalDraft}s
              </span>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <input
                type="range"
                min={3}
                max={60}
                step={1}
                value={intradayIntervalDraft}
                onChange={(e) => setIntradayIntervalDraft(parseInt(e.target.value, 10))}
                className="flex-1 h-1 accent-accent cursor-pointer"
              />
              <span className="text-[11px] text-muted shrink-0">
                {intradayIntervalDraft !== intradayInterval ? '2秒后保存' : '3s — 60s'}
              </span>
            </div>
          </div>
        </Card>

      </div>

      {/* ========== 右列 ========== */}
      <div className="space-y-6">
        {/* 全量分钟: 盘中全市场分钟落盘, 按能力路由 (TickFlow Expert 或声明 full_minute 的插件/自定义源) */}
        <Card icon={Zap} title="全量分钟" anchor="minute-refresh">
          <ToggleRow
            label="全量分钟同步"
            desc={
              !hasFullMinuteCap ? '当前数据源不提供全量分钟能力'
              : rs?.repair_only ? `服务运行中 · ${rs?.provider ?? '自定义源'} 不支持增量 · 按 ≥60 秒批量同步`
              : rs?.running ? (rs?.in_trading_hours ? '服务运行中' : '运行中 · 非交易时段暂停')
              : '已关闭'
            }
            checked={prefs?.minute_refresh_enabled ?? false}
            onChange={(v) => save({ minute_refresh_enabled: v })}
            disabled={!hasFullMinuteCap}
          />
          <div className="mt-3 pt-3 border-t border-border">
            <div className="flex items-center justify-between gap-4 py-1">
              <div className="min-w-0">
                <div className="text-sm text-foreground">刷新间隔</div>
                <div className="text-[11px] text-muted">
                  交易时段内全市场分钟 K 的同步间隔 · 中断后自动补齐当日数据
                </div>
              </div>
              <span className="text-[11px] font-mono text-foreground shrink-0 tabular-nums">
                {minuteRefreshIntervalDraft >= 60 && minuteRefreshIntervalDraft % 60 === 0 ? `${minuteRefreshIntervalDraft / 60}m` : `${minuteRefreshIntervalDraft}s`}
              </span>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <input
                type="range"
                min={3}
                max={120}
                step={3}
                value={minuteRefreshIntervalDraft}
                disabled={!hasFullMinuteCap}
                onChange={(e) => setMinuteRefreshIntervalDraft(parseInt(e.target.value, 10))}
                className="flex-1 h-1 accent-accent cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              />
              <span className="text-[11px] text-muted shrink-0">
                {minuteRefreshIntervalDraft !== minuteRefreshInterval ? '2秒后保存' : '3s — 120s'}
              </span>
            </div>
            {rs?.available && rs.rounds != null && rs.rounds > 0 && (
              <div className="mt-2 text-[11px] text-muted">
                已 {rs.rounds} 轮 · 最近 {rs.last_symbols} 标的 / {rs.last_rows} 行 / {rs.last_requests} 请求
                {rs.last_round_ms != null ? ` · ${(rs.last_round_ms / 1000).toFixed(1)}s` : ''}
                {rs.last_error ? ` · ${rs.last_error}` : ''}
              </div>
            )}
          </div>
        </Card>
      </div>
    </div>
    </HighlightContext.Provider>
  )
}


// ===== ToggleRow =====

function ToggleRow({
  label,
  desc,
  checked,
  onChange,
  icon: Icon,
  disabled,
}: {
  label: string
  desc: string
  checked: boolean
  onChange: (v: boolean) => void
  icon?: React.ComponentType<{ className?: string }>
  disabled?: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0 flex items-start gap-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-secondary shrink-0 mt-0.5" />}
        <div className="min-w-0">
          <div className="text-sm text-foreground">{label}</div>
          <div className="text-[11px] leading-4 text-muted">{desc}</div>
        </div>
      </div>
      <button
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
        className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${
          checked ? 'bg-accent' : 'bg-elevated'
        } ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[18px]' : 'translate-x-[3px]'
          }`}
        />
      </button>
    </div>
  )
}


// ===== 通用卡片 =====

interface CardProps {
  icon: React.ComponentType<{ className?: string }>
  title: string
  badge?: string
  right?: React.ReactNode
  children: React.ReactNode
}

function Card({ icon: Icon, title, badge, right, children, anchor }: CardProps & { anchor?: string }) {
  const highlight = useContext(HighlightContext)
  const { ref, flash } = useCardFlash(anchor ? highlight : undefined, anchor ?? '')
  const inner = (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {badge && (
            <span className="px-1.5 py-0.5 text-[11px] font-mono rounded bg-elevated text-muted">
              {badge}
            </span>
          )}
        </div>
        {right}
      </div>
      {children}
    </section>
  )
  if (!anchor) return inner
  return (
    <div ref={ref} id={anchor} className={cardFlashCls(flash)}>
      {inner}
    </div>
  )
}
