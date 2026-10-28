/** 机会页的数据钩子 (0.29.0): 一个请求取齐整页; 盘前 / 开盘 / 盘中 30 秒刷新, 其余 60 秒。
 * 0.39.0: 另有 SSE 状态帧 (opp_radar_updated, useQuoteStream 里监听) 在状态变化时立即重取; 轮询保留作兜底。 */
import { useQuery } from '@tanstack/react-query'
import { api, type OppPagePayload } from '@/lib/api'
import { OPP_PAGE_QUERY_KEY } from '@/lib/useQuoteStream'

export const OPP_PAGE_KEY = OPP_PAGE_QUERY_KEY
export const ACTIVE_PHASES = new Set(['premarket', 'open_wait', 'session'])

export function useOppPage() {
  return useQuery<OppPagePayload>({
    queryKey: OPP_PAGE_KEY,
    queryFn: () => api.oppPage(),
    refetchInterval: (q) => (ACTIVE_PHASES.has((q.state.data as OppPagePayload | undefined)?.phase?.phase ?? '') ? 30_000 : 60_000),
    refetchIntervalInBackground: false,
  })
}
