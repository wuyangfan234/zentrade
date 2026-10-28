// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ factorColumns: vi.fn(), factorRun: vi.fn(), factorBatch: vi.fn(), watchlistList: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('./charts/FactorICChart', () => ({ FactorICChart: () => null }))
vi.mock('./charts/FactorGroupNavChart', () => ({ FactorGroupNavChart: () => null }))
import { FactorBacktest } from './FactorBacktest'
import { FactorDiscovery } from './FactorDiscovery'
import { factorBatchCandidate, factorResultCandidate } from './researchCandidates'
import type { FactorBacktestResult, FactorBatchItem, FactorBatchResult } from '@/lib/api'

let qc: QueryClient
let root: Root
let container: HTMLDivElement
async function flush() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.factorColumns.mockResolvedValue({ columns: [{ id: 'momentum_20d', label: '20日涨幅', group: '动量', desc: '' }] })
  mocks.watchlistList.mockResolvedValue({ symbols: [] })
  mocks.factorRun.mockResolvedValue({ error: 'empty test panel', config: {} })
  mocks.factorBatch.mockResolvedValue({ error: 'empty test panel', config: {} })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  qc.clear()
})

it.each(['single', 'batch'] as const)('%s form exposes only slippage, submits default 5bp and explicit zero fees', async mode => {
  await act(async () => root.render(<QueryClientProvider client={qc}>
    {mode === 'single' ? <FactorBacktest /> : <FactorDiscovery />}
  </QueryClientProvider>))
  await flush()
  expect(container.textContent).not.toMatch(/佣金|万分之/)
  const label = [...container.querySelectorAll('label')].find(el => el.textContent?.includes('滑点'))
  expect(label).toBeTruthy()
  const input = (label!.querySelector('input') ?? label!.parentElement!.querySelector('input')) as HTMLInputElement
  expect(input.value).toBe('5')
  const button = [...container.querySelectorAll('button')].find(el => mode === 'single'
    ? el.textContent === '开始因子分析' : /筛选 1 个因子/.test(el.textContent ?? ''))!
  expect(button.disabled).toBe(false)
  await act(async () => button.click())
  await flush()
  const send = mode === 'single' ? mocks.factorRun : mocks.factorBatch
  expect(send.mock.calls[0][0]).toMatchObject({ fees_pct: 0, slippage_bps: 5 })
  // A zero slippage input must not fall back to 5bp.
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '0')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => button.click())
  await flush()
  expect(send.mock.calls[1][0]).toMatchObject({ fees_pct: 0, slippage_bps: 0 })
})

it.each([0, 0.0002])('candidate helpers retain actual fee=%s and slippage instead of applying new defaults', fees => {
  const config = { factor_name: 'momentum_20d', fees_pct: fees, slippage_bps: 0, end: '2026-01-12' }
  const single = factorResultCandidate({ config } as unknown as FactorBacktestResult, '动量')
  const batch = factorBatchCandidate({ config } as unknown as FactorBatchResult, { factor_name: 'momentum_20d', label: '动量' } as FactorBatchItem)
  for (const candidate of [single, batch]) {
    expect(candidate.config.fees_pct).toBe(fees)
    expect(candidate.config.slippage_bps).toBe(0)
  }
})
