// 后端 API 客户端 — 全项目统一入口
//
// Dev: Vite 按启动脚本解析出的 BACKEND_HOST/BACKEND_PORT 代理 /api
// Prod:同源(FastAPI 托管前端 dist)

import { toast } from '@/components/Toast'
import { cleanDeskNames } from './strategyDisplay'

const BASE = ''

type RequestOptions = RequestInit & {
  /** 为 true 时不弹错误 toast（由调用方自行汇总提示，如多图串行队列） */
  quiet?: boolean
  /** 请求超时毫秒数; null 关闭。默认 30s — 后端依赖 polars, 偶发挂起时无超时
   *  会占满浏览器同源连接池, 拖垮整页所有请求 (表现为全部排队"已停止")。 */
  timeoutMs?: number | null
}

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 把浏览器 fetch 抛出的裸网络错误文案翻译成可操作的提示。
 * 各浏览器文案不同: Chrome "Failed to fetch" / Safari "network error" / "Load failed"。
 * 典型根因: 后端等 AI 首包期间流式连接被代理/网关按空闲超时切断, 或 AI 服务繁忙。
 */
export function friendlyStreamError(message: string | undefined | null): string {
  if (!message) return ''
  if (/failed to fetch|network error|load failed|networkerror/i.test(message)) {
    return '网络连接中断: 通常是 AI 服务繁忙, 或代理/网关超时切断了长连接, 请重试'
  }
  return message
}

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
/** 同步计算型接口 (回测/筛选等) 的放宽超时: 合法耗时可能远超轮询类接口。 */
const COMPUTE_REQUEST_TIMEOUT_MS = 300_000
/** 扩展数据拉取类长请求: 跟随后端配置的单次超时 (timeoutSeconds, 默认 30s) + 10s 解析/写盘缓冲。
 *  浏览器端 fetch 默认 30s abort 会先于后端超时触发, 大响应接口 (如全量集合竞价
 *  /day, 后端超时 120s) 必须把这层同步放宽。 */
const extPullTimeoutMs = (timeoutSeconds?: number) => (timeoutSeconds ?? 30) * 1000 + 10_000

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { quiet, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchInit } = init ?? {}
  const isFormData = fetchInit.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  // 合并调用方传入的 headers (此前会被整体覆盖丢弃)
  Object.assign(headers, fetchInit.headers as Record<string, string> | undefined)
  // 自带 signal 的调用方 (上传/串行队列) 由其自行控制中止; 其余走默认超时。
  const ctl = timeoutMs == null || fetchInit.signal ? undefined : new AbortController()
  const timeoutSeconds = Math.round((timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS) / 1000)
  let timer: number | undefined
  if (ctl && timeoutMs != null) timer = window.setTimeout(() => ctl.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...fetchInit,
      headers,
      ...(ctl ? { signal: ctl.signal } : {}),
    })
  } catch (err) {
    if (ctl && err instanceof DOMException && err.name === 'AbortError') {
      const msg = `请求超时（${timeoutSeconds}s）· ${path.split('?')[0]}`
      if (!quiet) toast(msg, 'error')
      throw new ApiError(msg, 0)
    }
    throw err
  } finally {
    if (timer !== undefined) window.clearTimeout(timer)
  }
  if (!res.ok) {
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const raw = j.detail ?? j.message ?? ''
      if (Array.isArray(raw)) {
        // FastAPI 422 校验错误: [{type, loc, msg, input}, ...] → 取 msg 拼接
        detail = raw.map((e: any) => e?.msg || String(e)).join('; ')
      } else if (typeof raw === 'string') {
        detail = raw
      } else if (raw && typeof raw === 'object') {
        detail = JSON.stringify(raw)
      }
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    // 401 (未登录/会话过期) 不弹 toast — 由全局认证拦截器统一跳登录页, 避免刷屏
    if (res.status !== 401 && !quiet) toast(msg, 'error')
    throw new ApiError(msg, res.status)
  }
  return res.json() as Promise<T>
}

// ===== Capabilities =====
export interface CapabilityLimits {
  rpm: number | null
  batch: number | null
  subscribe: number | null
}

export interface CapabilitiesResponse {
  label: string
  capabilities: Record<string, CapabilityLimits>
}

// ===== 个股分析 =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3。前端按"显示到第几档"过滤。 */
  rank?: number
}

/** 带状曲线指标(布林带/Keltner/ATR)的每日时间序列,与 dates 对齐。 */
export interface LevelSeries {
  boll?: { upper: (number | null)[]; lower: (number | null)[]; mid?: (number | null)[] }
  keltner_s?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_m?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_l?: { upper: (number | null)[]; lower: (number | null)[] }
  atr?: { stop_loss: (number | null)[]; take_profit: (number | null)[] }
}

export interface StockLevels {
  levels: Record<LevelType, PriceLevel[]>
  close: number | null
  summary: string
  symbol: string
  /** dates 与 series 对齐;前端按自身 rows 的日期映射,缺失填 null */
  dates?: string[]
  series?: LevelSeries
}

export interface AiStockReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  summary?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

// ===== Kline =====
export interface MinuteKlineRow {
  datetime: string
  /** 分钟开盘价; 部分数据源(stock-sdk 历史日)无真实分钟 open, 为 null */
  open: number | null
  high: number
  low: number
  close: number
  /** 盘中实时拼接分钟的首根没有前一累计量, 成交量为 null (审计 2026-10-26 H-3) */
  volume: number | null
  amount: number | null
}

export interface MinuteKlineSession {
  date: string
  prev_close: number | null
  rows: MinuteKlineRow[]
}

export interface KlineRow {
  symbol?: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  change_pct?: number
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_14?: number | null
  vol_ratio_5d?: number | null
  [key: string]: any
}

export interface KlineDailyResponse {
  symbol: string
  name?: string
  stock_info?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
  rows: KlineRow[]
  source?: string
}

export interface KlineDailyLatestResponse {
  symbol: string
  row: KlineRow | null
  source: 'live' | 'none'
}

// ===== Watchlist =====
export interface WatchlistEntry {
  symbol: string
  added_at: string
  note?: string
  name?: string | null
  /** 所属分组 id 列表 (同一标的可属于多个分组; 空数组=未分组) */
  group_ids?: string[]
}

export type WatchlistGroupColor =
  | 'sky'
  | 'blue'
  | 'indigo'
  | 'violet'
  | 'fuchsia'
  | 'rose'
  | 'orange'
  | 'amber'
  | 'lime'
  | 'emerald'
  | 'teal'
  | 'cyan'

export interface WatchlistGroup {
  id: string
  name: string
  color: WatchlistGroupColor
}

export interface WatchlistImportCandidate {
  code: string
  symbol: string | null
  name: string | null
  matched: boolean
  already_in_watchlist: boolean
}

export interface WatchlistImportResult {
  provider: string
  codes: string[]
  candidates: WatchlistImportCandidate[]
  matched_count: number
  unmatched_count: number
}

export interface Quote {
  symbol: string
  price?: number
  pct?: number
  close?: number
  change_pct?: number
  [key: string]: any
}

export interface IndexQuote {
  symbol: string
  name?: string | null
  last_price?: number | null
  close?: number | null
  prev_close?: number | null
  change_pct?: number | null
  change_amount?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
  amount?: number | null
  timestamp?: number | null
  [key: string]: any
}

// ===== Screener =====
export interface ScreenerStrategy {
  id: string
  name: string
  description: string
  source?: string
  /** 支持的周期, 如 ['1d'] / ['1m'] (分钟策略) */
  timeframes?: string[]
}

export interface StrategyLoadError {
  file: string
  error: string
}

export interface ScreenerResult {
  as_of: string
  strategy: string | null
  rows: any[]
  total: number
  elapsed_ms: number
}

export interface ScreenerResultSummary {
  total: number
  as_of: string
  /** 渐进式 run_all 写入的计算时间戳 (Unix ms); 监控实时叠加等来源无此字段 */
  computed_at?: number | null
}

export interface ScreenerCachedSummary {
  as_of: string | null
  results: Record<string, ScreenerResultSummary>
  today_ever_counts: Record<string, number>
  updated_at: number | null
}

/** run_all 渐进式返回: 快策略已算完, 慢策略后台继续算 */
export interface ScreenerRunAllSummary {
  as_of: string | null
  results: Record<string, ScreenerResultSummary>
  /** 尚未算完的策略 (后台继续, 逐个写入缓存) */
  pending?: string[]
  /** 全部算完时为 true */
  complete?: boolean
  /** 后台执行出错时的错误信息 (部分结果仍会返回) */
  error?: string | null
  /** 本次执行起点 (Unix ms, 后端时钟), 用于判断缓存结果是否属于本轮 */
  started_at?: number | null
}

export interface ScreenerCachedResult {
  result: ScreenerResult | null
  today_ever_rows: Record<string, any> | null
  strategy_ids_by_symbol: Record<string, string[]>
  updated_at: number | null
}

export interface MarketSnapshotRow {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  amount?: number | null
  volume?: number | null
  turnover_rate?: number | null
  vol_ratio_5d?: number | null
  total_shares?: number | null
  float_shares?: number | null
  market_cap?: number | null
  float_market_cap?: number | null
  [key: string]: any
}

export interface OverviewDimensionRankItem {
  name: string
  count: number
  avg_pct: number
  up_count: number
  down_count: number
  amount: number
  /** 该维度组首个命中的扩展字段 "configId.field" (成分股弹窗直连; 无扩展源时缺失) */
  source_field?: string | null
  leader?: {
    symbol?: string | null
    name?: string | null
    change_pct?: number | null
  } | null
}

export interface OverviewMarket {
  as_of: string | null
  quote_status: {
    enabled?: boolean
    running?: boolean
    quote_age_ms?: number | null
    is_trading_hours?: boolean
    [key: string]: any
  }
  indices: IndexQuote[]
  breadth: {
    total: number
    up: number
    down: number
    flat: number
    up_pct: number
    down_pct: number
    avg_pct?: number | null
    median_pct?: number | null
    strong_up?: number
    strong_down?: number
  }
  amount: { total: number; avg: number }
  distribution: { label: string; count: number; pct: number }[]
  trend: { above_ma5: number; above_ma20: number; above_ma60: number; above_ma5_pct: number; above_ma20_pct: number; above_ma60_pct: number; new_high: number; new_low: number }
  activity: { avg_turnover: number; high_turnover: number; high_vol_ratio: number; vol_ratio: number }
  radar: { key: string; label: string; value: number }[]
  emotion: { score: number; label: string }
  top_gainers: MarketSnapshotRow[]
  top_losers: MarketSnapshotRow[]
  turnover_leaders: MarketSnapshotRow[]
  active_leaders: MarketSnapshotRow[]
  concept_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
  industry_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
}

// ===== Strategy Engine =====
export interface StrategyParamDef {
  id: string
  label: string
  type: 'float' | 'int' | 'select' | 'bool'
  default: number | string | boolean
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface CompositeChildInfo {
  id: string
  name: string
  source: string
  weight: number
}

export interface StrategyDetail {
  id: string
  name: string
  description: string
  tags: string[]
  source: 'builtin' | 'custom' | 'ai' | 'composite'
  research_only?: boolean
  execution_backend: 'polars_expr' | 'matrix_native' | 'python_history_legacy' | 'composite' | 'minute_filter'
  asset_types: string[]
  timeframes: string[]
  version: string
  basic_filter: Record<string, any>
  params: StrategyParamDef[]
  params_defaults: Record<string, any>
  scoring: Record<string, number>
  scoring_directions: Record<string, ScoringDirection>
  entry_signals: string[]
  exit_signals: string[]
  minute_exit_trigger_supported_signals: string[]
  stop_loss: number | null
  take_profit: number | null
  trailing_stop: number | null
  trailing_take_profit_activate: number | null
  trailing_take_profit_drawdown: number | null
  max_hold_days: number | null
  display_limit?: number
  order_by: string
  descending: boolean
  limit: number
  // 叠加策略(composite)专属: 子策略列表与合并模式。非 composite 时为 null。
  composite_children?: CompositeChildInfo[] | null
}

export type ScoringDirection = 'high' | 'low'

export interface StrategyBuildResult {
  code: string
  meta: Record<string, any>
  valid: boolean
  error: string | null
}

export type StrategyBuildStreamEvent =
  | { type: 'meta'; strategy_id?: string; step?: number }
  | { type: 'delta'; content: string }
  | ({ type: 'result' } & StrategyBuildResult)
  | { type: 'error'; message: string }
  | { type: 'ping' }

export interface StrategyCodeSaveResult {
  ok: boolean
  strategy_id: string
  source: 'ai' | 'custom' | 'composite'
  path: string
  meta: Record<string, any>
  research_only?: boolean
}

/** AI 迭代每一轮的回测证据 (stats 为比率, 0.15 = 15%) */
export interface AiIterateRound {
  round: number
  stats: Record<string, number> | null
  change_summary: string
}

export interface AiIterateResult {
  draft_strategy_id: string
  rounds: AiIterateRound[]
  final_code: string
  final_meta: Record<string, any>
}

// ===== Custom Signals (自定义信号) =====
export interface CustomSignalCondition {
  left: string     // 字段名
  op: string       // > >= < <= == !=
  right: string    // "field:xxx" 或数字字符串
  leftDays?: number   // 左字段取几日前 (0=当日, 默认)
  rightDays?: number  // 右字段取几日前 (仅 right 为字段时有意义)
}

export interface CustomSignal {
  id: string
  name: string
  kind: 'entry' | 'exit' | 'both'
  conditions: CustomSignalCondition[]
  enabled: boolean
}

export interface CustomSignalFieldGroup {
  key: string
  label: string
  fields: { key: string; label: string }[]
}

export interface CustomSignalOptions {
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays?: number
  operators: string[]
  /** string 扩展字段 (概念/行业归属): 这些字段用 stringOperators + 字符串右值 */
  stringFields?: string[]
  stringOperators?: string[]
  kinds: { key: string; label: string }[]
}

export interface CustomSignalAIGenerateResult {
  name: string
  conditions: CustomSignalCondition[]
}

// ===== Monitor (监控规则 + 触发记录) =====
export interface MonitorCondition {
  field: string
  op: string              // truth | > >= < <= == !=
  value?: number | null   // op 非 truth 时必填
}

export type StrategyNotifyEvent = 'buy_signal' | 'sell_signal' | 'pool_entry' | 'pool_exit'

// ===== 盘中异动 (enriched 当日信号聚合, 异动监控「盘中」tab) =====

export interface MonitorRule {
  id: string
  name: string
  enabled: boolean
  type: 'strategy' | 'signal' | 'price' | 'market' | 'volume_delta' | 'date'
  asset_type?: 'stock' | 'etf' | 'index'
  scope: 'symbols' | 'all' | 'watchlist_group'
  symbols: string[]
  /** scope=watchlist_group 时绑定的自选分组 id (成员动态解析, 增删自选自动生效) */
  group_id?: string | null
  threshold_pct?: number
  window_minutes?: 1 | 3 | 5 | 10 | 15
  strategy_id?: string | null
  direction: 'entry' | 'exit' | 'both'
  notify_events?: StrategyNotifyEvent[]
  score_min?: number | null
  score_max?: number | null
  conditions: MonitorCondition[]
  logic: 'and' | 'or'
  cooldown_seconds: number
  severity: 'info' | 'warn' | 'critical'
  message: string
  webhook_url?: string
  webhook_enabled?: boolean  // 兼容老规则, 已由 webhook_channels 取代
  webhook_channels?: string[]  // 合法值: feishu | wecom | custom | email
  created_at?: string
  runtime_warning?: string
  // volume_delta 阈值口径 (volume=股数, amount=金额 USD)
  metric?: 'volume' | 'amount'
  // volume_delta 专属 (轮询放量): 相邻两次全市场快照的成交量增量(股)
  threshold_volume?: number                 // 单轮增量 >= 此值(股)时报警
  threshold_amount?: number                 // metric=amount 时: 单轮增量 >= 此值(USD)时报警
  basic_filter?: VDBasicFilter             // 基础过滤 (与策略 basic_filter 语义对齐)
  // date 类型 (日期提醒): 纯日历窗口, 无行情 conditions
  remind_date?: string | null   // YYYY-MM-DD
  lead_days?: number            // 提前 N 天进入提醒窗口
  lot_id?: string               // 由「持仓提醒」页生成的规则, 托管在批次页 (监控中心只读)
}

// 批次登记 (薄批次, 页面名"持仓提醒") — 只作监控规则生成的载体, 不做任何会计
export interface Lot {
  id: string
  symbol: string
  qty: number
  cost_price: number
  buy_date?: string | null
  target_pct: number
  stop_pct: number
  remind_date?: string | null
  lead_days: number
  created_at?: string
}

/** 决策台来源 (GET /api/opportunities/desk/sources) */
export interface DeskBucket { n: number; mean: number | null; ci_low?: number | null }
export interface DeskSource {
  id: string
  name: string
  status: 'enabled' | 'observe' | 'disabled'
  tier?: 'proven' | 'explore' | 'negative'
  weight?: number | null
  risk_pct?: number | null
  reason?: string
  evidence?: { n?: number; mean?: number | null; ci_low?: number | null; ci_high?: number | null; win_rate?: number | null; method?: string; error?: string } | null
  by_bucket?: { top100?: DeskBucket; top1000?: DeskBucket; watchlist?: DeskBucket } | null
  manual?: { cap?: number; locked?: boolean }
  latest?: { opened: number; watched: number } | null
  /** 新闻催化来源 (固定只观察) 的影子证据 */
  news_shadow?: NewsShadowEvidence
}
export interface NewsShadowCompare {
  n_with: number
  n_without: number
  mean_with: number | null
  mean_without: number | null
  diff_ci: [number | null, number | null]
  months_compared: number
  months_consistent: boolean
  meets_bar: boolean
}
export interface NewsShadowEvidence {
  source: string
  name: string
  status: 'observe'
  snapshots: number
  resolved: number
  pending: number
  bearish_veto: NewsShadowCompare
  bullish_boost: NewsShadowCompare
  verdict: string
  rule: string
  since: string | null
}
export interface DeskSourcesPayload {
  sources: DeskSource[]
  evaluation: { generated_at?: string; window?: string[]; universe?: string; rules?: string; picks_per_day?: number }
  rules: Record<string, string>
  audit: { action: string; source: string; value: unknown; note?: string; at: string }[]
}

/** 机会页总览 (GET /api/opportunities/desk/overview, 只读) */
export interface DeskOverviewCapacity {
  planned_qty: number
  need_cash: number
  risk_pct: number
  downsized_qty?: number
  verdict: 'ok' | 'downsized' | 'cash' | 'max_positions' | 'portfolio_risk' | 'zero_size'
}
export interface DeskOverviewCard {
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
  evidence: Record<string, unknown> & { decision?: Record<string, unknown> }
  narrative: string
  state: string
  state_reason: string
  side?: 'long' | 'short'
  window: { elapsed: number; remaining: number; last_day: string | null; open: boolean }
  permission: { status?: string; tier?: string; weight?: number; risk_pct?: number; tradable?: boolean; reason?: string }
  blockers: string[]
  capacity: DeskOverviewCapacity | null
  in_watchlist: boolean
  held: boolean
  rr_suspect: boolean
  explain: string
}
export interface DeskOverviewAccount {
  nav: number
  cash: number
  buying_power: number
  positions: number
  max_positions: number
  open_risk: number
  open_risk_pct: number | null
  max_portfolio_risk_pct: number
  risk_per_trade_pct: number
  marks_origin: string[] | null
  unmarked: string[]
}
export interface DeskWatchHit { source: string; score: number | null; rank: number | null; total: number | null; via: string }
export interface DeskWatchRow {
  symbol: string
  held: boolean
  scan_day: string | null
  added_after_scan: boolean
  card: { id: string; state: string; state_label?: string; created_date: string; setup_type: string; entry_zone: [number, number];
          state_reason: string; tradable?: boolean; sources?: string[] } | null
  stage: 'pool' | 'no_signal' | 'card_build' | 'card' | 'pending' | 'unrecorded' | null
  reason: string | null
  hits: DeskWatchHit[]
  card_failures?: string[]
}
export interface DeskOverviewPayload {
  status: {
    today: string
    in_session: boolean
    latest_scan: string | null
    scan_generated_at: string | null
    gate: 'green' | 'yellow' | 'unavailable' | null
    tradable_sources: { id: string; name: string; weight: number | null; tier: string | null }[]
    evaluated_sources: number
    executable: number
    executable_clear: number
    circuit_breaker: Record<string, unknown> | null
    last_quote_at: string | null
    last_run_at: string | null
    entry_blockers: { symbol: string; reason: string }[]
    crowding: Record<string, string[]>
    account: DeskOverviewAccount | null
  }
  executable: DeskOverviewCard[]
  live_other_count: number
  watchlist: { ok: boolean; count: number; rows: DeskWatchRow[]; scan_day: string | null; removed_since_scan: string[] }
  positions?: DeskPositionAction[]
  events?: DeskEventRadar
  evidence?: DeskEvidenceBrief
  names: Record<string, string>
}

export interface WatchlistForward { n: number; months: number; excess: number | null; p: number | null; threshold: number | null; promoted: boolean; abs_mean?: number | null; tradable_rules?: string[] }
/** 规则面板回放 (tight-v1)。thin = 成交不足 5 笔, 只有 n (成交) / signals (过 4h 门的卡) / raw (门前命中), 没有 R (0.30.0) */
export interface WatchlistRuleHistory { n: number; mean_r?: number; excess?: number; p?: number; signals?: number; raw?: number; thin?: boolean }
export interface WatchlistRuleCatalogItem {
  id: string; name: string; kind: string; enabled: boolean; forward: { n: number; mean_r: number; excess: number } | null; history?: WatchlistRuleHistory | null
  /** 机会卡显示 (看, 2026-10-09): 看多出卡 / 能出看空 / 看空出卡 / 卖出当做空的回测 */
  show?: boolean; short_capable?: boolean; show_short?: boolean; history_short?: WatchlistRuleHistory | null
  /** 0.30.0: 做空列表用的名字 (与机会板空头卡同名, 如「吊灯翻空」); META name 是做多名 */
  name_short?: string | null
  /** 0.26.0: 系统发现的策略按机会板卡回放验收; passed = 赢过“4h 门内随便挑” (区间下限 > 0)。0.30.0: 验收失败带 error / detail */
  candidate?: {
    passed: boolean; excess?: number | null; excess_ci?: number[] | null; n?: number; mean_r?: number | null
    r_per_year?: number | null; base_r_per_year?: number | null; first_passed?: string | null
    error?: string; detail?: string
  } | null
}

/** 0.30.0 挖掘 / AI 策略卡回放验收的状态 (规则齿轮里「重新回放」按钮旁) */
export interface SwingCandidatesStatus { generated_at: string | null; window: string[] | null; running: boolean; pending_at: string | null }
/** 第一性原理机会板 (GET /api/opportunities/swing) */
export type SwingBoardCard = {
  contract: string
  symbol: string
  side: 'long' | 'short'
  score: number
  hit_count: number
  strategies: { id: string; name: string }[]
  /** 4h 门: event = 窗口内条件齐备; state = 4h 方向同向 */
  gate?: 'event' | 'state'
  close?: number | null
  /** tight-v1: 限价回踩入场区 [低, 高] */
  entry_zone: number[]
  stop: number
  target: number
  target_zone?: number[] | null
  risk: number
  rr: number
  atr?: number
  stop_pct?: number
  target_pct?: number
  entry_days: number
  hold_days: number
  as_of: string
  signal_day?: string
  card_id?: string
  /** 信号日后已开第几个交易日 (0 = 信号日当晚, 未开盘) */
  entry_day_n?: number
  state?: 'pending' | 'in_zone' | 'holding'
  price?: number | null
  price_source?: 'live' | 'snapshot' | 'premarket' | 'close'
  /** 0.26.0: price_source = close 时这个收盘价是哪一天的 (休市时 = 最近一个已收盘交易日) */
  price_asof?: string | null
  dist_pct?: number | null
  entry_price?: number | null
  qty?: number | null
  entry_date?: string | null
  r_now?: number | null
  /** 0.33.0: 持仓票已移出自选 -> 收盘后记离场, 下一交易日开盘平仓 */
  watch_removed?: boolean
  /** 今天不会开的原因 (卡仍显示): 同票持仓 / 平仓冷却 / 财报前一日 / 跨日多空冲突 / 交易环准入阻断 */
  blocked?: 'holding_other' | 'cooldown' | 'earnings' | 'direction_conflict' | 'paper_watch' | null
  paper_reason?: string | null
  /** 现价已越过止损 (0.27.0 起只有做多第 1 天 09:00 盘前越止损会作废; 其余只是一个状态, 已体现在 odds 里) */
  beyond_stop?: boolean
  /** 0.27.0 系统裁决: order 下单 / skip 不下单 (verdict_reason = 原因码) / holding 已成交 */
  verdict?: 'order' | 'skip' | 'holding'
  verdict_reason?: string | null
  /** 0.27.0 卡面两个数字: 从当前状态起, 回放里同类卡回到挂单区的比例 + 继续挂单的平均盈亏 (没成交记 0) */
  odds?: SwingOdds | null
  /** 0.27.0 盘前量比 (04:00 起累计 / 前 10 日同时段均量, SIP 延迟 16 分钟; through = 截至 HH:MM ET) */
  vol_pre?: { ratio: number; level: string; label: string; through?: string } | null
  /** 0.31.0 盘前定稿 (09:20 ET 起; 09:00 口径): 缺口按 ATR 顺向为正、盘前量比、隔夜新闻条数、是否排前 */
  pm?: { gap_atr: number | null; pvr: number | null; news_n: number; priority: boolean } | null
  pm_price?: number | null
  /** 历史 (灰): expired 入场窗结束 / closed 已平仓 / void 落簿时排除 / paper_expired 交易环作废 */
  void_reason?: string | null
  voided_at?: string | null
  r_final?: number | null
  exit_reason?: string | null
  exit_date?: string | null
  exit_price?: number | null
}
/** 0.27.0 状态赔率 (config/swing_state_odds.json 查表): sig = 期望 95% 区间整体在 0 上 (+1) / 0 下 (-1) / 跨 0 (0) */
export type SwingOdds = {
  fill: number; ev_r: number; ci_r: number[] | null; sig: -1 | 0 | 1; n: number; level: string
  day: number; phase: 'pre' | 'open' | 'am' | 'pm'; bin: string; dist_atr: number | null
  qty: number | null; risk_usd: number | null; ev_usd: number | null; ci_usd: number[] | null
}
export type SwingHistoryCard = Omit<SwingBoardCard, 'state'> & { state: 'expired' | 'closed' | 'void' | 'paper_expired' }
/** 0.26.0 卡面那一行: 同类卡的回放基础率 (比例 0~1) */
export type SwingBaseLine = { fill: number; win: number; target: number; stop: number; time: number; contract: string; rules?: string[]; stale?: boolean }
/** 0.26.0 机会板卡结局 (GET /api/opportunities/swing-settlement) */
export type SwingSettleProp = { p: number | null; ci: number[]; k: number; n: number }
export type SwingSettleStats = {
  cards: number; filled: number
  fill: SwingSettleProp; win: SwingSettleProp; target: SwingSettleProp; stop: SwingSettleProp; time: SwingSettleProp
  mean_r: { v: number | null; ci: number[] | null; n: number }
}
export type SwingSettleSide = { actual: SwingSettleStats; replay: SwingSettleStats | null; mismatch: string[]; void?: number }
export type SwingSettleRow = {
  card_id: string; symbol: string; side: 'long' | 'short'; signal_day: string; zone: number[]; stop: number; target: number
  status: 'no_fill' | 'closed' | 'void'; why?: 'target' | 'stop' | 'time' | 'premarket_beyond_stop'; r?: number; fill_date?: string; exit_date?: string
}
export type SwingSettlePayload = {
  contract: string; card_contract: string; alpha: number; base_window: string[] | null
  sides: { long: SwingSettleSide; short: SwingSettleSide }
  recent: SwingSettleRow[]; n_total: number
  reconcile: SwingReconcileRow[]
}
/** 0.26.0 每日对账一行: 信号层 (生产 vs 回放逐票) + 板层 (已落盘的簿 vs 重算板) + 超过历史最长的连续 0 命中 */
export type SwingReconcileRow = {
  day: string; ok: boolean; board_day?: string | null
  issues: { kind: string; rule?: string; symbol?: string; side?: string; day?: string }[]
  signal_check?: Record<string, { prod: number; replay: number }> | 'unavailable'
  zero_hit_rules?: Record<string, { days: number; history_max: number }>
}
export type SwingRefreshInfo = { count: number; last_at: string | null; changes: { at: string; card_id: string; symbol: string; side: string; from: string | null; to: string | null; blocked?: string | null; reason?: string | null }[] }
/** 0.29.0 机会页一次取齐 (GET /api/opportunities/page): 某块失败 = 该块 null + errors[块名] */
export type OppCardCtx = {
  news?: { count_24h?: number | null; tone?: NewsTone | null }
  earnings?: { date: string; timing?: string | null }
  premarket_pct?: number
  pcr?: number
  iv?: number
}
/** 0.35.0 开盘放量榜推送: 开关 + 门槛档位; 渠道跟随全局推送设置 (levels = 各档回放频率, 后端单一来源) */
export type RadarPushLevel = { v: number; days: number; per_day: number; up5: number }
export type RadarPushSettings = { enabled: boolean; min_rvol: number; period?: string; other?: number; levels?: RadarPushLevel[] }
export type OppSettings = { long: string[]; short: string[]; risk_per_trade_pct: number | null; ruleset_version: number | null; evo: boolean; radar_push?: RadarPushSettings
  /** 机会板方向门 (可插拔): 内置日线 MA50 门或插件门 */
  gate?: { id: string; name: string } }
/** 0.34.0 按时段换主角: 盘前 = 跳空榜 / 开盘 = 开盘放量榜 / 盘中 = 盘中强势 + 放量前 5 / 挂单 = 机会板 */
export type OppStageKey = 'pre' | 'open' | 'session' | 'board'
export type OpenRadarRow = {
  symbol: string; rvol: number; dir: 1 | 0 | -1; open: number; or_high: number; or_low: number; or_close: number
  prev_close: number; gap_pct: number; or_chg_pct: number; atr: number; pick: boolean; rank?: number; backfill?: boolean
  price?: number; price_source?: string; chg_now_pct?: number; close?: number; chg_close_pct?: number; high_pct?: number
}
export type OpenRadarPayload = {
  contract: string; today: string; ready_at: string; pending: boolean
  replay: { period: string; hit: number; other: number; ctl_hit: number; ctl_other: number; after_0935_mean_pct: number }
  stats: { n: number; hit: number } | null
  day: string | null; is_today: boolean; at: string | null; picks: OpenRadarRow[]; others: OpenRadarRow[]; eligible: number
  backfill?: boolean; settled?: boolean; errors?: number
  push?: { enabled: boolean; min_rvol: number; today: { symbols: string[]; at: string | null } | null }
}
export type GapBoardPayload = {
  at: string; live: boolean; threshold_pct: number; checked: number; warming?: boolean
  rows: { symbol: string; gap_pct: number; price?: number | null; role?: '持仓' | '挂单' }[]
}
export type IntradayHeatRow = { symbol: string; rvol: number | null; chg: number | null; price: number | null; vwap: number | null }
export type IntradayNearRow = { symbol: string; why: 'below_vwap' | 'no_live' | 'geometry'; chg: number | null; rvol: number | null; er: number | null; vwap: number | null; live: number | null }
/** 0.39.0 机会页雷达 (radar_view, 契约 opp-radar-v1): 一只自选票一个主状态, 环 = 离动作多近 */
export type RadarKind = 'exit' | 'hold' | 'order' | 'blocked' | 'watch' | 'quiet'
export type RadarNote = { src: 'intraday' | 'open_pick' | 'gap' | 'open_vol' | 'heat' | 'near'; rvol?: number | null; chg?: number | null; pct?: number | null
  rank?: number | null; r_now?: number | null; day_n?: number | null; hold_days?: number | null; why?: string | null; check?: string | null }
export type RadarEarnings = { date: string; timing?: string | null; reduce_day: string }
export type RadarOrder = {
  card_id?: string | null; zone: (number | null)[]; stop: number | null; target: number | null; qty: number | null; risk_usd: number | null
  price: number | null; price_source?: string | null; price_asof?: string | null; dist_pct: number | null; atr: number | null
  signal_day?: string | null; day_n?: number | null; entry_days?: number | null; valid_until: string | null; rules: string[]; gate?: string | null
  beyond_stop?: boolean; odds: { fill?: number | null; ev_r?: number | null; ev_usd?: number | null; ci_r?: number[] | null; ci_usd?: number[] | null; sig?: number | null; n?: number | null } | null
  sector?: string | null; earnings: RadarEarnings | null
}
export type RadarHold = {
  qty: number; avg: number; price: number | null; price_source?: string | null; price_asof?: string | null
  stop_now: number | null; stop_initial: number | null; stop_moved: boolean; target: number | null; entry_date: string; day_n: number; max_days: number
  exit_day: string | null; exit_at: string | null; exit_code: 'time_exit' | 'exit_next_open' | 'exit_watch_removed'; exit_reason?: string | null
  /** 次开盘平仓的执行日 (exit_next_open / exit_watch_removed) */
  exit_open_day?: string | null
  stop_dist_atr: number | null; r_now: number | null; pnl_usd: number | null; card_id?: string | null; earnings: RadarEarnings | null
}
export type RadarBlip = {
  symbol: string; side: 'long' | 'short' | null; kind: RadarKind; ring: 0 | 1 | 2 | 3 | 4; r: number; d: number | null
  /** 现价相对挂单区 (与交易环成交条件同式): in=区内 · wait=未到区 · past=已越过挂单区 (不会成交); zone_gap=区外距离 ATR */
  zone_pos?: 'in' | 'wait' | 'past' | null; zone_gap?: number | null
  bearing: number | null; group: string | null; sector?: string | null; codes?: string[]; mirror?: boolean; slot?: string
  order?: RadarOrder; hold?: RadarHold; also?: RadarNote[]; changed_at?: string | null; dir?: 'in' | 'out' | null
  off_scope?: boolean
}
export type RadarSlot = { key: string; sector: string; side: 'long' | 'short'; used: number; cap: number; free: number; holders: string[]; waiting: string[]
  near: string[]; state: 'full' | 'caution' | 'tight' | 'ok'; release_by: string[]; release_at: string | null }
export type RadarVerdict = {
  kind: 'idle' | 'go' | 'warn'; code: 'closed' | 'cutoff' | 'cap_full' | 'cap_caution' | 'orders' | 'pre_window' | 'no_orders'
  orders: number; blocked: string[]; holds: number; exits: string[]; exit_at: string | null; nearest: { symbol: string; d: number; zone_pos?: 'in' | 'wait' | 'past' | null; zone_gap?: number | null } | null
  next_day?: string; max_fills?: number; slot?: Pick<RadarSlot, 'key' | 'sector' | 'side' | 'used' | 'cap' | 'free' | 'holders' | 'waiting' | 'near' | 'state' | 'release_by' | 'release_at'> | null
  outside?: string[]; exits_next?: string[]; in_zone?: string[]
}
export type OppRadarView = {
  contract: string; at: string
  clock: { et: string; trading_day: boolean; phase: string; entry_window: boolean; entry_close: string | null; close: string | null; board_first: string
    session_day: string; next: { day: string; at: string; code: string; ts: string } }
  verdict: RadarVerdict
  caps: { slots: RadarSlot[]; sector_cap: number; risk_pct: number | null; opens: { used: number; cap: number; day: string }
    positions: { used: number; cap: number | null }; portfolio_risk: { used: number | null; cap: number | null; gate: string | null } }
  lanes: {
    board: { ticks: string[]; window: string[]; fills: { at: string | null; symbol: string; side: 'long' | 'short' }[]; full: (string | null)[][]; orders: number; refresh_last: string | null }
    gap: { at: string | null; live: boolean; warming: boolean; n: number; threshold: number | null }
    open: { at: string | null; ready_at: string; picks: string[]; eligible: number | null; push?: { enabled: boolean; min_rvol: number; today: { symbols: string[]; at: string | null } | null } | null }
    intraday: { ticks: string[]; schedule: string[]; fired: number; active: number; next: string | null }
  }
  blips: RadarBlip[]
  price_sources: Record<string, number>; stale_prices: boolean; frames: number; last_frame_at: string | null; missing: string[]
}
/** 0.41.0 一页作战台 (desk 段, 契约 opp-desk-v1): 大盘状态 + 跨引擎共振 + 共振影子账本 */
export type DeskRegimeKey = 'up' | 'normal' | 'down'
export type DeskRegime = { key: DeskRegimeKey | null; spy: number | null; qqq: number | null; n: number; up: number; down: number; up3: number
  chg: Record<string, number>; at: string }
export type DeskEngine = 'gap' | 'open' | 'intraday' | 'board'
export type DeskResonance = {
  symbol: string; side: 'long' | 'short'; n: number; chg: number | null; price: number | null; shadow_at: string | null
  engines: { gap?: { pct: number }; open?: { rank?: number | null; rvol?: number | null }; intraday?: { cards: { day: string; r: number | null }[] }
    board?: { state: string } }
}
export type OppDesk = { contract: string; regime: DeskRegime; resonance: DeskResonance[]
  shadow: { rule: string; n: number; settled: number; mean5: number | null; win5: number | null; since: string | null } }
export type OppPagePayload = {
  contract: string
  generated_at: string
  phase: { phase: 'closed' | 'night' | 'premarket' | 'open_wait' | 'session' | 'after'; label: string; at: string } | null
  stage?: { key: OppStageKey; label: string } | null
  swing: SwingBoardsPayload | null
  intraday: IntradayPayload | null
  radar?: OpenRadarPayload | null
  gaps?: GapBoardPayload | null
  mood: MarketMoodPayload | null
  ctx: Record<string, OppCardCtx>
  settings: OppSettings | null
  /** 0.39.0 雷达投影; 机会板读取失败时为 null */
  radar_view?: OppRadarView | null
  /** 0.41.0 大盘状态 + 共振 */
  desk?: OppDesk | null
  errors: Record<string, string>
}
export type SwingBoardsPayload = {
  contract: string
  /** 0.31.0 顶栏「盘前定稿 09:20」, spy_chg = 盘前 SPY 相对昨收 (比例) */
  premarket_final?: { at: string; spy_chg: number | null }
  available: boolean
  as_of: string | null
  long_rules: string[]
  short_rules: string[]
  long: SwingBoardCard[]
  short: SwingBoardCard[]
  risk_pct?: number | null
  equity?: number | null
  meta?: Record<string, unknown>
  generated_at?: string
  history?: SwingHistoryCard[]
  refresh?: SwingRefreshInfo
  base_rates?: { long: SwingBaseLine | null; short: SwingBaseLine | null }
  today?: string
  entry_day?: string
  now?: string
}

/** 0.28.0 盘中强势卡 (GET /api/opportunities/intraday): 只记录不下单, 波段 2R / 5 天出场。 */
export type IntradayOdds = {
  n: number; win: number; mean_r: number; ci_r: [number, number]; train_mean_r: number; period: string; sig: number
  qty?: number; risk_usd?: number; ev_usd?: number; ci_usd?: [number, number]
}
export type IntradayCard = {
  contract: string; card_id: string; symbol: string; side: 'long'; day: string; check: string; fired_at: string; slot: number
  entry_ref: number; entry: number; stop: number; target: number; risk: number; stop_pct: number; target_pct: number
  hold_days: number; status: 'open' | 'closed' | 'void'
  features?: { chg?: number | null; vwap?: number | null; rvol?: number | null; er?: number | null; above?: number | null; pull?: number | null }
  price?: number; price_source?: string; r_now?: number | null; day_n?: number
  exit?: number | null; exit_reason?: 'stop' | 'target' | 'time' | 'no_data' | 'corp_action_suspect'; exit_at?: string | null; r?: number | null
  backfill?: boolean
  odds?: IntradayOdds | null
}
export type IntradayPayload = {
  contract: string; today: string; now: string
  active: IntradayCard[]; history: IntradayCard[]
  stats: { n: number; win: number; mean_r: number; sum_r: number } | null
  replay: IntradayOdds
  checks_today: { at: string; check: string; symbols: number; candidates: number; fired: string[]; heat?: IntradayHeatRow[]; near?: IntradayNearRow[]; late?: boolean }[]
  next_check: string | null
  rule: { min_chg: number; min_rvol: number; min_er: number; min_above: number; max_pull: number; hold_days: number; target_r: number }
}

export type MoodSide = 'up' | 'down' | 'range'
export interface MarketMoodItem {
  key: 'trend' | 'overnight' | 'calendar' | 'rates' | 'sectors' | 'panic'
  label: string
  text: string
  status: 'ok' | 'stale' | 'missing'
  as_of?: string | null
  lean?: MoodSide | null
  note?: string
}
export interface MarketMoodJudgment {
  day: string
  generated_at: string
  mode: 'llm' | 'fallback'
  probs: Record<MoodSide, number>
  main: MoodSide
  stance: MoodSide
  stars: number
  panic: 'none' | 'tighten_long' | 'unknown'
  summary?: string
  items: MarketMoodItem[]
}
export interface MarketMoodScore { day: string; chg_pct: number; outcome: MoodSide; main: MoodSide; hit: boolean }
export interface MarketMoodPayload {
  now: string
  expected_day: string
  next_at: string | null
  judgment: MarketMoodJudgment | null
  is_today: boolean
  is_latest: boolean
  stale_items: string[]
  missing_items: string[]
  score?: MarketMoodScore | null
  last_score: MarketMoodScore | null
  stats: { n: number; hit: number; hit_rate: number | null }
}

/** 方向 A 卡结局表 (GET /api/opportunities/settlement, 只读) */
export interface SettlementCell {
  rule?: string; side?: 'long' | 'short'; family?: string | null
  n: number; settled: number; exp: number; pass: number; unsettled: number; missing: number; same_bar: number
  trigger_rate: number | null; win_rate: number | null; mean_r: number | null; mean_excess_r: number | null
  ci: [number, number] | null; worst_r: number | null; tone: 'gray' | 'plain' | 'pos' | 'neg'
}
export interface SettlementTable { kind: 'fill' | 'signal'; scope: 'live' | 'all'; m: number; cells: SettlementCell[]; all: SettlementCell }
export interface SettlementPayload {
  available: boolean
  contract: string
  data_asof?: string
  generated_at?: string
  counts?: Record<string, number>
  tables: { fill: { live: SettlementTable; all: SettlementTable }; signal: { live: SettlementTable; all: SettlementTable } } | null
  thresholds?: Record<string, number>
}
export interface WatchlistRulesPayload {
  ruleset: { version: number; enabled: string[]; short?: string[]; evo?: boolean; params: { risk_per_trade_pct?: number }; created_at?: string | null }
  /** 0.15.0 自进化卡面板: 开关 + 最近一次盘后的规则权重 / 训练窗 / 大盘门槛 / 当天出卡 */
  evo?: WatchlistEvoPanel
  catalog: WatchlistRuleCatalogItem[]
  /** 空头规则 (生来做空, 只观察, 不进规则集; 可勾选是否出看空卡) */
  short_rules?: { id: string; name: string; history?: WatchlistRuleHistory | null; show_short?: boolean }[]
  /** 机会卡显示勾选 (只影响展示) */
  display?: { long: string[]; short: string[]; saved: boolean }
  versions: number[]
  forward: WatchlistForward
  rule: { start: string; min_n: number; min_months: number }
  /** 0.11.0 单一口径: 可下单规则 (做空 = 规则集 short + SPY<50日线 门槛) */
  trading?: { long: string[]; short: string[]; risk_pct: number }
  candidates?: SwingCandidatesStatus
  /** 0.30.1 内置策略回放文件的元数据 (窗口 / 名单 / 数据截至 / 生成时刻); 齿轮的来源行读它 */
  history_meta?: { window: string[] | null; universe: string | null; data_end: string | null; generated: string | null } | null
}

export interface WatchlistEvoPanel {
  on: boolean
  as_of: string | null
  hold_days: number
  train?: { days: number; from: string; to: string; long_n: number; short_n: number } | null
  bear?: boolean | null
  index_close?: number | null
  index_ma50?: number | null
  index_ma20?: number | null
  short_gate?: string | null
  long_picks?: number | null
  long: { rule: string; w: number; name?: string }[]
  short: { rule: string; w: number; name?: string }[]
  cards: { symbol: string; side: 'long' | 'short'; kind: 'long' | 'short' | 'floor'; score: number }[]
}

/** 持仓 + 下一步动作 (overview.positions) */
export interface DeskPositionAction {
  symbol: string
  side: 'long' | 'short'
  qty: number
  avg_cost: number
  last: number | null
  mark_source: string | null
  r_now: number | null
  stop: number | null
  stop_dist_pct: number | null
  target: number[] | null
  target_dist_pct: number | null
  held_days: number | null
  max_days: number | null
  source: string | null
  source_name: string | null
  action: string
  urgency: 'high' | 'mid' | 'low'
}
export interface DeskEventRadar {
  premarket: { date?: string; generated_at?: string; alerts: { symbol: string; roles?: string[]; gap_pct?: number; direction?: string; hl_price?: number; prev_close?: number }[] } | null
  earnings: { symbol: string; date: string; timing?: string; roles: string[] }[]
  earnings_source: string | null
}
export interface DeskForwardProgress { source: string; n: number; months: number; p: number | null; threshold: number; promoted: boolean; progress_text: string }
export interface DeskEvidenceBrief { mode: 'v1' | 'v2' | 'v3' | 'v4'; v3_generated_at: string | null; forward: DeskForwardProgress[]; forward_min_n?: number }

/** 证据 v3.1 来源体检 (evidence.v3) */
export interface V3Stat { n: number; mean: number | null; ci_low: number | null; ci_high: number | null; p: number | null; days?: number }
export interface V3SourceRow {
  source: string
  name: string | null
  kind: 'strategy' | 'event' | null
  n: number
  mean: number | null
  median: number | null
  baseline: number | null
  excess: V3Stat
  discovery: V3Stat
  holdout: V3Stat
  by_year: Record<string, { n: number; mean_r: number; mean_excess: number }>
  concentration: Record<string, number>
  delisted_trades: number
  holm_pass: boolean
  tier: 'proven' | 'explore' | 'negative'
  reason: string
  forward: { candidate: boolean; n: number; months: number; excess: number | null; p: number | null; threshold: number; promoted: boolean; progress_text: string } | null
  shadow: { snapshots: number; closed: number; pending: number; mean_r: number | null; excess: number | null; since: string } | null
  real: { opened: number; closed: number; mean_r: number | null } | null
}
export interface V3Payload {
  contract: string
  window: string[]
  generated_at: string
  holdout_start: string
  cluster_unit: string
  calibration: Record<string, unknown>
  with_delisted?: boolean
  hypotheses_tested?: number
  pool?: string
  baseline?: string
  sources: V3SourceRow[]
  forward_rule: { start: string; min_n: number; min_months: number; candidates: string[] }
  baseline_shadow: { snapshots: number; closed: number; mean_r: number | null } | null
}

/** 新旧证据并排 (GET /api/opportunities/desk/evidence) */
export interface EvidenceStat { n?: number | null; mean?: number | null; ci_low?: number | null; ci_high?: number | null; method?: string; censored?: number }
export interface EvidenceCompareRow {
  v1: EvidenceStat | null
  v2: EvidenceStat | null
  tier_v1: string | null
  tier_v2: string | null
  weight_v1: number | null
  weight_v2: number | null
  changes_permission: boolean
  version: 'match' | 'changed' | 'unknown' | 'unverified'
}
export interface EvidencePayload {
  mode: 'v1' | 'v2' | 'v3' | 'v4'
  v1: { generated_at?: string; window?: string[]; rules?: string } | null
  v2: { generated_at?: string; window?: string[]; rules?: string; contract?: string; elapsed_s?: number } | null
  comparison: Record<string, EvidenceCompareRow>
  v2_job: { running: boolean; last: Record<string, unknown> | null }
  v3_job?: { running: boolean; stage?: string | null; last: Record<string, unknown> | null }
  v3?: V3Payload | null
  permission_changes: string[]
}

/** 系统状态 (GET /api/trading/system-status) */
export interface SystemStatusComponent {
  id: string; label: string; state: 'ok' | 'warn' | 'error' | 'idle'; summary: string; source?: string
  /** 0.7.1 只读聚合字段 (旧后端没有) */
  since?: string | null; impact?: string; action?: string; idle_kind?: 'waiting' | 'closed' | 'disabled'; stale?: boolean
}
export interface SystemStatusJob {
  id: string; next_run: string | null
  state?: 'ok' | 'pending' | 'running' | 'missing' | 'error' | null; due?: string; last_at?: string | null
}
export interface SystemStatusMarket {
  phase: 'premarket' | 'session' | 'postmarket' | 'closed'; trading_day: boolean; next_open: string; close: string | null
}
export interface SystemStatusPayload {
  at: string
  components: SystemStatusComponent[]
  jobs: SystemStatusJob[]
  market?: SystemStatusMarket
}

/** 准入漏斗 (GET /api/trading/funnel) */
export interface EntryFunnelDay {
  day: string
  active: number
  touched: number
  blocked: number
  filled: number
  backfill: number
  reasons: { reason: string; label: string; n: number }[]
  global: { reason: string; label: string; since: string }[]
  recorded: boolean
}
export interface EntryFunnelPayload { days: EntryFunnelDay[]; as_of: string }

/** Alpaca 纸面镜像 (GET /api/trading/alpaca-mirror) */
export interface AlpacaMirrorOrder {
  cid: string; seq: number; symbol: string; side: 'buy' | 'sell'; status: string
  ledger_qty: number; ledger_price: number; ledger_kind: string; ledger_at?: string
  qty?: number; filled_qty?: number; filled_price?: number; deviation_bps?: number; error?: string
}
export interface AlpacaMirrorPayload {
  summary: { enabled: boolean; started_seq: number | null; orders: number; filled: number; by_status: Record<string, number>
    avg_deviation_bps: number | null; worst_deviation_bps: number | null; positions: Record<string, number>
    last_error: string | null; max_orders_per_day: number; rules: string }
  orders: AlpacaMirrorOrder[]
}

/** 持仓页实时视图 (GET /api/portfolio/live, 5 秒刷新) */
export interface StopMove {
  seq?: number | null
  at?: string | null
  trade_date?: string | null
  from?: number | null
  stop_price?: number | null
  reason?: string | null
}

/** 0.18.0 已结算卡逐张明细 (GET /api/opportunities/settlement/cards, 只读 jsonl) */
export interface SettlementCardRow {
  key: string
  card_id: string
  record_kind?: string
  outcome?: string
  rule?: string | null
  symbol?: string
  side?: string
  signal_day?: string
  fill_date?: string | null
  exit_date?: string | null
  exit_reason?: string | null
  hold_days?: number | null
  r?: number | null
  excess_r?: number | null
  provenance?: string
  /** pass_signal 行的信号口径结算字段 */
  signal_r?: number | null
  signal_excess_r?: number | null
  signal_exit_date?: string | null
  signal_exit_reason?: string | null
}
export interface SettlementCardsPayload {
  available: boolean
  contract: string
  total: number
  rows: SettlementCardRow[]
  rules: string[]
  exit_reasons: string[]
}


export interface LivePosition {
  symbol: string
  side?: 'long' | 'short'
  qty: number
  setup_type?: string | null
  /** 2026-10-11: 展示名 = 机会卡同款中文名 +（多/空）; null = 映射不到, 回退显示 setup_type */
  setup_label?: string | null
  card_id?: string | null
  entry_date?: string | null
  entry_price: number
  avg_cost: number
  last_price: number
  price_source: string
  price_age_s?: number | null
  market_value: number
  unrealized_pnl: number
  unrealized_pct?: number | null
  r_now?: number | null
  stop_price?: number | null
  /** 0.18.0: 首次成交时的止损 / 目标, 与本回合 stop_move 记录 (台账推算) */
  stop_initial?: number | null
  target_initial?: number[] | null
  stop_moves?: StopMove[]
  stop_distance_pct?: number | null
  target_zone?: number[] | null
  target_distance_pct?: number | null
  days_held?: number | null
  max_holding_days?: number | null
  partial_taken?: boolean
  /** 2026-10-10: contract = 合约式核算 (只结算盈亏, 多空按名义×保证金率占用); stock = 股票式 */
  accounting?: 'contract' | 'stock'
  notional?: number | null
  risk_pct?: number | null
  entry_tier?: string | null
}

export interface ClosedTrip {
  symbol: string
  side?: 'long' | 'short'
  setup_type?: string | null
  setup_label?: string | null
  entry_date?: string | null
  entry_price: number
  exit_date?: string | null
  exit_price?: number | null
  qty: number
  partial_exits: number
  exit_reason: string
  exit_reason_label: string
  days_held?: number | null
  fees: number
  pnl: number
  return_pct?: number | null
  r_multiple?: number | null
}

export interface PortfolioLive {
  positions: LivePosition[]
  closed: ClosedTrip[]
  summary: {
    nav: number; cash: number; market_value: number; initial_equity: number; total_return_pct?: number | null
    realized_pnl: number; unrealized_pnl: number; closed_trades: number; win_rate?: number | null; avg_r?: number | null
    /** 0.26.0: 已平仓按口径分开 (机会板卡 / 旧规则卡) */
    by_scope?: { key: 'swing' | 'legacy'; label: string; closed_trades: number; win_rate: number; avg_r: number | null }[]
    open_risk: number; refresh_seconds: number; as_of: string
    margin_used?: number | null; buying_power?: number | null
  } | null
  note?: string
}

export interface VDBasicFilter {
  price_min?: number | null                 // 股价下限 (元)
  price_max?: number | null                 // 股价上限 (元)
  market_cap_min?: number | null            // 总市值下限 (元)
  float_cap_min?: number | null             // 流通市值下限 (元)
  float_cap_max?: number | null             // 流通市值上限 (元)
  amount_min?: number | null                // 当日成交额下限 (元)
  exclude_st?: boolean                      // 剔除 ST
}

export interface MonitorRuleOptions {
  threshold_fields: { key: string; label: string }[]
  builtin_signals: { key: string; label: string }[]
  custom_signals: { key: string; label: string }[]
  operators: string[]
  types: { key: string; label: string }[]
  scopes: { key: string; label: string }[]
  logics: { key: string; label: string }[]
  severities: { key: string; label: string }[]
  directions: { key: string; label: string }[]
  intraday_signal_support: {
    available: boolean
    source: string | null
    max_symbols: number
    reason: string
  }
}

export interface AlertEvent {
  ts: number
  rule_id?: string
  rule_name?: string
  source: string
  type: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  severity?: string
  strategy_id?: string
  conditions?: MonitorCondition[]
  logic?: 'and' | 'or'
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

/** 生成监控规则 id (时间戳 + 随机后缀), 用户无需手动填写。 */
export function genRuleId(): string {
  const ts = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 6)
  return `mr_${ts}_${rand}`
}

// ===== Backtest =====
export interface BacktestResult {
  run_id: string
  config: any
  stats: Record<string, any>
  equity_curve: { date: string; value: number }[]
  trades: any[]
  per_symbol_stats: { symbol: string; total_return: number }[]
}

// ===== Factor Backtest =====
export interface FactorColumn {
  id: string
  label: string
  group: string
  desc: string
}

// ===== Factor Library (注册表, P1) =====
export type FactorKind = 'base' | 'virtual' | 'composite' | 'custom'
export type FactorStability = 'stable' | 'experimental' | 'deprecated'

export interface FactorLibraryItem {
  id: string
  label: string
  group: string
  kind: FactorKind
  version: number
  formula: string
  direction: 'high' | 'low' | 'none'
  unit: string
  warmup_bars: number
  pit: boolean
  asset_types: string[]
  stability: FactorStability
  scale_free: boolean
  dependencies: string[]
}

export interface FactorDslError {
  code: string
  message: string
  position: { offset: number; line: number }
  detail?: Record<string, unknown>
}

export interface FactorValidateResponse {
  ok: boolean
  errors: FactorDslError[]
  dependencies: string[]
  referenced_factors: string[]
  warmup_bars: number
  cross_sectional: boolean
}

export interface FactorTrialResponse {
  ok: boolean
  n_dates: number
  null_ratio: number | null
  ic_mean: number | null
  ic_std: number | null
  ir: number | null
  ic_win_rate: number | null
  t_newey_west?: number | null
  ic_series: { date: string; ic: number; n_symbols: number }[]
  message?: string
}

export interface GroupStat {
  group: number
  label: string
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  win_rate: number
}

/** 回测候选 (candidates): 回测报告的持久化标量摘要; metrics 单位为小数 (0.052 = 5.2%) */
export interface BacktestCandidate {
  id: string
  kind: 'factor' | 'strategy'
  name: string
  source_id: string
  metrics: Partial<{
    total_return: number
    annual_return: number
    max_drawdown: number
    sharpe: number
    sortino: number
    win_rate: number
    n_trades: number
    profit_factor: number
    avg_return: number
    median_return: number
  }>
  data_as_of: string | null
  status: 'pending' | 'validated' | 'rejected'
  created_at: string
}

export interface FactorBacktestResult {
  run_id: string
  config: Record<string, any>
  ic_mean: number | null
  ic_std: number | null
  ir: number | null
  ic_win_rate: number | null
  ic_series: { date: string; ic: number }[]
  group_stats: GroupStat[]
  group_nav: Record<string, any>[]
  long_short_stats: Record<string, any>
  long_short_nav: { date: string; value: number }[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

export interface FactorBatchItem {
  factor_name: string
  label: string
  group: string
  ic_mean: number | null
  ir: number | null
  ic_win_rate: number | null
  long_short_return: number | null
  long_short_max_drawdown: number | null
  n_symbols: number
  n_dates: number
  elapsed_ms: number
  error: string | null
  // metrics_v2 (P3): NW HAC t 值与 BH-FDR q 值; 样本不足为 null (前端降级经验规则)
  t_naive?: number | null
  t_newey_west?: number | null
  nw_lag?: number | null
  p_value?: number | null
  q_value?: number | null
}

export interface FactorBatchResult {
  run_id: string
  config: Record<string, any>
  results: FactorBatchItem[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

// ===== Factor / strategy mining =====
export type MiningBudgetProfile = 'exploratory' | 'balanced' | 'strict'
export type MiningRunStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'succeeded_with_budget_exhausted'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'skipped_prerequisite'

export interface MiningAvailability {
  asset_type: 'stock' | 'etf'
  budget_profile: MiningBudgetProfile
  trading_bars: number
  required_bars: number
  outer_folds: number
  required_outer_folds: number
  eligible: boolean
  available_start: string | null
  available_end: string | null
  effective_start: string | null
  effective_end: string | null
  suggested_start: string | null
}

export interface SetupStatusRow {
  setup: string
  status: 'active' | 'degraded'
  degraded_at?: string
  restored_at?: string
  evidence?: { samples?: number; avg_r?: number | null; window_start?: string; window_end?: string }
}

export interface MiningRequestV1 {
  factor_names: string[]
  strategy_ids?: string[]
  symbols?: string[] | null
  asset_type?: 'stock' | 'etf'
  start?: string | null
  end?: string | null
  budget_profile?: MiningBudgetProfile
  cost_model?: 'us_per_share_v1'
  commission_per_share?: number
  commission_min?: number
  slippage_bps?: number
  correlation_threshold?: number
  max_combination_factors?: number
  beam_width?: number
  max_finalists?: number
  force?: boolean
}

export interface MiningRunProgress {
  phase: string
  label?: string
  done?: number
  total?: number
  percent?: number
  elapsed_ms?: number
  message?: string
}

export interface MiningRun {
  run_id: string
  signature: string
  status: MiningRunStatus
  request: MiningRequestV1 & { auto?: boolean; auto_screening?: AutoScreening }
  source?: 'manual' | 'scheduled' | 'auto'
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
  data_as_of?: string | null
  progress?: MiningRunProgress | null
  error?: string | null
  reused?: boolean
  summary?: MiningResultSummary | null
}

// 自动挖掘 L1 筛选摘要 (后端 app/services/auto_mining.py 产出结构)
export interface AutoScreening {
  profile: MiningBudgetProfile
  gate: { min_abs_ic: number; min_abs_ir: number; min_abs_t: number; max_q: number }
  screen_window: { start: string; end: string }
  n_total: number
  n_qualified: number
  pool: string[]
  pool_truncated: boolean
  qualified: Array<{ factor_name: string; label: string; group: string; ic: number | null; ir: number | null; t: number | null; q: number | null; direction: 1 | -1 }>
  failed: Array<{ factor_name: string; label: string; group: string; ic: number | null; ir: number | null; t: number | null; q: number | null; reason: string }>
  reason_counts: Record<string, number>
  elapsed_ms: number
}

export interface MiningAutoStartPayload {
  asset_type?: 'stock' | 'etf'
  start?: string | null
  end?: string | null
  budget_profile?: MiningBudgetProfile
  correlation_threshold?: number
  force?: boolean
}

export interface MiningAutoStartResponse {
  started: boolean
  reason?: string
  run?: MiningRun
  screening?: AutoScreening
}

export interface MiningResultSummary {
  factor_count: number
  selected_factor_count: number
  candidate_count: number
  valid_fold_count: number
  skipped_fold_count: number
  confidence: 'low' | 'standard' | 'high'
  budget_exhausted?: boolean
  elapsed_ms?: number
  peak_rss_bytes?: number
}

export interface MiningFactorRow {
  factor_name: string
  label?: string
  direction: 1 | -1
  score: number | null
  ic_mean: number | null
  ir: number | null
  coverage: number | null
  turnover: number | null
  spread_return?: number | null
  spread_sharpe?: number | null
  selected: boolean
  excluded_reason?: string | null
}

export interface MiningRegimeRow {
  state: 'overall' | 'strong' | 'range' | 'weak' | string
  label: string
  n_dates: number
  total_return: number | null
  sharpe: number | null
  max_drawdown: number | null
}

export interface MiningFoldRow {
  fold: number
  label?: string
  train_start?: string
  train_end?: string
  test_start?: string
  test_end?: string
  selected_factors?: string[]
  total_return: number | null
  sharpe: number | null
  max_drawdown?: number | null
  n_trades?: number | null
  skipped?: boolean
  reason?: string | null
  evaluation_kind?: 'selected' | 'cross' | 'benchmark' | null
}

export interface MiningCandidateGate {
  qualified: boolean
  reasons: string[]
}

export interface MiningCandidateRow {
  signature: string
  name: string
  kind: 'factor_combination' | 'existing_strategy'
  factor_names?: string[]
  strategy_id?: string | null
  regime_state?: string | null
  score: number | null
  oos_return: number | null
  oos_sharpe: number | null
  oos_max_drawdown: number | null
  oos_positive_fold_ratio: number | null
  oos_n_trades: number | null
  confidence: 'low' | 'standard' | 'high'
  valid_folds?: number | null
  skipped_folds?: number | null
  promoted_candidate_id?: string | null
  published_strategy_id?: string | null
  gate?: MiningCandidateGate | null
  folds?: MiningFoldRow[]
}

export interface MiningTelemetry {
  elapsed_ms?: number
  peak_rss_bytes?: number
  panel_scans?: number
  matrix_bytes?: number
  cache_hits?: number
  fold_reuses?: number
  serialized_result_bytes?: number
  phase_ms?: Record<string, number>
}

export interface MiningRequestSummary {
  asset_type: string
  budget_profile: string
  start: string | null
  end: string | null
  factor_count: number
  strategy_count: number
  cost_model?: 'us_per_share_v1' | 'legacy_pct' | null
  commission_per_share?: number | null
  commission_min?: number | null
  commission_pct: number | null
  stamp_tax_pct: number | null
  slippage_bps: number | null
  correlation_threshold: number | null
}

export interface MiningResult {
  run_id: string
  methodology_version: string
  algorithm_version: string
  data_as_of: string | null
  summary: MiningResultSummary
  request_summary?: MiningRequestSummary | null
  factors: MiningFactorRow[]
  correlation: {
    labels: string[]
    matrix: (number | null)[][]
    pair_counts?: (number | null)[][]
    threshold: number
  }
  regimes: MiningRegimeRow[]
  candidates: MiningCandidateRow[]
  folds: MiningFoldRow[]
  telemetry: MiningTelemetry
}

export interface MiningEvent {
  id: number
  type: string
  timestamp?: string
  payload?: Record<string, unknown>
  message?: string
}

export interface MiningScheduleConfig {
  mining_schedule_enabled: boolean
  mining_schedule_weekday: number
  mining_budget_profile: Exclude<MiningBudgetProfile, 'exploratory'>
}

export type ResearchCandidateKind = 'factor' | 'strategy'
export type ResearchCandidateStatus = 'pending' | 'validated' | 'rejected'

export interface ResearchCandidate {
  id: string
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of: string | null
  status: ResearchCandidateStatus
  created_at: string
  updated_at: string
}

export interface ResearchCandidateCreate {
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of?: string | null
  status?: ResearchCandidateStatus
}

// ===== Strategy Backtest =====
export interface BacktestCostParams {
  cost_model?: 'us_per_share_v1' | 'legacy_pct'
  commission_per_share?: number
  commission_min?: number
  fees_pct?: number | null
  commission_pct?: number | null
  stamp_tax_pct?: number | null
}

export interface StrategyBacktestParams extends BacktestCostParams {
  strategy_id: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
  matching?: 'close_t' | 'open_t+1'
  entry_fill?: 'close_t' | 'open_t+1' | null
  exit_fill?: 'close_t' | 'open_t+1' | 'signal_next_minute' | null
  slippage_bps?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: 'equal' | 'score_weight'
  asset_type?: 'stock' | 'etf' | 'index'
  mode?: 'position' | 'full'
  holding_days?: number
  minute_fill?: boolean
  regime_filter?: { states?: string[]; min_score?: number } | null
}

/** POST and SSE share one explicit cost contract; null legacy fees stay unset. */
export function strategyBacktestPayload(params: StrategyBacktestParams): StrategyBacktestParams {
  return { cost_model: 'us_per_share_v1', commission_per_share: 0.005, commission_min: 1, fees_pct: null, ...params }
}

export function strategyBacktestQuery(params: StrategyBacktestParams): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(strategyBacktestPayload(params))) {
    if (value == null || value === '') continue
    query.set(key, key === 'symbols' ? (value as string[]).join(',')
      : typeof value === 'object' ? JSON.stringify(value) : String(value))
  }
  return query.toString()
}

export interface StrategyBacktestTrade {
  symbol: string
  name?: string
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  pnl_pct: number
  duration: number
  exit_reason: string
  shares?: number
  lots?: number
  position_pct?: number
  entry_value?: number
  exit_value?: number
  pnl_amount?: number
  entry_score?: number | null
  entry_signal_date?: string | null
  exit_signal_date?: string | null
  blocked_exit_days?: number
  entry_signal_id?: string | null
  exit_signal_id?: string | null
}

export interface StrategyBacktestResult {
  run_id: string
  config: Record<string, any>
  stats: Record<string, any>
  equity_curve: { date: string; value: number; cash?: number; positions?: number; exposure?: number }[]
  drawdown_curve: { date: string; value: number }[]
  benchmark_curve?: { date: string; value: number; close?: number; name?: string; symbol?: string }[]
  trades: StrategyBacktestTrade[]
  /** v1 因子归因: 入场信号日因子值快照 × 成交盈亏 (胜/败单均值对比); 无评分因子或分钟路径时为 null */
  factor_attribution?: {
    factors: { factor: string; win_mean: number | null; lose_mean: number | null; win_n: number; lose_n: number }[]
    n_win: number
    n_lose: number
  } | null
  per_symbol_stats: {
    symbol: string
    n_trades: number
    total_return: number
    win_rate: number
    best: number
    worst: number
  }[]
  strategy_info: {
    id: string
    name: string
    description: string
    entry_signals: string[]
    exit_signals: string[]
    stop_loss: number | null
    take_profit: number | null
    trailing_stop: number | null
    trailing_take_profit_activate: number | null
    trailing_take_profit_drawdown: number | null
    score_min: number | null
    score_max: number | null
    max_hold_days: number | null
    source: string
    execution_backend?: string
    // 叠加策略回测: 子策略构成与权重归因
    composite_children?: { id: string; weight: number }[]
  }
  elapsed_ms: number
  error: string | null
}

// ===== Settings =====

/** 端点发现清单 —— 对应 tickflow.org/endpoints.json */
export type SetupLevel = 'required' | 'recommended' | 'optional'
export interface SetupItem {
  id: string
  group: 'market' | 'news' | 'ai' | 'notify'
  label: string
  level: SetupLevel
  configured: boolean
  source: string
  purpose: string
  impact: string
  /** 设置页相对路径, 如 settings?tab=ai#ai-tasks; 空 = 无需配置 */
  where: string
  env: string[]
  link: string
  note: string
  warn: string
}
export interface SetupChecklist {
  items: SetupItem[]
  summary: { required_missing: string[]; recommended_missing: string[]; ready: boolean }
  dismissed: boolean
}

export interface SettingsState {
  mode: 'none' | 'free' | 'api_key'
  tickflow_api_key_masked: string
  has_tickflow_key: boolean
  tier_label: string
  current_endpoint: string
  probe_log: string[]
  missing_caps: string[]
  extras_caps: string[]
  // 首次使用引导
  onboarding_completed: boolean
  // AI 配置
  ai_provider: string
  ai_base_url: string
  ai_api_key_masked: string
  has_ai_key: boolean
  ai_configured?: boolean
  ai_model: string
  ai_openai_model?: string
  ai_reasoning_effort?: string
  ai_codex_model?: string
  ai_codex_command?: string
  ai_codex_reasoning_effort?: string
  ai_user_agent: string
  ai_max_output_tokens?: number
  ai_context_window?: number
}

export interface DataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  path?: string | null
}

/** 内置可选插件数据源 (plugins/ 目录, 需手动装依赖) */
export interface PluginDataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  runtime: string          // node | python | none
  available: boolean       // 依赖是否已安装
  status: string           // 可用性原因 (供 UI 显示)
  description: string
  install_hint: string     // 未装依赖时显示的安装命令
  homepage?: string        // 插件官网/申请地址 (manifest 可选声明)
  api_key_env?: string     // 声明后设置页提供 Key 输入框 (先探后存)
  api_key_masked?: string  // 当前生效 Key 的脱敏串 (secrets.json 优先, .env 兜底; 与 TickFlow Key 同一展示契约)
}

/** 数据源路由偏好字段 (每个能力一个, 与后端能力注册表一一对应) */
export type ProviderField =
  | 'daily_data_provider'
  | 'adj_factor_provider'
  | 'minute_data_provider'
  | 'full_minute_data_provider'
  | 'realtime_data_provider'
  | 'financial_data_provider'

/** 能力路由矩阵中的一个候选源 (candidates 只含当前确实可提供该能力的源) */
export interface CapabilityCandidate {
  name: string
  display: string
  kind: 'builtin' | 'plugin' | 'custom'
  available: boolean
  status: string
  note?: string | null
}

/** 一个能力 (标准化数据集) 的路由视图 */
export interface CapabilityRoute {
  id: string
  label: string
  desc: string
  field: ProviderField | null                    // null = 不可路由能力 (仅 TickFlow 提供)
  default: string
  tf_tier: string                                  // TickFlow 所需最低订阅档位
  tf_available: boolean                            // 当前 TickFlow 档位是否提供该能力
  usable: boolean                                  // 生效源当前能否真正提供 (各页能力门控的统一判定)
  current: string                                  // 原始偏好值
  current_display: string
  effective: string                                // 当前生效源 (独立路由, current 即生效)
  effective_display: string
  candidates: CapabilityCandidate[]                // 当前可用候选 (按当前 TickFlow 档位过滤)
  pending: CapabilityCandidate[]                   // 声明了该能力但未就绪的源 (置灰提示)
}

export interface CapabilityMatrix {
  tickflow_tier?: string                           // TickFlow 当前档位基础名 (none/free/...)
  capabilities: CapabilityRoute[]
}

export interface DataSourceLoadError {
  name?: string
  path: string
  errors: string[]
}

export interface DataSourcesResponse {
  builtin: DataSourceItem[]
  plugins: PluginDataSourceItem[]
  custom: DataSourceItem[]
  errors: DataSourceLoadError[]
  config_dir: string
}

export interface DataSourceTestResult {
  provider: string
  dataset: string
  rows: number
  columns: string[]
  preview: Record<string, unknown>[]
}

/** 插件 Key 保存结果 (先探后存: 无效 Key 返回 ok=false 且不落盘) */
export interface PluginKeyResult {
  ok: boolean
  reason?: string
  error?: string
  api_key_masked?: string
  plugin_available?: boolean
  plugin?: PluginDataSourceItem | null
}

export interface DatasetConfig {
  url: string
  method: string
  batch?: number | null
  rpm?: number | null
  response_path: string
  field_map: Record<string, string>
  transforms?: Record<string, string>
  symbols_param?: string
  start_param?: string
  end_param?: string
  asset_type_param?: string | null
  freq_param?: string | null
  timeout?: number | null
}

export interface AuthConfig {
  type: string
  token_env?: string | null
  header?: string
  param?: string
}

export interface CustomSourceConfig {
  name: string
  display_name: string
  auth: AuthConfig
  datasets: Record<string, DatasetConfig>
}

export interface WecomBotStatus {
  enabled: boolean
  running: boolean
  connected: boolean
  bot_id_configured: boolean
  secret_configured: boolean
  last_error: string
}

export interface Preferences {
  realtime_quotes_enabled: boolean
  watchlist_groups_in_nav: boolean
  minute_sync_enabled: boolean
  minute_sync_days: number
  minute_sync_segment_days: number
  minute_refresh_enabled: boolean
  minute_refresh_interval: number
  daily_data_provider?: string
  adj_factor_provider?: string
  minute_data_provider?: string
  /** 全量分钟 (盘中全市场分钟落盘) 生效源; 默认 tickflow (需 Expert 档) */
  full_minute_data_provider?: string
  /** 分钟源 1 分钟历史深度(交易日); null/缺省 = 深历史 (如 tickflow)。分时档位据此收窄 */
  minute_history_days?: number | null
  realtime_data_provider?: string
  financial_data_provider?: string
  data_source_job_timeout_s: number
  data_source_long_job_timeout_s: number
  minute_batch_compress: boolean
  daily_batch_compress: boolean
  realtime_pull_stock?: boolean
  realtime_pull_etf?: boolean
  pipeline_pull_a_share: boolean
  pipeline_pull_etf: boolean
  pipeline_pull_index: boolean
  pipeline_index_symbols: string
  pipeline_schedule: { hour: number; minute: number }
  instruments_schedule: { hour: number; minute: number }
  enriched_batch_size: number
  index_daily_batch_size: number
  limit_ladder_monitor_enabled: boolean
  depth_polling_interval: number
  depth_finalize_time: { hour: number; minute: number }
  review_schedule: { enabled: boolean; hour: number; minute: number }
  review_push_channels: string[]
  review_push_mode?: 'auto' | 'manual'
  sse_refresh_pages: Record<string, boolean>
  strategy_monitor_enabled: boolean
  strategy_monitor_ids: string[]
  system_notify_enabled: boolean
  /** 审计 2026-10-26 L-9: 掩码地址 (前缀 + •••• + 末 4 位); 原样回传 = 保持不变 */
  feishu_webhook_url?: string
  /** 恒为空串, 只看 feishu_webhook_secret_set */
  feishu_webhook_secret?: string
  feishu_webhook_secret_set?: boolean
  feishu_app_credentials_set?: boolean
  feishu_app_chat_id?: string
  feishu_app_configured?: boolean
  wecom_webhook_url?: string
  custom_webhook_url?: string
  custom_webhook_secret_set?: boolean
  email_smtp_config?: EmailSmtpConfig
  email_smtp_password_set?: boolean
  wecom_bot_id?: string
  wecom_bot_secret?: string
  wecom_bot_secret_set?: boolean
  wecom_bot_enabled?: boolean
  webhook_enabled_default?: boolean
  webhook_default_channels?: string[]
  nav_order: string[]
  nav_hidden: string[]
  screener_auto_run: boolean
  minute_intraday_refresh: boolean
  minute_intraday_refresh_interval: number
  monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
}

export interface EmailSmtpConfig {
  host: string
  port: number
  security: 'ssl' | 'starttls' | 'none'
  username: string
  from_address: string
  to_addresses: string[]
}

/** 监控中心 ext 字段单项配置 (行业/概念标签的来源 + 显示裁剪) */
export interface MonitorExtFieldItem {
  /** "configId.fieldName" */
  field: string
  /** 显示前N个标签, 0=不限制 */
  maxTags?: number
  /** 隐藏的位置 (0-based), 如 [0] 表示隐藏第一个 */
  hiddenIndices?: number[]
}
export interface StrategyAlertEvent {
  source: 'strategy' | 'depth'
  type: string
  strategy_id?: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

// ===== API surface =====
export const api = {
  health: () => request<{ status: string; version: string; mode: string }>('/health'),

  aiTasks: () => request<{ tasks: AiTaskRow[] }>('/api/settings/ai/tasks'),
  saveAiTask: (task: string, body: { model?: string; base_url?: string; api_key?: string | null; thinking?: string }) =>
    request<{ tasks: AiTaskRow[] }>(`/api/settings/ai/tasks/${encodeURIComponent(task)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // ===== QVeris 付费数据 =====
  qverisUsage: (refreshBalance = false) => request<QverisUsage>(`/api/qveris/usage${refreshBalance ? '?refresh_balance=1' : ''}`),
  qverisSetLimit: (dailyLimit: number) =>
    request<QverisUsage>('/api/qveris/limit', { method: 'POST', body: JSON.stringify({ daily_limit: dailyLimit }) }),
  qverisOptions: (symbol: string, refresh = false) =>
    request<QverisOptions>(`/api/qveris/options?symbol=${encodeURIComponent(symbol)}${refresh ? '&refresh=1' : ''}`),
  qverisEstimates: (symbol: string, refresh = false) =>
    request<QverisEstimates>(`/api/qveris/estimates?symbol=${encodeURIComponent(symbol)}${refresh ? '&refresh=1' : ''}`),
  qverisTranscript: (symbol: string, quarter?: string, refresh = false) =>
    request<QverisTranscript>(`/api/qveris/transcript?symbol=${encodeURIComponent(symbol)}${quarter ? `&quarter=${quarter}` : ''}${refresh ? '&refresh=1' : ''}`),
  qverisDigest: (symbol: string, quarter: string) =>
    request<{ symbol: string; quarter: string; digest: { points: string[] } }>('/api/qveris/transcript/digest', {
      method: 'POST', body: JSON.stringify({ symbol, quarter }),
    }),
  // ===== 个股新闻 =====
  stockNews: (symbol: string, days = 7) =>
    request<{ symbol: string; days: number; items: NewsItem[]; summary: NewsSummary; disclaimer: string }>(
      `/api/news/stock?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  newsSummary: (symbols: string[]) =>
    request<{ summaries: Record<string, NewsSummary>; disclaimer: string }>(
      `/api/news/summary?symbols=${encodeURIComponent(symbols.join(','))}`,
    ),
  newsStatus: () => request<NewsStatus>('/api/news/status'),


  // ===== Auth (访问认证) =====
  authStatus: () =>
    request<{ configured: boolean; authenticated: boolean }>('/api/auth/status'),
  authSetup: (password: string) =>
    request<{ ok: boolean }>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogin: (password: string) =>
    request<{ ok: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),

  settings: () => request<SettingsState>('/api/settings'),
  /** 标记首次使用向导完成（持久化到后端 preferences） */
  completeOnboarding: () =>
    request<{ ok: boolean; onboarding_completed: boolean }>(
      '/api/settings/onboarding/complete', { method: 'POST' },
    ),

  /** 保存 AI 配置 */
  saveAiSettings: (ai: { provider?: string; base_url?: string; api_key?: string; model?: string; reasoning_effort?: string; codex_command?: string; codex_reasoning_effort?: string; user_agent?: string; max_output_tokens?: number; context_window?: number }) =>
    request<{ ok: boolean; ai_provider?: string; ai_model?: string; ai_openai_model?: string; ai_reasoning_effort?: string; ai_codex_model?: string; ai_codex_command?: string; ai_codex_reasoning_effort?: string; ai_configured?: boolean; ai_max_output_tokens?: number; ai_context_window?: number }>('/api/settings/ai', {
      method: 'POST',
      body: JSON.stringify(ai),
    }),

  /** 一键清空 AI 配置(保留自定义 UA) */
  clearAiSettings: () =>
    request<{ ok: boolean }>('/api/settings/ai', { method: 'DELETE' }),

  /** 配置清单 (0.42.0): 每项是否已配 / 不配的影响 / 去哪配; 不含密钥 */
  setupChecklist: () => request<SetupChecklist>('/api/settings/setup-checklist'),
  setSetupBanner: (dismissed: boolean) =>
    request<{ ok: boolean; dismissed: boolean }>('/api/settings/setup-checklist/banner', {
      method: 'POST', body: JSON.stringify({ dismissed }),
    }),
  saveServiceSecret: (name: 'finnhub' | 'sec_user_agent', value: string) =>
    request<{ ok: boolean; configured: boolean; masked: string }>(`/api/settings/service-secret/${name}`, {
      method: 'PUT', body: JSON.stringify({ value }),
    }),
  preferences: () => request<Preferences>('/api/settings/preferences'),
  dataSources: () => request<DataSourcesResponse>('/api/settings/data-sources'),
  capabilityMatrix: () => request<CapabilityMatrix>('/api/settings/capability-matrix'),
  dataSource: (name: string) => request<CustomSourceConfig>(`/api/settings/data-sources/${encodeURIComponent(name)}`),
  saveDataSource: (config: CustomSourceConfig) =>
    request<DataSourcesResponse>('/api/settings/data-sources', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  deleteDataSource: (name: string) =>
    request<DataSourcesResponse>(`/api/settings/data-sources/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  reloadDataSources: () => request<DataSourcesResponse>('/api/settings/data-sources/reload', { method: 'POST' }),
  installPlugin: (name: string) => {
    // npm install 可能耗时较长, 用 6 分钟超时
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 360_000)
    return request<DataSourcesResponse & { install_ok: boolean; install_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'POST', signal: controller.signal },
    ).finally(() => clearTimeout(timer))
  },
  uninstallPlugin: (name: string) =>
    request<DataSourcesResponse & { uninstall_ok: boolean; uninstall_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'DELETE' },
    ),
  savePluginKey: (plugin: string, apiKey: string) => {
    // 先探后存: 后端会用候选 Key 实探一次, 探测超时 10s + 余量
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 30_000)
    return request<PluginKeyResult>('/api/settings/plugin-key', {
      method: 'POST',
      body: JSON.stringify({ plugin, api_key: apiKey }),
      signal: controller.signal,
    }).finally(() => clearTimeout(timer))
  },
  clearPluginKey: (plugin: string) =>
    request<PluginKeyResult>(`/api/settings/plugin-key/${encodeURIComponent(plugin)}`, { method: 'DELETE' }),
  testDataSource: (
    provider: string,
    dataset: string,
    symbols?: string[],
    config?: CustomSourceConfig,
  ) =>
    request<DataSourceTestResult>('/api/settings/data-sources/test', {
      method: 'POST',
      body: JSON.stringify({ provider, dataset, symbols, config }),
    }),
  updateDataProviders: (cfg: Partial<Pick<Preferences, ProviderField>>) =>
    request<Pick<Preferences, ProviderField>>(
      '/api/settings/preferences/data-providers',
      { method: 'PUT', body: JSON.stringify(cfg) },
    ),
  updateDataSourceJobTimeouts: (dataSourceJobTimeoutS: number, dataSourceLongJobTimeoutS: number) =>
    request<Pick<Preferences, 'data_source_job_timeout_s' | 'data_source_long_job_timeout_s'>>(
      '/api/settings/preferences/data-source-job-timeouts',
      {
        method: 'PUT',
        body: JSON.stringify({
          data_source_job_timeout_s: dataSourceJobTimeoutS,
          data_source_long_job_timeout_s: dataSourceLongJobTimeoutS,
        }),

      },
    ),

  /** 分时批量响应 gzip 压缩开关 (网络设置) — 逐请求即时生效 */
  updateMinuteBatchCompress: (enabled: boolean) =>
    request<Pick<Preferences, 'minute_batch_compress'>>('/api/settings/preferences/minute-batch-compress', {
      method: 'PUT',
      body: JSON.stringify({ minute_batch_compress: enabled }),
    }),

  /** 日K批量响应 gzip 压缩开关 (与分时独立) — 逐请求即时生效 */
  updateDailyBatchCompress: (enabled: boolean) =>
    request<Pick<Preferences, 'daily_batch_compress'>>('/api/settings/preferences/daily-batch-compress', {
      method: 'PUT',
      body: JSON.stringify({ daily_batch_compress: enabled }),
    }),
  updateMinuteSync: (enabled: boolean, days: number, segmentDays?: number) =>
    request<Preferences>('/api/settings/preferences/minute-sync', {
      method: 'PUT',
      body: JSON.stringify({
        minute_sync_enabled: enabled,
        minute_sync_days: days,
        ...(segmentDays != null ? { minute_sync_segment_days: segmentDays } : {}),
      }),
    }),

  /** 全量分钟 (盘中全市场分钟落盘) 服务状态 (按能力路由: TickFlow Expert 或声明 full_minute 的插件/自定义源) */
  minuteRefreshStatus: () =>
    request<{
      available: boolean
      enabled?: boolean
      running?: boolean
      /** 读侧 freshness: 本地分区正被服务持续写入 (前端据此切本地读/解除截断) */
      healthy?: boolean
      provider?: string
      provider_effective?: string
      repair_only?: boolean
      interval_seconds?: number
      capability_ok?: boolean
      in_trading_hours?: boolean
      gate_reason?: string | null
      rounds?: number
      last_round_at?: number | null
      last_round_ms?: number | null
      last_rows?: number
      last_symbols?: number
      last_requests?: number
      next_round_at?: number | null
      last_error?: string | null
    }>('/api/settings/minute-refresh/status'),
  updatePipelinePullTypes: (cfg: Partial<Pick<Preferences, 'pipeline_pull_a_share' | 'pipeline_pull_etf' | 'pipeline_pull_index'>>) =>
    request<{
      pipeline_pull_a_share: boolean
      pipeline_pull_etf: boolean
      pipeline_pull_index: boolean
    }>('/api/settings/preferences/pipeline-pull-types', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateRealtimeQuotes: (enabled: boolean) =>
    request<{ realtime_quotes_enabled: boolean; realtime_allowed?: boolean; mode?: string; error?: string }>('/api/settings/preferences/realtime-quotes', {
      method: 'PUT',
      body: JSON.stringify({ realtime_quotes_enabled: enabled }),
    }),
  updateWatchlistGroupsInNav: (enabled: boolean) =>
    request<{ watchlist_groups_in_nav: boolean }>('/api/settings/preferences/watchlist-groups-in-nav', {
      method: 'PUT',
      body: JSON.stringify({ watchlist_groups_in_nav: enabled }),
    }),
  quoteStatus: () =>
    request<{
      enabled: boolean
      running: boolean
      paused?: boolean
      mode?: 'none' | 'watchlist' | 'full_market'
      realtime_allowed?: boolean
      interval_s: number
      symbol_count: number
      watchlist_symbol_count?: number
      index_symbol_count?: number
      etf_symbol_count?: number
      quote_age_ms: number | null
      is_trading_hours: boolean
      is_polling_window?: boolean
      market_phase?: string
      final_sync_done?: boolean
      final_sync_failed?: string | null
      last_fetch_ms: number | null
    }>('/api/intraday/status'),
  quoteInterval: () =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
    ),
  updateQuoteInterval: (interval: number) =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
      { method: 'PUT', body: JSON.stringify({ interval }) },
    ),
  intradayRefresh: () => request<{ status: string }>('/api/intraday/refresh', { method: 'POST' }),
  indexQuotes: (symbols?: string[]) =>
    request<{ rows: IndexQuote[]; count: number }>(
      `/api/intraday/indices${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  updateRealtimeMonitorConfig: (cfg: {
    sse_refresh_pages?: Record<string, boolean>
    strategy_monitor_enabled?: boolean
    strategy_monitor_ids?: string[]
    screener_auto_run?: boolean
    minute_intraday_refresh?: boolean
    minute_intraday_refresh_interval?: number
    monitor_ext_fields?: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  }) =>
    request<{
      sse_refresh_pages: Record<string, boolean>
      strategy_monitor_enabled: boolean
      strategy_monitor_ids: string[]
      screener_auto_run: boolean
      minute_intraday_refresh: boolean
      minute_intraday_refresh_interval: number
      monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
    }>('/api/settings/preferences/realtime-monitor', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  /** secret 省略 = 保持已保存的签名密钥 (L-9) */
  updateFeishuWebhook: (url: string, secret?: string) =>
    request<{ feishu_webhook_url: string; feishu_webhook_secret: string; feishu_webhook_secret_set: boolean }>('/api/settings/preferences/feishu-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url, ...(secret !== undefined ? { secret } : {}) }),
    }),
  feishuAppChats: () =>
    request<{ credentials_set: boolean; chats: { chat_id: string; name: string }[]; detail?: string }>('/api/settings/preferences/feishu-app/chats'),
  updateFeishuApp: (chatId: string) =>
    request<{ feishu_app_chat_id: string; feishu_app_configured: boolean }>('/api/settings/preferences/feishu-app', {
      method: 'PUT',
      body: JSON.stringify({ chat_id: chatId }),
    }),
  updateWecomWebhook: (url: string) =>
    request<{ wecom_webhook_url: string }>('/api/settings/preferences/wecom-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url }),
    }),
  updateCustomWebhook: (url: string, secret?: string) =>
    request<{ custom_webhook_url: string; custom_webhook_secret_set: boolean }>('/api/settings/preferences/custom-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url, ...(secret !== undefined ? { secret } : {}) }),
    }),
  updateEmailSmtp: (config: EmailSmtpConfig, password?: string) =>
    request<{ email_smtp_config: EmailSmtpConfig; email_smtp_password_set: boolean }>('/api/settings/preferences/email-smtp', {
      method: 'PUT',
      body: JSON.stringify({ ...config, ...(password !== undefined ? { password } : {}) }),
    }),
  sendTestWebhook: (channel: 'feishu' | 'feishu_app' | 'wecom' | 'custom' | 'email') =>
    request<{ ok: boolean; detail: string }>('/api/settings/preferences/webhook-test', {
      method: 'POST',
      body: JSON.stringify({ channel }),
    }),
  /** secret 省略 = 保持已保存的 Secret (L-9) */
  updateWecomBot: (botId: string, secret: string | undefined, enabled: boolean = true) =>
    request<{
      wecom_bot_id: string
      wecom_bot_secret: string
      wecom_bot_secret_set?: boolean
      wecom_bot_enabled: boolean
      wecom_bot_status: WecomBotStatus
    }>('/api/settings/preferences/wecom-bot', {
      method: 'PUT',
      body: JSON.stringify({ bot_id: botId, ...(secret !== undefined ? { secret } : {}), enabled }),
    }),
  toggleWecomBot: (enabled: boolean) =>
    request<{ wecom_bot_enabled: boolean; wecom_bot_status: WecomBotStatus }>('/api/settings/preferences/wecom-bot-toggle', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateWebhookDefaultChannels: (channels: string[]) =>
    request<{ webhook_default_channels: string[] }>('/api/settings/preferences/webhook-default-channels', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updatePipelineSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/pipeline-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  saveNavOrder: (nav_order: string[]) =>
    request<{ nav_order: string[] }>('/api/settings/preferences/nav-order', {
      method: 'PUT',
      body: JSON.stringify({ nav_order }),
    }),
  saveNavHidden: (nav_hidden: string[]) =>
    request<{ nav_hidden: string[] }>('/api/settings/preferences/nav-hidden', {
      method: 'PUT',
      body: JSON.stringify({ nav_hidden }),
    }),
  updateInstrumentsSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/instruments-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateEnrichedBatchSize: (size: number) =>
    request<{ enriched_batch_size: number }>('/api/settings/preferences/enriched-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),

  // 自选列表列配置
  watchlistColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/watchlist-columns'),
  updateWatchlistColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/watchlist-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  // 策略结果列表列配置
  screenerResultColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/screener-result-columns'),
  updateScreenerResultColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/screener-result-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  capabilities: () => request<CapabilitiesResponse>('/api/capabilities'),
  version: () => request<{ version: string }>('/api/data/version'),

  klineDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }, extColumns?: string) =>
    request<KlineDailyResponse>(
      (dateRange
        ? `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`)
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : ''),
    ),
  klineDailyLatest: (symbol: string) =>
    request<KlineDailyLatestResponse>(
      `/api/kline/daily/latest?symbol=${encodeURIComponent(symbol)}`,
    ),
  klineDailyBatch: (symbols: string[], days = 12) =>
    request<{ data: Record<string, KlineRow[]> }>('/api/kline/daily-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, days }),
    }),
  klineMinuteBatch: (symbols: string[], date?: string, preferLocal?: boolean, since?: string) =>
    request<{ data: Record<string, MinuteKlineRow[]>; full_minute_local?: boolean; incremental?: boolean }>('/api/kline/minute-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, date, ...(preferLocal ? { prefer_local: true } : {}), ...(since ? { since } : {}) }),
    }),
  instrumentSearch: (q: string, limit = 20, assetTypes?: string) =>
    request<{ results: { symbol: string; name: string; code: string; asset_type?: string }[] }>(
      `/api/kline/instruments/search?q=${encodeURIComponent(q)}&limit=${limit}${assetTypes ? `&asset_types=${encodeURIComponent(assetTypes)}` : ''}`,
    ),

  /** 批量查股票名称 (传入 symbol 列表, 返回 {symbol: name}) */
  instrumentNames: (symbols: string[]) =>
    request<{ names: Record<string, string> }>('/api/kline/instruments/names', {
      method: 'POST',
      body: JSON.stringify(symbols),
    }),
  klineMinute: (symbol: string, date?: string, live?: boolean) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number }
      date: string | null
      rows: MinuteKlineRow[]
      source?: 'local' | 'live' | 'none'
      asset_type?: 'stock' | 'etf' | 'index'
      data_delay_minutes?: number
      official_delay_minutes?: number
      realtime_tail?: { source: string; bars: number; from: string; to: string; note: string } | null
      in_session?: boolean
      realtime_source?: string | null
      as_of?: string | null
      prev_close?: number | null
    }>(
      `/api/kline/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}${live ? '&live=1' : ''}`,
    ),
  klineMinuteRange: (symbol: string, days = 10) =>
    request<{
      symbol: string
      name?: string
      asset_type: 'stock' | 'etf' | 'index'
      requested_days: number
      sessions: MinuteKlineSession[]
      source: 'local' | 'none'
    }>(
      `/api/kline/minute-range?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  syncMinute: (days?: number, extend?: boolean) =>
    request<{ status: string; job_id: string }>('/api/kline/sync_minute', {
      method: 'POST',
      body: JSON.stringify({ ...(days ? { days } : {}), ...(extend ? { extend: true } : {}) }),
    }),
  syncMinuteSingle: (symbol: string, days?: number) =>
    request<{ status: string; symbol: string; rows: number }>('/api/kline/sync_minute_single', {
      method: 'POST',
      body: JSON.stringify({ symbol, ...(days != null ? { days } : {}) }),
    }),
  clearMinute: () =>
    request<{ status: string; removed: number }>('/api/kline/clear_minute', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  extendHistory: (value: number, unit: 'day' | 'month' | 'year') =>
    request<{ status: string; job_id: string }>('/api/kline/extend_history', {
      method: 'POST',
      body: JSON.stringify({ value, unit }),
    }),
  repairDaily: (startDate: string) =>
    request<{ status: string; job_id: string }>('/api/kline/repair_daily', {
      method: 'POST',
      body: JSON.stringify({ start_date: startDate }),
    }),
  rebuildEnriched: () =>
    request<{ status: string; job_id: string }>('/api/kline/rebuild_enriched', {
      method: 'POST',
    }),

  watchlistList: () => request<{ symbols: WatchlistEntry[] }>('/api/watchlist'),
  /** 自选股公司图标缓存: logos = 代码 → 版本 (图片 URL 带上它, 换图即换 URL); pending = 后台仍在取 */
  watchlistLogos: () =>
    request<{ logos: Record<string, string>; pending: string[] }>('/api/watchlist/logos', { quiet: true }),

  // ── ZenTrade 机会 (P3) ──
  opportunitiesToday: () => request<Record<string, unknown>>('/api/opportunities/today'),
  // P2/P6：决策台名称只在展示层清洗（去内部出处名、英文指标名补中文名），后端名称与 id 不变
  deskSources: () => request<DeskSourcesPayload>('/api/opportunities/desk/sources').then(cleanDeskNames),
  deskOverview: () => request<DeskOverviewPayload>('/api/opportunities/desk/overview', { quiet: true }).then(cleanDeskNames),
  /** 0.29.0 机会页一次取齐 (机会板 + 盘中强势 + 大盘条 + 板卡标注) */
  oppPage: () => request<OppPagePayload>('/api/opportunities/page', { quiet: true }),
  intelToday: () => request<IntelPayload>('/api/intel/today', { quiet: true }),
  intelSettings: (macro_impact: IntelImpactLevel) =>
    request<{ macro_impact: IntelImpactLevel }>('/api/intel/settings', { method: 'POST', body: JSON.stringify({ macro_impact }) }),
  intelRefresh: () => request<{ ran: Record<string, number | string> }>('/api/intel/refresh', { method: 'POST', timeoutMs: 180_000 }),
  /** 0.29.0 规则齿轮唯一保存口: 3+3 + 单笔风险 (风险变了才建规则集新版本) */
  saveOppSettings: (long: string[], short: string[], riskPct: number | null, radarPush?: { enabled: boolean; min_rvol: number } | null) =>
    request<{ swing: { long: string[]; short: string[] }; ruleset: { version: number | null; risk_per_trade_pct: number | null }; radar_push?: { enabled: boolean; min_rvol: number } }>(
      '/api/opportunities/settings', { method: 'POST', body: JSON.stringify({ long, short, risk_per_trade_pct: riskPct, ...(radarPush ? { radar_push: radarPush } : {}) }) }),
  cardSettlement: () => request<SettlementPayload>('/api/opportunities/settlement', { quiet: true }),
  swingSettlement: () => request<SwingSettlePayload>('/api/opportunities/swing-settlement', { quiet: true }),
  settlementCards: () => request<SettlementCardsPayload>('/api/opportunities/settlement/cards', { quiet: true }),
  watchlistRules: () => request<WatchlistRulesPayload>('/api/opportunities/rules'),
  /** 0.30.0 挖掘 / AI 策略重新按卡回放: 收盘后立即跑, 交易时段排队到盘后管道之后 */
  runSwingCandidates: () =>
    request<{ started: boolean; queued: boolean; running: boolean; requested_at?: string }>('/api/opportunities/rules/candidates/run', { method: 'POST' }),
  deskEvidence: () => request<EvidencePayload>('/api/opportunities/desk/evidence', { quiet: true }).then(cleanDeskNames),
  setDeskEvidenceMode: (mode: 'v1' | 'v2' | 'v3' | 'v4', note = '') =>
    request<{ ok: boolean; mode: string; previous: string }>('/api/opportunities/desk/evidence/mode', { method: 'POST', body: JSON.stringify({ mode, note }) }),
  runDeskEvidenceV2: () => request<{ started: boolean; reason?: string }>('/api/opportunities/desk/evidence/v2/run', { method: 'POST' }),
  runDeskEvidenceV3: () => request<{ started: boolean; reason?: string }>('/api/opportunities/desk/evidence/v3/run', { method: 'POST' }),
  setDeskSourceStatus: (id: string, status: 'enabled' | 'observe' | 'disabled', note = '') =>
    request<{ ok: boolean }>(`/api/opportunities/desk/sources/${encodeURIComponent(id)}/status`, { method: 'POST', body: JSON.stringify({ status, note }) }),
  portfolioOverview: () => request<Record<string, unknown>>('/api/portfolio/overview'),
  portfolioLive: () => request<PortfolioLive>('/api/portfolio/live'),
  alpacaMirror: () => request<AlpacaMirrorPayload>('/api/trading/alpaca-mirror'),
  systemStatus: () => request<SystemStatusPayload>('/api/trading/system-status'),
  entryFunnel: (days = 10) => request<EntryFunnelPayload>(`/api/trading/funnel?days=${days}`, { quiet: true }),
  setAlpacaMirror: (enabled: boolean, note = '') =>
    request<AlpacaMirrorPayload['summary']>('/api/trading/alpaca-mirror/enabled', { method: 'POST', body: JSON.stringify({ enabled, note }) }),
  setupStatus: () => request<{ setups: SetupStatusRow[]; rule?: string }>('/api/trading/setup-status'),
  restoreSetup: (setup: string, note = '') =>
    request<{ setup: SetupStatusRow }>(`/api/trading/setup-status/${encodeURIComponent(setup)}/restore`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note }),
    }),
  tradingStatus: () => request<{ booted: boolean; scheduled: boolean; in_session: boolean; last_run_at?: string; last_quote_at?: string; last_error?: string; notifications?: {label?: string}; protections?: { earnings_auto?: string; earnings_calendar?: { status?: string; source?: string | null; fetched_at?: string | null }; corporate_actions_auto?: string; corporate_actions?: { status?: string; fetched_at?: string | null; due_at?: string | null } } }>('/api/trading/status'),
  watchlistAdd: (symbol: string, note = '', groupId?: string | null) =>
    request<{ symbols: WatchlistEntry[] }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({ symbol, note, group_id: groupId ?? null }),
    }),
  watchlistBatchAdd: (symbols: string[], note = '', groupId?: string | null, groupIds?: string[]) =>
    request<{ symbols: WatchlistEntry[]; added: number }>('/api/watchlist/batch', {
      method: 'POST',
      body: JSON.stringify({
        symbols,
        note,
        group_id: groupId ?? null,
        group_ids: groupIds?.length ? groupIds : null,
      }),
    }),
  watchlistGroups: () =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups'),
  watchlistGroupCreate: (name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[]; group: WatchlistGroup }>('/api/watchlist/groups', {
      method: 'POST',
      body: JSON.stringify({ name, color }),
    }),
  watchlistGroupRename: (groupId: string, name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'PUT', body: JSON.stringify({ name, color }) },
    ),
  watchlistGroupReorder: (orderedIds: string[]) =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups/reorder', {
      method: 'PUT',
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  watchlistGroupDelete: (groupId: string) =>
    request<{ groups: WatchlistGroup[]; symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'DELETE' },
    ),
  watchlistGroupClear: (groupId: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/clear`,
      { method: 'POST' },
    ),
  watchlistGroupAddMember: (groupId: string, symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}`,
      { method: 'POST' },
    ),
  watchlistGroupRemoveMember: (groupId: string, symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  watchlistImportCsv: (file: File, signal?: AbortSignal, quiet = false) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<WatchlistImportResult>('/api/watchlist/import-csv', {
      method: 'POST',
      body: fd,
      signal,
      quiet,
    })
  },
  watchlistImportCodes: (text: string, signal?: AbortSignal) =>
    request<WatchlistImportResult>('/api/watchlist/import-codes', {
      method: 'POST',
      body: JSON.stringify({ text }),
      signal,
    }),
  watchlistRemove: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  watchlistMoveToTop: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/top`,
      { method: 'POST' },
    ),
  watchlistClear: () =>
    request<{ removed: number }>('/api/watchlist', { method: 'DELETE' }),
  watchlistEnriched: (extColumns?: string) =>
    request<{ rows: any[]; as_of: string | null; elapsed_ms: number }>(
      extColumns
        ? `/api/watchlist/enriched?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/watchlist/enriched',
    ),

  // timeframe='all' 时不传参数 → 后端不过滤周期, 返回日线+分钟合并列表
  screenerStrategies: async (assetType?: 'stock' | 'etf' | 'index', timeframe: '1d' | '1m' | '1h' | 'all' = '1d') => {
    const data = await request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies?${assetType ? `asset_type=${assetType}&` : ''}${timeframe !== 'all' ? `timeframe=${timeframe}` : ''}`,
    )
    return { presets: data.strategies, load_errors: data.load_errors }
  },
  screenerRunPreset: (strategy_id: string, pool?: string[], asOf?: string, extColumns?: string, assetType: 'stock' | 'etf' = 'stock', timeframe: '1d' | '1m' | '1h' = '1d') =>
    request<ScreenerResult>('/api/screener/run_preset', {
      method: 'POST',
      timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ strategy_id, pool, as_of: asOf ?? null, ext_columns: extColumns || null, asset_type: assetType, timeframe }),
    }),
  screenerRunAll: (asOf?: string, strategyIds?: string[], assetType: 'stock' | 'etf' = 'stock', timeframe: '1d' | '1m' = '1d') =>
    request<ScreenerRunAllSummary>(
      '/api/screener/run_all', { method: 'POST', timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS, body: JSON.stringify({ as_of: asOf ?? null, strategy_ids: strategyIds ?? null, asset_type: assetType, timeframe, summary_only: true }) },
    ),
  screenerCachedSummary: () =>
    request<ScreenerCachedSummary>('/api/screener/cached-summary'),
  screenerCachedResult: (strategyId: string, extColumns?: string) =>
    request<ScreenerCachedResult>(
      extColumns
        ? `/api/screener/cached-result/${encodeURIComponent(strategyId)}?ext_columns=${encodeURIComponent(extColumns)}`
        : `/api/screener/cached-result/${encodeURIComponent(strategyId)}`,
    ),
  screenerCached: (extColumns?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string; rows: any[] }>; today_ever_matched: Record<string, string[]> | null; today_ever_rows: Record<string, Record<string, any>> | null; updated_at: number | null }>(
      extColumns
        ? `/api/screener/cached?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/screener/cached',
    ),
  marketSnapshot: () =>
    request<{ as_of: string | null; rows: MarketSnapshotRow[] }>('/api/screener/market-snapshot'),
  overviewMarket: (asOf?: string) => request<OverviewMarket>(`/api/overview/market${asOf ? `?as_of=${asOf}` : ''}`),

  backtestStatus: () => request<{ available: boolean }>('/api/backtest/status'),

  /** 策略/因子候选 (回测报告的持久化摘要, 模拟盘对比用) */
  backtestCandidates: () =>
    request<{ items: BacktestCandidate[] }>('/api/backtest/candidates'),

  factorColumns: () =>
    request<{ columns: FactorColumn[] }>('/api/backtest/factor/columns'),

  factorLibrary: (assetType?: 'stock' | 'etf') =>
    request<{ factors: FactorLibraryItem[] }>(
      `/api/factors${assetType ? `?asset_type=${assetType}` : ''}`,
    ),

  factorValidate: (formula: string) =>
    request<FactorValidateResponse>('/api/factors/validate', {
      method: 'POST',
      body: JSON.stringify({ formula }),
    }),

  factorTrial: (payload: { formula: string; asset_type?: 'stock' | 'etf'; days?: number }) =>
    request<FactorTrialResponse>('/api/factors/trial', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorCustomCreate: (payload: {
    id?: string
    label: string
    group?: string
    formula: string
    description?: string
    direction?: 'high' | 'low' | 'none'
  }) =>
    request<{ ok: boolean; id: string; version: number }>('/api/factors/custom', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorCustomUpdate: (factorId: string, payload: {
    label: string
    group?: string
    formula: string
    description?: string
    direction?: 'high' | 'low' | 'none'
  }) =>
    request<{ ok: boolean; id: string; version: number; status: string }>(
      `/api/factors/custom/${encodeURIComponent(factorId)}/update`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  factorCompositeCreate: (payload: {
    id?: string
    label: string
    group?: string
    members: Record<string, number>
    description?: string
    direction?: 'high' | 'low' | 'none'
  }) =>
    request<{ ok: boolean; id: string; version: number }>('/api/factors/composite', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorDelete: (factorId: string, force = false) =>
    request<{ ok: boolean; id: string; removed_references?: string[] }>(
      `/api/factors/custom/${encodeURIComponent(factorId)}${force ? '?force=true' : ''}`,
      { method: 'DELETE', quiet: true },
    ),

  factorSetStatus: (factorId: string, status: 'draft' | 'active' | 'watch' | 'retired') =>
    request<{ ok: boolean; id: string; status: string }>(
      `/api/factors/custom/${encodeURIComponent(factorId)}/status`,
      { method: 'POST', body: JSON.stringify({ status }) },
    ),

  factorSetGroup: (factorId: string, group: string) =>
    request<{ ok: boolean; id: string; group: string }>(
      `/api/factors/custom/${encodeURIComponent(factorId)}/group`,
      { method: 'POST', body: JSON.stringify({ group }) },
    ),

  factorRun: (payload: {
    factor_name: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    /** Compatibility alias: new factor runs accept zero fees only. */
    fees_pct?: 0
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBacktestResult>('/api/backtest/factor/run', {
      method: 'POST',
      timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
      body: JSON.stringify(payload),
    }),

  factorBatch: (payload: {
    factor_names: string[]
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    /** Compatibility alias: new factor runs accept zero fees only. */
    fees_pct?: 0
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBatchResult>('/api/backtest/factor/batch', {
      method: 'POST',
      timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS,
      body: JSON.stringify(payload),
    }),

  miningRuns: () =>
    request<{ items: MiningRun[] }>('/api/backtest/mining/runs'),

  miningAvailability: (params: {
    assetType: 'stock' | 'etf'
    budgetProfile: MiningBudgetProfile
    start?: string
    end?: string
  }) => {
    const query = new URLSearchParams({
      asset_type: params.assetType,
      budget_profile: params.budgetProfile,
    })
    if (params.start) query.set('start', params.start)
    if (params.end) query.set('end', params.end)
    return request<MiningAvailability>(`/api/backtest/mining/availability?${query}`, {
      quiet: true,
    })
  },

  miningRun: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}`),

  miningAutoStart: (payload: MiningAutoStartPayload) =>
    request<MiningAutoStartResponse>('/api/backtest/mining/auto', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningStart: (payload: MiningRequestV1) =>
    request<MiningRun>('/api/backtest/mining/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningResult: (runId: string) =>
    request<MiningResult>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/result`),

  miningCancel: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    }),

  miningPromote: (runId: string, signature: string) =>
    request<ResearchCandidate>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/promote`,
      { method: 'POST' },
    ),

  miningPublish: (runId: string, signature: string) =>
    request<{ ok: boolean; strategy_id: string }>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/publish`,
      { method: 'POST' },
    ),

  miningConfig: () =>
    request<MiningScheduleConfig>('/api/backtest/mining/config'),

  updateMiningConfig: (payload: Partial<MiningScheduleConfig>) =>
    request<MiningScheduleConfig>('/api/backtest/mining/config', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidates: () =>
    request<{ items: ResearchCandidate[] }>('/api/backtest/candidates'),

  researchCandidateCreate: (payload: ResearchCandidateCreate) =>
    request<ResearchCandidate>('/api/backtest/candidates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  researchCandidateUpdate: (
    id: string,
    payload: { name?: string; status?: ResearchCandidateStatus },
  ) =>
    request<ResearchCandidate>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidateDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  pipelineRun: () => request<{ job_id: string; reused: boolean }>(
    '/api/pipeline/run', { method: 'POST' },
  ),
  pipelineJob: (id: string) => request<PipelineJob>(`/api/pipeline/jobs/${id}`),
  /** 手动停止一个 running/pending 的同步任务 (协作式: 当前分块完成后线程自行退出) */
  pipelineJobCancel: (id: string) =>
    request<{ cancelled: string }>(`/api/pipeline/jobs/${id}/cancel`, { method: 'POST' }),
  pipelineJobs: (limit = 20) =>
    request<{ active_id: string | null; jobs: PipelineJobSummary[] }>(
      `/api/pipeline/jobs?limit=${limit}`,
    ),

  dataStatus: () => request<DataStatus>('/api/data/status'),
  dataClear: () => request<{ deleted_files: number }>('/api/data/clear', { method: 'POST' }),
  enrichedSchema: (table: string) => request<EnrichedField[]>(`/api/data/schema/${table}`),

  // ===== 扩展数据 =====
  extDataList: () =>
    request<{ items: ExtDataConfig[] }>('/api/ext-data'),

  extDataRows: (id: string, opts?: { date?: string; limit?: number; columns?: string[] }) => {
    const qs = new URLSearchParams()
    if (opts?.date) qs.set('date', opts.date)
    if (opts?.limit) qs.set('limit', String(opts.limit))
    if (opts?.columns?.length) qs.set('columns', opts.columns.join(','))
    const suffix = qs.toString()
    return request<ExtDataRowsResult>(`/api/ext-data/${encodeURIComponent(id)}/rows${suffix ? `?${suffix}` : ''}`)
  },

  dimensionMembers: (id: string, opts: { field: string; value: string; date?: string; limit?: number }) => {
    const qs = new URLSearchParams({ field: opts.field, value: opts.value })
    if (opts.date) qs.set('date', opts.date)
    if (opts.limit) qs.set('limit', String(opts.limit))
    return request<DimensionMembersResult>(`/api/ext-data/${encodeURIComponent(id)}/dimension-members?${qs.toString()}`)
  },

  dimensionIntraday: (id: string, opts: { field: string; value: string; date?: string }) => {
    const qs = new URLSearchParams({ field: opts.field, value: opts.value })
    if (opts.date) qs.set('date', opts.date)
    return request<DimensionIntradayResult>(`/api/ext-data/${encodeURIComponent(id)}/dimension-intraday?${qs.toString()}`)
  },

  analysisMenus: () =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus'),

  analysisMenu: (id: string) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`),

  analysisMenuSave: (id: string, body: Omit<AnalysisMenu, 'id' | 'created_at' | 'updated_at' | 'builtin'>) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  analysisMenuDelete: (id: string) =>
    request<{ status: string }>(`/api/analysis-menus/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  extDataCreate: (body: { id: string; label: string; mode: 'snapshot' | 'timeseries'; fields: { name: string; dtype: string; label: string }[]; description?: string; symbol_map?: Record<string, string>; code_map?: Record<string, string> }) =>
    request<ExtDataConfig>('/api/ext-data', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataUpdate: (id: string, body: { label?: string; fields?: { name: string; dtype: string; label: string }[]; description?: string }) =>
    request<ExtDataConfig>(`/api/ext-data/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  extDataDelete: (id: string) =>
    request<{ status: string }>(`/api/ext-data/${id}`, { method: 'DELETE' }),

  extDataUpload: (id: string, file: File, snapshotDate?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/upload${snapshotDate ? `?snapshot_date=${snapshotDate}` : ''}`,
      { method: 'POST', body: fd },
    )
  },

  extDataSchemaAll: () =>
    request<{ items: { id: string; label: string; mode: string; columns: { name: string; type: string; label: string }[] }[] }>('/api/ext-data/schema-all'),

  extDataPullConfig: (id: string, body: {
    url: string; method?: string; headers?: Record<string, string>; body?: string;
    response_path?: string; field_map?: Record<string, string>;
    schedule_minutes?: number; enabled?: boolean;
    time_window_start?: string | null; time_window_end?: string | null;
    date_param?: string | null;
    time_field?: string | null;
    auth?: ExtPullAuth;
    timeout_seconds?: number;
  }) =>
    request<{ status: string; pull: PullConfig }>(
      `/api/ext-data/${id}/pull`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  /** 查询拉取接口 API Key 状态 (脱敏, 不返回明文) */
  extDataApiKey: (id: string) =>
    request<{ key_set: boolean; masked_key: string }>(
      `/api/ext-data/${encodeURIComponent(id)}/api-key`,
    ),

  /** 设置 (或空串清除) 拉取接口的 API Key */
  extDataApiKeySet: (id: string, key: string) =>
    request<{ status: string; key_set: boolean; masked_key: string }>(
      `/api/ext-data/${encodeURIComponent(id)}/api-key`,
      { method: 'PUT', body: JSON.stringify({ key }) },
    ),

  extDataPullTest: (id: string, timeoutSeconds?: number) =>
    request<{ status: string; total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean }>(
      `/api/ext-data/${id}/pull/test`,
      { method: 'POST', timeoutMs: extPullTimeoutMs(timeoutSeconds) },
    ),

  extDataPullRun: (id: string, timeoutSeconds?: number) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/pull/run`,
      { method: 'POST', timeoutMs: extPullTimeoutMs(timeoutSeconds) },
    ),

  /** 历史回补: 按本地交易日逐日拉取写入 timeseries 分区 (需 pull.date_param)。
   *  timeoutMs 由调用方按 天数×单日超时 估算传入 (服务端逐日串行, 总耗时随天数线性)。 */
  extDataBackfill: (id: string, start: string, end: string, timeoutMs?: number) =>
    request<ExtDataBackfillResult>(
      `/api/ext-data/${encodeURIComponent(id)}/backfill?start=${start}&end=${end}`,
      { method: 'POST', timeoutMs: timeoutMs ?? 600_000 },
    ),

  extDataDetectFields: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ fields: { name: string; dtype: string; label: string }[]; rows: number; symbol_candidates: string[]; code_candidates: string[] }>(
      '/api/ext-data/detect-fields',
      { method: 'POST', body: fd },
    )
  },

  extDataDetectUrl: (body: ExtDataDetectUrlRequest) =>
    request<ExtDataDetectUrlResult>('/api/ext-data/detect-url', {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: extPullTimeoutMs(body.timeout_seconds),
    }),

  extDataFixSymbol: (id: string) =>
    request<{ status: string; fixed_files: number }>(
      `/api/ext-data/${id}/fix-symbol`,
      { method: 'POST' },
    ),

  // ===== 个股分析 =====
  stockAnalysisLevels: (symbol: string, days = 120) =>
    request<StockLevels>(`/api/stock-analysis/levels?symbol=${encodeURIComponent(symbol)}&days=${days}`),

  stockAnalysisReportsList: () =>
    request<{ reports: AiStockReport[] }>('/api/stock-analysis/reports'),

  stockAnalysisReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    summary?: string; close?: number | null
    levels?: Record<LevelType, PriceLevel[]>
  }) =>
    request<{ ok: boolean; report: AiStockReport }>('/api/stock-analysis/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  stockAnalysisReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/stock-analysis/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 个股四维分析 — 流式调用(NDJSON,与财务分析同协议)。
   * meta 里额外带 levels(关键价位)供图表回放。
   */
  async *stockAnalyzeStream(symbol: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done' | 'ping'
    symbol?: string
    summary?: string
    levels?: Record<LevelType, PriceLevel[]>
    close?: number | null
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/stock-analysis/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== Strategy Engine =====
  strategyList: (assetType?: 'stock' | 'etf', timeframe: '1d' | '1m' | 'all' = '1d', includeResearch = false) => {
    const params = new URLSearchParams()
    if (assetType) params.set('asset_type', assetType)
    if (timeframe && timeframe !== 'all') params.set('timeframe', timeframe)
    if (includeResearch) params.set('include_research', 'true')
    const qs = params.toString()
    return request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies${qs ? `?${qs}` : ''}`,
    )
  },

  strategyGet: (id: string) =>
    request<StrategyDetail>(`/api/strategies/${id}`),

  /** 发布 research_only 的 AI 草稿策略(翻转为公开) */
  strategyPublish: (strategyId: string) =>
    request<{ ok: boolean; strategy_id: string }>(`/api/strategies/${encodeURIComponent(strategyId)}/publish`, { method: 'POST' }),

  strategySaveConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyPatchConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'PATCH',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyResetConfig: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/config/${strategyId}`, { method: 'DELETE' }),

  /** 删除自定义策略（内置策略不可删除） */
  strategyDelete: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/${strategyId}`, { method: 'DELETE' }),

  strategyReload: () =>
    request<{ ok: boolean; count: number }>('/api/strategies/reload', { method: 'POST' }),

  // ===== Custom Signals (自定义信号) =====
  customSignalsList: () =>
    request<{ signals: CustomSignal[] }>('/api/custom-signals'),

  customSignalsOptions: () =>
    request<CustomSignalOptions>('/api/custom-signals/options'),

  customSignalSave: (signal: CustomSignal) =>
    request<{ ok: boolean; signal: CustomSignal }>('/api/custom-signals', {
      method: 'POST',
      body: JSON.stringify(signal),
    }),

  customSignalDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/custom-signals/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  customSignalsAiGenerate: (description: string) =>
    request<CustomSignalAIGenerateResult>('/api/custom-signals/ai/generate', {
      method: 'POST',
      body: JSON.stringify({ description }),
    }),

  // ===== Monitor Rules (监控规则) =====
  monitorRulesList: () =>
    request<{ rules: MonitorRule[] }>('/api/monitor-rules'),

  monitorRuleOptions: () =>
    request<MonitorRuleOptions>('/api/monitor-rules/options'),

  monitorRuleSave: (rule: MonitorRule) =>
    request<{ ok: boolean; rule: MonitorRule }>('/api/monitor-rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),

  monitorRuleDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/monitor-rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ===== Lots (批次登记, 页面名"持仓提醒"; 保存/删除自动同步监控规则) =====
  lotsList: () =>
    request<{ lots: Lot[] }>('/api/lots'),

  lotSave: (lot: Lot) =>
    request<{ ok: boolean; lot: Lot }>('/api/lots', {
      method: 'POST',
      body: JSON.stringify(lot),
    }),

  lotDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/lots/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 生成演示监控规则 (Dev 页用) */
  monitorRuleSeed: () =>
    request<{ ok: boolean; generated: number }>('/api/monitor-rules/seed', { method: 'POST' }),

  // ===== Alerts (触发记录) =====
  alertsList: (params?: { days?: number; limit?: number; source?: string; type?: string; extColumns?: string }) => {
    const qs = new URLSearchParams()
    if (params?.days) qs.set('days', String(params.days))
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.source) qs.set('source', params.source)
    if (params?.type) qs.set('type', params.type)
    if (params?.extColumns) qs.set('ext_columns', params.extColumns)
    const s = qs.toString()
    return request<{ alerts: AlertEvent[]; total: number }>(`/api/alerts${s ? `?${s}` : ''}`)
  },

  alertsClear: () =>
    request<{ ok: boolean; cleared: number }>('/api/alerts', { method: 'DELETE' }),

  alertDelete: (ev: { ts: number; symbol?: string; rule_id?: string }) => {
    const qs = new URLSearchParams()
    if (ev.symbol) qs.set('symbol', ev.symbol)
    if (ev.rule_id) qs.set('rule_id', ev.rule_id)
    const q = qs.toString()
    return request<{ ok: boolean }>(`/api/alerts/${ev.ts}${q ? `?${q}` : ''}`, { method: 'DELETE' })
  },

  /** 生成演示触发记录 (Dev 页用) */
  alertSeed: (count = 12, recent = true) =>
    request<{ ok: boolean; generated: number }>(`/api/alerts/seed?count=${count}&recent=${recent}`, { method: 'POST' }),

  /** 检查 AI 配置状态 */
  strategyAiStatus: () =>
    request<{ configured: boolean; has_key: boolean; has_model: boolean; provider?: string }>('/api/strategies/ai/status'),

  /** 测试 AI 连通性 */
  strategyAiTest: () =>
    request<{ ok: boolean; error?: string; model?: string; response?: string; usage?: { prompt: number; completion: number } }>(
      '/api/strategies/ai/test',
      { method: 'POST' },
    ),

  /** 获取策略源文件内容 */
  strategyGetSource: (id: string) =>
    request<{ code: string; source: string }>(`/api/strategies/${id}/source`),

  async *strategyBuildStream(step: number, payload: Record<string, any>): AsyncGenerator<StrategyBuildStreamEvent> {
    const res = await fetch('/api/strategies/build/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step, ...payload }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  strategyValidateCode: (payload: { code: string; strategy_id?: string; name?: string; description?: string }) =>
    request<StrategyBuildResult>('/api/strategies/code/validate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategySaveCodeV2: (payload: {
    strategy_id: string
    code: string
    target_source: 'ai' | 'custom'
    mode: 'create' | 'update'
    name?: string
    description?: string
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/code/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 创建/更新叠加策略(composite): 声明式引用多个子策略 */
  strategySaveComposite: (payload: {
    strategy_id: string
    name: string
    description?: string
    children: { strategy_id: string; weight: number }[]
    merge_mode: 'union' | 'intersect'
    min_confirm?: number
    mode: 'create' | 'update'
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/composite/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** AI 迭代: 生成 v1 → 跑回测 → 诊断 → 修改 的有界闭环, 草稿已落盘 data/strategies/ai/ */
  strategyAiIterate: (payload: {
    name?: string
    description?: string
    direction?: string
    rules?: string
    execution_backend?: 'polars_expr' | 'matrix_native'
    max_rounds?: number
  }) =>
    request<AiIterateResult>('/api/strategies/ai/iterate', {
      method: 'POST',
      timeoutMs: null,
      body: JSON.stringify(payload),
    }),
}

// ===== Pipeline =====
export interface PipelineJob {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  stage: string
  progress: number          // 0-100 整体进度
  stage_pct: number         // 0-100 当前阶段内进度
  log: { ts: string; stage: string; msg: string }[]
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  result: {
    universe_size: number
    daily_days: number
    adj_factor_symbols: number
    enriched_days: number
    index_count?: number
    index_daily_rows?: number
    minute_rows: number
    skipped_stages?: string[]
  } | null
  error: string | null
}

export type PipelineJobSummary = Omit<PipelineJob, 'log'>

// ===== Data status =====
interface TableStats {
  rows: number
  earliest_date: string | null
  latest_date: string | null
  symbols_covered: number
  trading_days: number
}

interface InstrumentsStats {
  rows: number
  symbols_covered: number
  latest_as_of: string | null
  named: number
}

export interface DataStatus {
  daily: TableStats | null
  enriched: TableStats | null
  index_daily: TableStats | null
  index_enriched: TableStats | null
  index_instruments: InstrumentsStats | null
  etf_daily: TableStats | null
  etf_enriched: TableStats | null
  etf_instruments: InstrumentsStats | null
  minute: TableStats | null
  adj_factor: TableStats | null
  instruments: InstrumentsStats | null
  financials: { rows: number; tables: Record<string, { rows: number; symbols: number }> } | null
  storage: {
    daily_files: number
    daily_size_mb: number
    enriched_files: number
    enriched_size_mb: number
    index_daily_files?: number
    index_daily_size_mb?: number
    index_enriched_files?: number
    index_enriched_size_mb?: number
    index_instruments_files?: number
    index_instruments_size_mb?: number
    etf_daily_files?: number
    etf_daily_size_mb?: number
    etf_enriched_files?: number
    etf_enriched_size_mb?: number
    etf_instruments_files?: number
    etf_instruments_size_mb?: number
    etf_adj_factor_files?: number
    etf_adj_factor_size_mb?: number
    minute_files: number
    minute_size_mb: number
    adj_factor_files: number
    adj_factor_size_mb: number
    instruments_files: number
    instruments_size_mb: number
    financials_files?: number
    financials_size_mb?: number
    ext_data_files?: number
    ext_data_size_mb?: number
    total_size_mb: number
  }
  next_pipeline_run: string | null
  next_instruments_run: string | null
  last_pipeline_run: string | null
  last_instruments_run: string | null
  checked_at: string
  indicators_ready?: boolean
}

export interface EnrichedField {
  name: string
  type: string
  desc: string
}

// ===== 扩展数据 =====
export interface ExtDataField {
  name: string
  dtype: string
  label: string
}

/** 拉取接口鉴权方式; Key 本体存 secrets_store, 不出现在配置里 */
export interface ExtPullAuth {
  type: 'none' | 'bearer' | 'header' | 'query'
  header?: string
  param?: string
}

export interface PullConfig {
  url: string
  method: string
  headers?: Record<string, string>
  body?: string | null
  response_path: string
  field_map?: Record<string, string>
  schedule_minutes: number
  enabled: boolean
  last_run?: string | null
  last_status?: string | null
  last_message?: string | null
  last_rows?: number | null
  next_run?: string | null
  time_window_start?: string | null
  time_window_end?: string | null
  /** 接口按日查询的参数名 (如 "date"): 配置后支持历史回补 */
  date_param?: string | null
  /** 日期参数值的格式: iso=YYYY-MM-DD / compact=YYYYMMDD / ts_s=unix秒 / ts_ms=unix毫秒 (该交易日美东 ET 00:00) */
  date_format?: string
  /** 日内序列表时间列名 (如 "ts"): 配置后同 symbol 允许多行 (按 symbol+时间列去重), 用于集合竞价等多盘数据 */
  time_field?: string | null
  auth?: ExtPullAuth | null
  /** 单次拉取请求超时 (秒), 默认 30 */
  timeout_seconds?: number
}

export interface ExtDataBackfillResult {
  status: string
  total_days: number
  fetched: number
  skipped_existing: number
  empty: number
  failed: { date: string; reason: string }[]
  rows_written: number
}

export interface ExtDataDetectUrlRequest {
  url: string
  method?: string
  headers?: Record<string, string>
  body?: string
  response_path?: string
  field_map?: Record<string, string>
  /** 探测超时 (秒), 默认 30 */
  timeout_seconds?: number
}

export interface ExtDataDetectUrlResult {
  status: string
  total_rows: number
  response_path: string
  response_path_candidates: string[]
  fields: ExtDataField[]
  symbol_candidates: string[]
  code_candidates: string[]
  preview: Record<string, unknown>[]
}

export interface ExtDataConfig {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  fields: ExtDataField[]
  description?: string
  symbol_map?: Record<string, string>
  code_map?: Record<string, string>
  created_at: string
  updated_at: string
  latest_sync_date?: string | null
  date_range?: string[] | null
  pull?: PullConfig | null
}

export interface ExtDataRowsResult {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  date: string | null
  total: number
  limit: number
  fields: ExtDataField[]
  rows: Record<string, any>[]
}

// ===== QVeris 付费数据 (期权 / 财报预期 / 电话会; 约 1 积分/次, 缓存命中免费) =====
export interface QverisUsage {
  configured: boolean
  day: string
  spent_today: number | null
  error?: string | null
  daily_limit: number
  by_kind: Record<string, { calls: number; credits: number; failed: number }>
  balance: { remaining: number | null; error: string | null; checked_at?: number }
  recent: { at: string; kind: string; symbol: string; trigger: string; ok: boolean; charged: number; error?: string }[]
  note?: string
}
export interface OptionTermRow { expiration: string; dte: number; atm_strike: number; atm_iv: number | null; straddle: number | null; expected_move_pct: number | null }
export interface OptionPcr { volume: number | null; open_interest: number | null; put_volume: number; call_volume: number; put_oi: number; call_oi: number }
export interface OptionsAnalysis {
  ok: boolean
  reason?: string
  spot?: number
  as_of?: string | null
  contracts?: number
  expirations?: number
  term?: OptionTermRow[]
  pcr?: { all: OptionPcr; front: OptionPcr; front_expiration: string | null }
  skew?: { expiration: string; put25_iv: number; call25_iv: number; put25_strike: number; call25_strike: number; risk_reversal: number } | null
  oi_walls?: { calls_above: { strike: number; open_interest: number }[]; puts_below: { strike: number; open_interest: number }[]; expirations: string[] }
  unusual?: { contract: string; expiration: string; strike: number; type: string; volume: number; open_interest: number; ratio: number; iv: number | null }[]
  earnings_move?: { earnings_date: string; expiration: string; expected_move_pct: number | null; atm_iv: number | null } | null
  heatmap?: OptionsHeatmap | null
}
export interface QverisPaidMeta { symbol: string; available: boolean; reason?: string; fetched_at?: string; cached?: boolean; charged?: number; fresh?: boolean; cost: number; note?: string }
export interface QverisOptions extends QverisPaidMeta { spot?: number | null; spot_source?: string; analysis?: OptionsAnalysis; earnings_date?: string | null; source?: 'alpaca' | 'qveris' }
export interface EstimateRow {
  date: string; horizon: string
  eps_avg: number | null; eps_high: number | null; eps_low: number | null; eps_analysts: number | null
  eps_avg_7d_ago: number | null; eps_avg_30d_ago: number | null
  eps_up_7d: number | null; eps_down_7d: number | null; eps_up_30d: number | null; eps_down_30d: number | null
  rev_avg: number | null; rev_high: number | null; rev_low: number | null; rev_analysts: number | null
  eps_change_30d_pct?: number | null
}
export interface QverisEstimates extends QverisPaidMeta { estimates?: { next: EstimateRow | null; quarters: EstimateRow[]; years: EstimateRow[]; rows: number }; earnings_date?: string | null }
export interface QverisTranscript extends QverisPaidMeta {
  quarter: string
  segments?: { speaker: string; title: string; content: string; sentiment: number | null }[]
  avg_sentiment?: number | null
  digest?: { points: string[] } | null
}

// ===== AI 按任务选模型 =====
/** 任务思考档位: '' = 按任务默认; default = 模型默认 (不指定档位); off = 关; 其余按档位发 reasoning_effort */
export type AiThinking = 'default' | 'off' | 'low' | 'medium' | 'high' | 'xhigh'
export interface AiTaskRow {
  task: string
  label: string
  default_thinking: 'default' | 'off'
  override: { model: string; base_url: string; thinking: string; has_api_key: boolean; api_key_masked: string }
  /** reasoning_effort = 首次请求实际发送的档位 ('' = 不指定); route = api / codex_cli (旧后端无这两个字段) */
  effective: { model: string; base_url: string; thinking: AiThinking; reasoning_effort?: string; route?: 'api' | 'codex_cli'; overridden: string[] }
}

// ===== 个股新闻 (2026-10-02 产品决策; 中文优先视图, 方向/重要度为 AI 判读) =====
export type NewsDirection = 'bullish' | 'bearish' | 'neutral'
export type NewsImportance = 'high' | 'medium' | 'low'
export type NewsRelevance = 'primary' | 'secondary' | 'passing'

export interface NewsItem {
  id: string
  symbol: string
  published_at: string | null
  age_hours: number | null
  headline: string
  summary: string
  original_headline: string
  translated: boolean
  tagged: boolean
  tag_mode: 'llm' | 'rule' | 'fallback' | null
  relevance: NewsRelevance | null
  event_type: string | null
  event_label: string | null
  direction: NewsDirection | null
  importance: NewsImportance | null
  rehash: boolean
  mention: 'headline' | 'summary' | null
  scope: 'focused' | 'broad' | null
  sources: string[]
  url: string
  other_symbols: string[]
}

// ===== 0.40.0 盘面情报面板 (GET /api/intel/today, 只读; 只展示不参与下单) =====
export type IntelImpact = 'high' | 'medium' | 'low'
export type IntelImpactLevel = 'high' | 'high_medium' | 'all'
export interface IntelQuote {
  symbol: string; name: string; price?: number; chg_pct?: number; d5?: number | null
  as_of?: string | null; day?: string | null; live?: boolean; base?: string
  /** 0.42.4: 现价所在时段 (pre 盘前 / regular 盘中 / post 盘后) 与美东成交时刻 HH:MM */
  session?: 'pre' | 'regular' | 'post' | null; time_et?: string | null
}
export interface IntelTape {
  indexes: IntelQuote[]; sectors: IntelQuote[]; cross: IntelQuote[]
  fetched_at?: string | null; age_min?: number | null; source?: string; daily_day?: string | null
}
export interface IntelVolPoint { value: number; prev_close: number | null; chg_pct: number | null; as_of: string | null; chg_basis?: string }
export interface IntelVol { vix: IntelVolPoint; vix3m?: IntelVolPoint; vix9d?: IntelVolPoint; ratio?: number; source?: string; fetched_at?: string; age_min?: number | null }
export interface IntelRates { us10y?: number; us10y_chg_bp?: number; as_of?: string; hy_oas?: number; hy_chg_bp?: number; hy_as_of?: string; source?: string }
export interface IntelPcr {
  day: string; total?: number; equity?: number; index?: number; spx?: number; etp?: number
  equity_avg20: number | null; n_hist: number; series: { day: string; equity?: number; total?: number }[]; source?: string
}
export interface IntelBreadthDay {
  day: string; universe: number; adv: number; dec: number; up_pct: number | null
  above_ma20_pct: number | null; above_ma50_pct: number | null; above_ma200_pct: number | null; new_highs: number; new_lows: number
}
export interface IntelBreadth extends IntelBreadthDay {
  as_of: string; series: IntelBreadthDay[]; rule?: string; computed_at?: string
  live?: { day: string; n: number; adv: number; dec: number; up_pct: number; source: string } | null
}
export interface IntelEvent {
  kind: 'macro' | 'earnings' | 'opex'; at: string; day: string; time: string; title: string; title_zh?: string | null
  impact?: IntelImpact; forecast?: string | null; previous?: string | null; actual?: string | null; actual_source?: string | null
  surprise?: 'above' | 'below' | 'inline' | 'near' | null; actual_status?: 'ok' | 'pending' | 'unmapped' | 'mismatch' | null; past: boolean
  actual_derived?: boolean
  symbol?: string; role?: string | null; timing?: string
}
export interface IntelCalendar {
  events: IntelEvent[]; days: string[]; today: string; level: IntelImpactLevel; fetched_at?: string | null; error?: string | null
  source?: string; is_trading_day: boolean; session_close: string
}
export interface IntelEarningsReport { date: string; timing: string; eps: number | null; forecast: number | null; surprise_pct: number | null; react_day: string | null; move_pct: number | null }
export interface IntelEarningsRow {
  symbol: string; role: string | null; date: string; timing: string; days: number
  date_conflict?: string | null; eps_consensus?: number | null; eps_source?: 'QVeris' | 'Finnhub' | 'Nasdaq' | null; eps_nasdaq?: number | null
  eps_forecast?: number | null; n_est?: number | null; last_year_eps?: number | null; fiscal_quarter?: string | null
  qveris?: { eps_avg: number | null; eps_change_30d_pct: number | null; rev_avg: number | null; fetched_at?: string; eps_up_30d?: number | null; eps_down_30d?: number | null } | null
  hist?: { n: number; median_abs_move: number | null; max_abs_move: number | null; beat_n: number; surprise_n: number } | null
  last?: IntelEarningsReport | null; reports?: IntelEarningsReport[]
  implied?: { expected_move_pct: number; atm_iv: number | null; expiration: string; spot: number; spot_basis?: 'parity' | 'quote'; as_of?: string; source?: string } | null
}
export interface IntelNewsItem extends NewsItem { role: string | null; macro?: boolean }
export interface IntelPayload {
  contract: string; generated_at: string
  phase: { phase: string; label: string; at: string } | null
  settings: { macro_impact: IntelImpactLevel }
  mood: MarketMoodPayload | null; tape: IntelTape | null; vol: IntelVol | null; rates: IntelRates | null
  pcr: IntelPcr | null; breadth: IntelBreadth | null; calendar: IntelCalendar | null
  earnings: { rows: IntelEarningsRow[]; fetched_at?: string | null; hist_at?: string | null; source?: string } | null
  actions: { symbol: string; role: string; type: string; date: string; rate?: number | null; special?: boolean | null }[] | null
  news: {
    items: IntelNewsItem[]; total: number; tone: Record<string, { bullish: number; bearish: number }>; hours: number; disclaimer: string
    /** 0.42.4: 各筛选的总条数 / 每个筛选最多显示条数 (items = 各筛选最新 limit 条的并集) */
    counts?: Partial<Record<'all' | 'hold' | 'bullish' | 'bearish' | 'macro', number>>; limit?: number
  } | null
  watch_count: number
  state: Record<string, { last_attempt_at?: string; last_ok_at?: string; error?: string | null }>
  errors: Record<string, string>
}

export interface NewsTone {
  bearish: number
  bullish: number
  bearish_heads: string[]
  bullish_heads: string[]
}

export interface NewsSummary {
  count_24h: number
  high_bearish_48h: boolean
  high_bullish_48h: boolean
  top: NewsItem[]
  /** 0.16.0: 48h 内重要度高/中的主角新闻按利空/利多计数 (旧后端没有此字段) */
  tone?: NewsTone
}

export interface NewsStatus {
  last_run_at?: string
  universe?: number
  sources?: string[]
  ingest?: { raw: number; kept: number; added: number }
  ingest_errors?: string[]
  translate?: { llm: number; fallback: number; failed_batches: number; pending_after: number }
  pushed?: number
  last_error?: string
  last_error_at?: string
  events_24h: number
  translated_24h: number
  tagged_24h: number
}

export interface DimensionMembersResult {
  id: string
  label: string
  date: string | null
  field: string
  value: string
  total: number
  limit: number
  rows: Record<string, any>[]
}

export interface DimensionIntradayPoint {
  time: string
  sector: number | null
  market: number | null
}

export interface DimensionIntradayResult {
  status: 'ok' | 'no_data' | 'empty'
  reason?: string | null
  date?: string | null
  basis?: 'prev_close' | 'first_close' | 'mixed' | null
  member_count?: number
  members_with_minute?: number
  points: DimensionIntradayPoint[]
}

export interface AnalysisColumn {
  field: string
  label?: string
  type?: 'string' | 'number' | 'percent' | 'amount' | 'date'
  width?: number | null
  sortable?: boolean
  precision?: number | null
  format?: string | null
  aggregate?: 'count' | 'avg' | 'sum' | 'min' | 'max' | null
  visible?: boolean
}

export interface AnalysisMenu {
  id: string
  label: string
  icon: string
  data_source: string
  template: 'dimension_rank' | 'ranking' | 'table'
  dimension_field?: string | null
  rank_field?: string | null
  group_columns: AnalysisColumn[]
  detail_columns: AnalysisColumn[]
  default_sort?: { field: string; order: 'asc' | 'desc' } | null
  visible: boolean
  order: number
  created_at?: string | null
  updated_at?: string | null
  builtin?: boolean
}

export interface OptionsHeatCell { call_oi: number; put_oi: number; call_vol: number; put_vol: number; bias: number; oi: number }
export interface OptionsHeatmap {
  expirations: string[]
  rows: { strike: number; cells: (OptionsHeatCell | null)[] }[]
  spot: number
  call_share: number
  stance: '偏多' | '偏空' | '多空均衡'
  resistance: number | null
  support: number | null
  call_focus: [number, number] | null
}
