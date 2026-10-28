/**
 * 日K查询配置 — klineDaily 的唯一权威 options。
 *
 * StockDailyKChart(图表) 与 StockPanel(信息条) 各自 useQuery 共享同一 cache key,
 * React Query 按 key 去重只发一次请求; 邻近预取 prefetchQuery 也复用本配置, 三处不会漂移。
 *
 * placeholderData 内置"仅同 symbol 占位"守卫: 改日期范围/扩展字段时旧数据可暂显(不闪),
 * 切股时不透传上一只股票的数据(不误显示)。
 */
import { api, type KlineDailyLatestResponse, type KlineDailyResponse } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 分时 tab 多日分时默认周期 (StockPanel 预取与弹窗存储回退共用, 避免魔数两处漂移) */
export const DEFAULT_INTRADAY_DAYS = 10

export function klineDailyQueryOptions(
  symbol: string,
  dateRange: { start: string; end: string },
  extColumns?: string,
) {
  return {
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, extColumns),
    queryFn: () => api.klineDaily(symbol, undefined, dateRange, extColumns),
    // 工厂无 TData 泛型, 参数用 any 以便 useQuery/prefetchQuery 共用
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}

export function klineDailyLatestQueryOptions(symbol: string) {
  return {
    queryKey: QK.klineLatest(symbol),
    queryFn: () => api.klineDailyLatest(symbol),
  }
}

export function mergeLatestKlineRow(
  current: KlineDailyResponse | undefined,
  latest: KlineDailyLatestResponse,
): KlineDailyResponse | undefined {
  if (!current || !latest.row || current.symbol !== latest.symbol) return current

  const latestDate = String(latest.row.date).slice(0, 10)
  const last = current.rows.at(-1)
  if (!last) return { ...current, rows: [{ ...latest.row, date: latestDate }] }

  const lastDate = String(last.date).slice(0, 10)
  if (latestDate < lastDate) return current
  if (latestDate === lastDate) {
    return {
      ...current,
      rows: [...current.rows.slice(0, -1), { ...last, ...latest.row, date: latestDate }],
    }
  }
  return { ...current, rows: [...current.rows, { ...latest.row, date: latestDate }] }
}

/**
 * 单日分时查询配置 — 与 klineDailyQueryOptions 同风格的单源 options (date 为空 = 最新日内)。
 *
 * live 请求后端刷新官方分钟K, 盘中再拼接实时源 (TickFlow 5 秒报价) 的最近分钟。
 * 历史日期后端自行忽略 live, 故多日图与预取恒传 true 亦不影响历史读取。
 */
export function klineMinuteQueryOptions(symbol: string, date?: string, live?: boolean) {
  return {
    queryKey: QK.klineMinute(symbol, date ?? ''),
    queryFn: () => api.klineMinute(symbol, date ?? undefined, live),
  }
}

/** 常规时段的最后一分钟，16:00 是空边界，不是成交分钟。 */
const CLOSE_MINUTE = '15:59'

const MARKET_OPEN = '09:30'
const MARKET_CLOSE = '16:00'
const WAIT_FOR_MINUTES_MS = 60_000
const LATEST_SESSION_IDLE_MS = 300_000

/** 美东今日 YYYY-MM-DD；分钟响应 date 与后端 us_clock.today() 同口径。 */
function marketToday(now: Date): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(now)
}

/** 美东此刻 'HH:MM'。 */
function marketNowHHMM(now: Date): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false }).format(now)
}

/** 行 datetime ('YYYY-MM-DD HH:MM:SS' 美东墙钟契约) → 'HH:MM'; 非常规格式返回 ''。 */
function rowMinute(r: { datetime?: string } | undefined): string {
  const s = String(r?.datetime ?? '')
  return s.length >= 16 ? s.slice(11, 16) : ''
}

/**
 * 停表依据请求日期（QK.klineMinute），不能把响应回退日误当成用户选择的历史日。
 * - 明确选择历史日可停；source=none、盘前空数据、昨天的缓存均以至少60秒继续等。
 * - 16:00 后缺收盘根仍以至少60秒补拉，不因 SIP 延迟或暂时失败永久停表。
 * - 当日收盘根到齐，明确日期可停；“最新”保持低频探测，跨日无需重挂组件。
 * - 没有交易日历元数据时不猜休市/半日市；保留低频等待，由后端决定可用数据。
 */
export function minuteRefetchInterval(ms?: number) {
  return (query: any): number | false => {
    if (ms == null || !Number.isFinite(ms) || ms <= 0) return false
    const now = new Date()
    const today = marketToday(now)
    const requestedDate = query.queryKey?.[0] === 'kline-minute' ? query.queryKey[2] : undefined
    const explicitDate = typeof requestedDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(requestedDate)
      ? requestedDate : undefined
    if (explicitDate && explicitDate < today) return false

    const waitInterval = Math.max(ms, WAIT_FOR_MINUTES_MS)
    const d = query.state.data
    if (!d || d.source === 'none' || d.date !== today || !d.rows?.length) return waitInterval

    const time = marketNowHHMM(now)
    if (time < MARKET_OPEN) return waitInterval
    if (time >= MARKET_CLOSE) {
      const closeBarArrived = d.rows.some((r: { datetime?: string }) => (
        r.datetime?.slice(0, 10) === today && rowMinute(r) === CLOSE_MINUTE
      ))
      if (!closeBarArrived) return waitInterval
      return explicitDate === today ? false : Math.max(ms, LATEST_SESSION_IDLE_MS)
    }
    return ms
  }
}

/** 多日分时查询配置 — 分时 tab 的 StockMultiDayIntradayChart 与 邻近预取 共用。
 * 内嵌「仅同 symbol 占位」守卫 (key 结构 ['kline-minute-range', symbol, days], index 1 为 symbol),
 * 与 klineDailyQueryOptions 同源, 调用点不再各自手写。 */
export function klineMinuteRangeQueryOptions(symbol: string, days: number) {
  return {
    queryKey: QK.klineMinuteRange(symbol, days),
    queryFn: () => api.klineMinuteRange(symbol, days),
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}
