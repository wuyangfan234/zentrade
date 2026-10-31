// @vitest-environment jsdom
// 0.43.2 回归: 回测结束日默认 = 最近一个已定版的日K (data/status enriched.settled_date), 不是浏览器的“今天”。
// 盘中结束日默认今天 → 读到每 10 秒重写一次的盘中快照, 回测报「指标数据正在发布更新」,
// 年化等统计出不来; 选择器也不能选到还没出日K的日子。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const pickers: { value: string; max?: string }[] = []
vi.mock('@/components/WarmupBadge', () => ({ WarmupBadge: () => null }))
vi.mock('@/components/DatePicker', () => ({
  DatePicker: (p: { value: string; max?: string }) => { pickers.push({ value: p.value, max: p.max }); return <input readOnly value={p.value} /> },
}))
vi.mock('@/pages/backtest/charts/ReturnDistributionChart', () => ({ ReturnDistributionChart: () => null }))
vi.mock('./charts/FactorICChart', () => ({ FactorICChart: () => null }))
vi.mock('./charts/FactorGroupNavChart', () => ({ FactorGroupNavChart: () => null }))
const mocks = vi.hoisted(() => ({
  screenerStrategies: vi.fn(), strategyGet: vi.fn(), dataStatus: vi.fn(), capabilities: vi.fn(),
  customSignalsList: vi.fn(), factorColumns: vi.fn(), factorLibrary: vi.fn(), factorRun: vi.fn(), watchlistList: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { StrategyBacktest } from './StrategyBacktest'
import { FactorBacktest } from './FactorBacktest'
import { clearBacktest } from '@/lib/backtestTask'

const SETTLED = '2026-10-29'
let qc: QueryClient, root: Root, host: HTMLDivElement
const flush = async (n = 8) => { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-10-30T17:40:00Z'))          // 美东 10-30 13:40 盘中, 当天日K未出
  localStorage.clear()
  pickers.length = 0
  mocks.screenerStrategies.mockResolvedValue({ presets: [] })
  mocks.dataStatus.mockResolvedValue({
    enriched: { earliest_date: '2021-01-04', latest_date: '2026-10-30', settled_date: SETTLED },
    etf_enriched: { earliest_date: '2021-01-04', latest_date: '2026-10-30', settled_date: SETTLED },
  })
  mocks.capabilities.mockResolvedValue({ label: 'x', capabilities: {} })
  mocks.customSignalsList.mockResolvedValue({ signals: [] })
  mocks.factorColumns.mockResolvedValue({ columns: [{ id: 'momentum_20d', label: '20日涨幅', group: '动量', desc: '' }] })
  mocks.factorLibrary.mockResolvedValue({ factors: [] })
  mocks.factorRun.mockResolvedValue({ error: 'x', config: {} })
  mocks.watchlistList.mockResolvedValue({ symbols: [] })
  vi.stubGlobal('EventSource', class { close() {} addEventListener() {} })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); clearBacktest(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('策略回测: 默认结束日 = 最近定版日K, 结束日选择器不能选到之后', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  const end = pickers.filter(p => p.value).slice(-1)[0]
  expect(end.value).toBe(SETTLED)
  expect(pickers.slice(-2).every(p => p.max === undefined || p.max <= SETTLED)).toBe(true)
})

it('策略回测: 上次保存的结束日是今天 (还没出日K) 也回落到定版日', async () => {
  localStorage.setItem('strategy-backtest-last', JSON.stringify({ selectedStrategy: null, start: '2026-07-30', end: '2026-10-30' }))
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  expect(pickers.slice(-1)[0].value).toBe(SETTLED)
})

it('策略回测: 快捷区间“3个月”结束日也是定版日', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === '3个月') as HTMLButtonElement
  await act(async () => { btn.click() })
  await flush()
  expect(pickers.slice(-1)[0].value).toBe(SETTLED)
  expect(host.textContent).toContain('近 3 个月')                 // 快捷区间仍被识别, 不显示“自定义区间”
})

it('单因子检验: 默认结束日 = 最近定版日K, 并作为请求的 end 发出', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorBacktest /></QueryClientProvider>) })
  await flush()
  expect(pickers.slice(-1)[0].value).toBe(SETTLED)
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent === '开始因子分析') as HTMLButtonElement
  await act(async () => { btn.click() })
  await flush()
  expect(mocks.factorRun.mock.calls[0][0].end).toBe(SETTLED)
})
