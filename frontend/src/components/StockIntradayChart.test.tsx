// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { StockIntradayChart, intradayNotice } from './StockIntradayChart'

const chartProps = vi.hoisted(() => vi.fn())
vi.mock('@/lib/api', () => ({ api: { klineMinute: vi.fn(), syncMinuteSingle: vi.fn() } }))
vi.mock('@/components/EChartsIntraday', () => ({
  EChartsIntraday: (props: unknown) => {
    chartProps(props)
    return <div data-testid="minute-chart">分钟图</div>
  },
}))

const day = '2026-09-30'
const response = {
  symbol: 'AAPL', date: day, source: 'local' as const, data_delay_minutes: 16,
  rows: [{ datetime: `${day} 09:30:00`, open: 100, high: 100, low: 100, close: 100, volume: 10, amount: 1000 }],
}
let cleanup = async () => {}
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-30T13:25:00Z')) // 09:25 ET
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
})
afterEach(async () => {
  await cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.resetAllMocks()
  vi.unstubAllGlobals()
})

async function mount(date = day) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); client.clear() }
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <StockIntradayChart symbol="AAPL" date={date} refetchIntervalMs={6000} />
    </QueryClientProvider>,
  ))
  await advance(1)
  return host
}

it('recovers on the same mount from pre-open source=none to the first bar delayed by 16 minutes', async () => {
  const firstBarAvailable = new Date('2026-09-30T13:46:00Z').getTime()
  vi.mocked(api.klineMinute).mockImplementation(async () => Date.now() >= firstBarAvailable
    ? response
    : { ...response, source: 'none', rows: [] })
  const host = await mount()
  expect(api.klineMinute).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('暂无分钟数据')
  await advance(60_000)
  expect(api.klineMinute).toHaveBeenCalledTimes(2)
  await advance(20 * 60_000)
  expect(host.querySelector('[data-testid="minute-chart"]')).not.toBeNull()
  expect(api.klineMinute).toHaveBeenLastCalledWith('AAPL', day, true)
})

it('catches up the delayed closing tail once a minute and then stops the explicit date', async () => {
  vi.setSystemTime(new Date('2026-09-30T20:00:00Z'))
  const closeAvailable = new Date('2026-09-30T20:16:00Z').getTime()
  vi.mocked(api.klineMinute).mockImplementation(async () => ({
    ...response,
    rows: [{ ...response.rows[0], datetime: `${day} ${Date.now() >= closeAvailable ? '15:59' : '15:44'}:00` }],
  }))
  await mount()
  await advance(60_000)
  expect(api.klineMinute).toHaveBeenCalledTimes(2)
  await advance(15 * 60_000)
  const completedCalls = vi.mocked(api.klineMinute).mock.calls.length
  expect(chartProps.mock.calls.at(-1)![0].data[0].datetime).toBe(`${day} 15:59:00`)
  await advance(5 * 60_000)
  expect(api.klineMinute).toHaveBeenCalledTimes(completedCalls)
})

it('does not label realtime TickFlow minutes as delayed and drops obsolete price-limit metadata', async () => {
  vi.mocked(api.klineMinute).mockResolvedValue({
    ...response, in_session: true, realtime_source: 'tickflow',
    price_limit: { rate: 0.1, limit_up: 110, limit_down: 90, source: 'rule' },
  } as any)
  const host = await mount()
  expect(chartProps.mock.calls.at(-1)![0]).not.toHaveProperty('priceLimit')
  expect(host.textContent).toContain('实时 · TickFlow · 美东时间')
  expect(host.textContent).not.toContain('延迟')
  expect(host.textContent).not.toContain('非实时')
})

it('intradayNotice: delay is only mentioned in session when no realtime source is available', () => {
  expect(intradayNotice({ in_session: true, realtime_source: 'tickflow', data_delay_minutes: 16 })).toBe('实时 · TickFlow · 美东时间')
  expect(intradayNotice({ in_session: true, realtime_tail: { source: 'tickflow' }, data_delay_minutes: 0 })).toBe('实时 · TickFlow · 美东时间')
  expect(intradayNotice({ in_session: true, realtime_source: null, data_delay_minutes: 16 })).toBe('实时源暂不可用，最近约16分钟待补 · 美东时间')
  // 收盘后 / 历史日 = 完整分钟K, 不提延迟
  expect(intradayNotice({ in_session: false, realtime_source: null, data_delay_minutes: 16 })).toBe('美东时间')
  expect(intradayNotice(undefined)).toBe('美东时间')
})

it('does not invent a 16-minute delay for a source that declares zero delay', async () => {
  vi.mocked(api.klineMinute).mockResolvedValue({ ...response, data_delay_minutes: 0 } as any)
  const host = await mount()
  expect(host.textContent).not.toContain('16分钟')
  expect(host.textContent).toContain('美东时间')
})

it('does not schedule polling for an explicitly selected historical date with no data', async () => {
  vi.mocked(api.klineMinute).mockResolvedValue({ ...response, date: '2026-09-29', source: 'none', rows: [] })
  await mount('2026-09-29')
  await advance(21 * 60_000)
  expect(api.klineMinute).toHaveBeenCalledTimes(1)
})
