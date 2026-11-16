/** 机会页的数据钩子 (0.29.0): 一个请求取齐整页; 盘前 / 开盘 / 盘中 30 秒刷新, 其余 60 秒。
 * 0.39.0: 另有 SSE 状态帧 (opp_radar_updated, useQuoteStream 里监听) 在状态变化时立即重取; 轮询保留作兜底。
 * 2026-11-15 B 方案: 价格尺现价另走轻量报价 (useOppQuotes), 跟行情推送 quotes_updated 刷新, 15 秒轮询兜底。 */
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api, type OppPagePayload, type OppQuote, type OppQuotesPayload } from '@/lib/api'
import { OPP_PAGE_QUERY_KEY } from '@/lib/useQuoteStream'

export const OPP_PAGE_KEY = OPP_PAGE_QUERY_KEY
export const ACTIVE_PHASES = new Set(['premarket', 'open_wait', 'session'])
export const QUOTES_MAX = 60

/** live = false (2026-11-15 历史日回看): 停止轮询, 保留已取到的当天数据 */
export function useOppPage(live = true) {
  return useQuery<OppPagePayload>({
    queryKey: OPP_PAGE_KEY,
    queryFn: () => api.oppPage(),
    enabled: live,
    refetchInterval: (q) => (!live ? false : ACTIVE_PHASES.has((q.state.data as OppPagePayload | undefined)?.phase?.phase ?? '') ? 30_000 : 60_000),
    refetchIntervalInBackground: false,
  })
}

/** 票集合 → 稳定的查询键 (去重 · 排序 · 最多 60 只) */
export function quoteKey(symbols: string[]): string {
  return [...new Set(symbols.filter(Boolean))].sort().slice(0, QUOTES_MAX).join(',')
}

/** 价格尺现价: 只在盘前 / 开盘 / 盘中取 (其余时段页面价即收盘价); 取不到的票沿用 /page 的价 */
export function useOppQuotes(symbols: string[], active: boolean): { quotes: Record<string, OppQuote>; at: string | null } {
  const key = quoteKey(symbols)
  const q = useQuery<OppQuotesPayload>({
    queryKey: ['opp-quotes', key],
    queryFn: () => api.oppQuotes(key),
    enabled: active && key.length > 0,
    refetchInterval: active ? 15_000 : false,
    refetchIntervalInBackground: false,
    staleTime: 4_000,
    placeholderData: keepPreviousData,
  })
  return active && q.data ? { quotes: q.data.quotes ?? {}, at: q.data.at ?? null } : { quotes: {}, at: null }
}
