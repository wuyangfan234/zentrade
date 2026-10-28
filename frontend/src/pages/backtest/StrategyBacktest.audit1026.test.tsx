// @vitest-environment jsdom
// audit1026 b3c L-20: 仓位/股数列不重复 / 全量模式收益指数刻度 / 候选载入复测换策略时保留参数与风控
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/WarmupBadge', () => ({ WarmupBadge: () => null }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: () => <input /> }))
vi.mock('@/pages/backtest/charts/ReturnDistributionChart', () => ({ ReturnDistributionChart: () => null }))
const captured: any[] = []
vi.mock('@/pages/backtest/charts/useECharts', () => ({
  useECharts: (option: any) => { if (option) captured.push(option); return { current: null } },
}))
const detail = (id: string) => ({
  id, name: `策略${id}`, description: '', tags: [], source: 'custom', execution_backend: 'polars_expr',
  asset_types: ['stock'], timeframes: ['1d'], version: '1', basic_filter: {},
  params: [{ id: 'period', label: '周期', type: 'int', default: 10, min: 1, max: 100 }],
  params_defaults: { period: 10 }, scoring: {}, scoring_directions: {},
  entry_signals: ['signal_a'], exit_signals: ['signal_b'], minute_exit_trigger_supported_signals: [],
  stop_loss: -0.05, take_profit: null, trailing_stop: null,
  trailing_take_profit_activate: null, trailing_take_profit_drawdown: null, max_hold_days: 5,
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
import { StrategyNavChart } from './charts/StrategyNavChart'
import { clearBacktest } from '@/lib/backtestTask'
import { startBacktest } from './backtestRequest'

let qc: QueryClient, root: Root, host: HTMLDivElement
let listeners: Record<string, (e: MessageEvent) => void> = {}
const flush = async (n = 8) => { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  captured.length = 0
  mocks.screenerStrategies.mockResolvedValue({ presets: [detail('A'), detail('B')] })
  mocks.strategyGet.mockImplementation(async (id: string) => detail(id))
  mocks.dataStatus.mockResolvedValue({ enriched: { earliest_date: '2020-01-02' }, etf_enriched: { earliest_date: '2020-01-02' } })
  mocks.capabilities.mockResolvedValue({ label: 'x', capabilities: {} })
  mocks.customSignalsList.mockResolvedValue({ signals: [] })
  mocks.factorColumns.mockResolvedValue({ columns: [] })
  mocks.factorLibrary.mockResolvedValue({ factors: [] })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: { cancel: vi.fn().mockResolvedValue(undefined) } }))
  listeners = {}
  vi.stubGlobal('EventSource', class { constructor(_u: string) {} close() {} addEventListener(n: string, f: any) { listeners[n] = f } })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); clearBacktest(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('L-20 仓位/股数列只显示一次股数', async () => {
  mocks.screenerStrategies.mockResolvedValue({ presets: [] })
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  startBacktest({ strategy_id: 's' })
  await flush()
  const result = {
    run_id: 'r', config: { strategy_id: 's', start: '2026-01-02', end: '2026-01-31' },
    stats: { total_return: 0.01, n_trades: 1 }, equity_curve: [{ date: '2026-01-02', value: 1 }], drawdown_curve: [],
    trades: [{ symbol: 'AAPL', entry_date: '2026-01-05', exit_date: '2026-01-09', entry_price: 200, exit_price: 210,
      pnl_pct: 0.05, duration: 4, exit_reason: 'signal', shares: 150, position_pct: 0.03, entry_value: 30000, exit_value: 31500, pnl_amount: 1500 }],
    per_symbol_stats: [], strategy_info: { id: 's', name: 'S', description: '', entry_signals: [], exit_signals: [] }, elapsed_ms: 1,
  }
  await act(async () => { listeners.done?.(new MessageEvent('done', { data: JSON.stringify(result) })) })
  await flush()
  const tab = [...host.querySelectorAll('button')].find(b => b.textContent?.startsWith('交易明细')) as HTMLButtonElement
  await act(async () => { tab.click() })
  await flush()
  const cell = (host.querySelector('tbody tr') as HTMLTableRowElement).querySelectorAll('td')[3]
  expect(cell.textContent).toBe('+3.00%150 股')
})

describe('L-20 全量模式收益指数', () => {
  const equity_curve = [
    { date: '2026-01-05', value: 1.0 }, { date: '2026-01-06', value: 1.0123 },
    { date: '2026-01-07', value: 0.9871 }, { date: '2026-01-08', value: 1.0456 },
  ]
  const base: any = {
    run_id: 'r', config: {}, stats: { mode: 'full', full_kind: 'candidate_execution' },
    equity_curve, drawdown_curve: equity_curve.map(r => ({ date: r.date, value: 0 })), trades: [], per_symbol_stats: [],
  }
  it('无基准: 刻度按指数 2 位小数, 轴名不写美元', async () => {
    await act(async () => { root.render(<StrategyNavChart result={base} />) })
    const opt = captured[captured.length - 1]
    const fmt = opt.yAxis[0].axisLabel.formatter as (v: number) => string
    expect([0.98, 1.0, 1.02].map(fmt)).toEqual(['0.98', '1.00', '1.02'])
    expect(opt.yAxis[0].name).not.toContain('USD')
    const tip = String(opt.tooltip.formatter([{ axisValue: '2026-01-08', seriesName: '净值', value: 1.0456, color: '#000' }]))
    expect(tip).toContain('1.0456')
  })
  it('有 SPY 基准: 净值轴同样按指数刻度', async () => {
    const r = { ...base, run_id: 'r2', benchmark_curve: equity_curve.map((x, i) => ({ date: x.date, value: 600 + i, close: 600 + i, symbol: 'SPY' })) }
    await act(async () => { root.render(<StrategyNavChart result={r} />) })
    const opt = captured[captured.length - 1]
    const nav = opt.series.find((s: any) => s.name === '净值')
    const fmt = opt.yAxis[nav.yAxisIndex].axisLabel.formatter as (v: number) => string
    expect(fmt(1.02)).toBe('1.02')
  })
  it('账户模式仍按美元资金', async () => {
    const r = { ...base, run_id: 'r3', stats: { mode: 'position' }, equity_curve: equity_curve.map(x => ({ ...x, value: x.value * 100000 })) }
    await act(async () => { root.render(<StrategyNavChart result={r} />) })
    const opt = captured[captured.length - 1]
    expect(opt.yAxis[0].axisLabel.formatter(102000)).toBe('10万')
  })
})

const candidate = {
  id: 'c1', kind: 'strategy', name: 'B候选', source_id: 'B', status: 'pending', metrics: {}, data_as_of: null,
  created_at: '', updated_at: '',
  config: {
    strategy_id: 'B', asset_type: 'stock', start: '2025-01-02', end: '2025-06-30',
    params: { period: 33 },
    overrides: { stop_loss: -0.2, max_hold_days: 9, entry_signals: ['signal_a'], exit_signals: ['signal_b'] },
    cost_model: 'us_per_share_v1', commission_per_share: 0.005, commission_min: 1,
  },
}
const savedForm = (strategy: string) => JSON.stringify({ selectedStrategy: strategy, symbols: '', assetType: 'stock', start: '2025-01-02', end: '2025-06-30', matching: 'open_t+1', entryFill: 'open_t+1', exitFill: 'close_t', fees: '0.005', slippage: '5', maxPositions: '10', maxExposure: '100', initialCapital: '1000000', positionSizing: 'equal', mode: 'position', holdingDays: '5', result: null })

async function runAndReadQuery(): Promise<URLSearchParams> {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('运行回测')) as HTMLButtonElement
  await act(async () => { btn.click() })
  await flush(3)
  return new URLSearchParams(localStorage.getItem('backtest_reconnect') ?? '')
}

it('L-20 候选载入复测换策略: 参数与风控按候选值进入请求', async () => {
  localStorage.setItem('strategy-backtest-last', savedForm('A'))
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest loadCandidate={candidate as any} onLoadConsumed={() => {}} /></QueryClientProvider>) })
  await flush()
  const qs = await runAndReadQuery()
  expect(qs.get('strategy_id')).toBe('B')
  expect(JSON.parse(qs.get('params') ?? '{}').period).toBe(33)
  const ov = JSON.parse(qs.get('overrides') ?? '{}')
  expect(ov.stop_loss).toBe(-0.2)
  expect(ov.max_hold_days).toBe(9)
})

it('L-20 候选载入复测跨页首挂载: 参数与风控按候选值进入请求', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest loadCandidate={candidate as any} onLoadConsumed={() => {}} /></QueryClientProvider>) })
  await flush()
  const qs = await runAndReadQuery()
  expect(qs.get('strategy_id')).toBe('B')
  expect(JSON.parse(qs.get('params') ?? '{}').period).toBe(33)
  expect(JSON.parse(qs.get('overrides') ?? '{}').stop_loss).toBe(-0.2)
})

it('L-20 候选就是当前策略: 参数与风控按候选值进入请求', async () => {
  localStorage.setItem('strategy-backtest-last', savedForm('B'))
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>) })
  await flush()
  await act(async () => { root.render(<QueryClientProvider client={qc}><StrategyBacktest loadCandidate={candidate as any} onLoadConsumed={() => {}} /></QueryClientProvider>) })
  await flush()
  const qs = await runAndReadQuery()
  expect(JSON.parse(qs.get('params') ?? '{}').period).toBe(33)
  expect(JSON.parse(qs.get('overrides') ?? '{}').max_hold_days).toBe(9)
})
