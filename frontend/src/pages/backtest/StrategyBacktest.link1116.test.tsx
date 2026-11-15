// @vitest-environment jsdom
// 11-16 易用性: 挖掘发布弹窗 / 策略页「回测」直达 —— 预选策略并自动跑一次; 算力被占时显示排队在等谁。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/WarmupBadge', () => ({ WarmupBadge: () => null }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: () => <input /> }))
vi.mock('@/pages/backtest/charts/ReturnDistributionChart', () => ({ ReturnDistributionChart: () => null }))
vi.mock('@/pages/backtest/charts/useECharts', () => ({ useECharts: () => ({ current: null }) }))
const detail = (id: string) => ({
  id, name: `策略${id}`, description: '', tags: [], source: 'custom', execution_backend: 'matrix_native',
  asset_types: ['stock'], timeframes: ['1d'], version: '1', basic_filter: { price_min: 5 },
  params: [{ id: 'top_rank', label: '每日最多入选', type: 'int', default: 20, min: 1, max: 100 }],
  params_defaults: { top_rank: 20 }, scoring: {}, scoring_directions: {},
  entry_signals: ['signal_factor_rank_entry'], exit_signals: ['signal_factor_rank_exit'], minute_exit_trigger_supported_signals: [],
  stop_loss: -0.08, take_profit: null, trailing_stop: null,
  trailing_take_profit_activate: null, trailing_take_profit_drawdown: null, max_hold_days: 30,
  order_by: 'score', descending: true, limit: 100,
})
const mocks = vi.hoisted(() => ({
  screenerStrategies: vi.fn(), strategyGet: vi.fn(), dataStatus: vi.fn(), capabilities: vi.fn(),
  customSignalsList: vi.fn(), factorColumns: vi.fn(), factorLibrary: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { StrategyBacktest } from './StrategyBacktest'
import { clearBacktest } from '@/lib/backtestTask'

let qc: QueryClient, root: Root, host: HTMLDivElement
let listeners: Record<string, (e: MessageEvent) => void> = {}
let opened: string[] = []
const flush = async (n = 10) => { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  mocks.screenerStrategies.mockResolvedValue({ presets: [detail('A'), detail('mined_factor_new')] })
  mocks.strategyGet.mockImplementation(async (id: string) => detail(id))
  mocks.dataStatus.mockResolvedValue({ enriched: { earliest_date: '2020-01-02' }, etf_enriched: { earliest_date: '2020-01-02' } })
  mocks.capabilities.mockResolvedValue({ label: 'x', capabilities: {} })
  mocks.customSignalsList.mockResolvedValue({ signals: [] })
  mocks.factorColumns.mockResolvedValue({ columns: [] })
  mocks.factorLibrary.mockResolvedValue({ factors: [] })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: { cancel: vi.fn().mockResolvedValue(undefined) } }))
  listeners = {}
  opened = []
  vi.stubGlobal('EventSource', class {
    constructor(u: string) { opened.push(u) }
    close() {}
    addEventListener(n: string, f: any) { listeners[n] = f }
  })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); clearBacktest(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

const render = (node: React.ReactNode) => act(async () => { root.render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>) })

it('直达链接 autoRun: 预选新策略并自动跑一次 (上次选的是别的策略也一样)', async () => {
  localStorage.setItem('strategy-backtest-last', JSON.stringify({ selectedStrategy: 'A', symbols: 'NVDA,MU', assetType: 'stock', start: '2023-01-03', end: '2026-10-30' }))
  const consumed = vi.fn()
  await render(<StrategyBacktest linkTarget={{ strategyId: 'mined_factor_new', autoRun: true }} onLinkConsumed={consumed} />)
  await flush()

  expect(consumed).toHaveBeenCalledTimes(1)
  expect(opened).toHaveLength(1)
  const query = new URLSearchParams(opened[0].split('?')[1])
  expect(query.get('strategy_id')).toBe('mined_factor_new')
  expect(query.get('symbols')).toBe('NVDA,MU')            // 沿用上次的股票池 / 区间
  expect(query.get('start')).toBe('2023-01-03')

  // 父组件清空 linkTarget 后重渲染不会再跑第二次
  await render(<StrategyBacktest linkTarget={null} onLinkConsumed={consumed} />)
  await flush()
  expect(opened).toHaveLength(1)
})

it('直达链接不带 autoRun: 只预选, 不自动跑', async () => {
  await render(<StrategyBacktest linkTarget={{ strategyId: 'mined_factor_new', autoRun: false }} onLinkConsumed={() => {}} />)
  await flush()
  expect(opened).toHaveLength(0)
  expect(host.textContent).toContain('策略mined_factor_new')
})

it('排队: 显示在等哪个任务, 开始回测后提示消失', async () => {
  await render(<StrategyBacktest linkTarget={{ strategyId: 'mined_factor_new', autoRun: true }} onLinkConsumed={() => {}} />)
  await flush()
  await act(async () => { listeners.queued?.(new MessageEvent('queued', { data: JSON.stringify({ waiting_for: ['回放验收'] }) })) })
  expect(host.querySelector('[data-backtest-status="queued"]')?.textContent).toBe('排队中 · 等待回放验收结束')
  expect(host.textContent).toContain('轮到后自动开始')

  await act(async () => { listeners.progress?.(new MessageEvent('progress', { data: JSON.stringify({ day: 3, total: 10, date: '2024-01-05', equity: 1 }) })) })
  expect(host.querySelector('[data-backtest-status="queued"]')).toBeNull()
  expect(host.textContent).toContain('回测中 · 第 3/10 天')
})
