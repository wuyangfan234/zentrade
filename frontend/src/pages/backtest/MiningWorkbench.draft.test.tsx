// @vitest-environment jsdom
// 审计回归: 挖掘页草稿里残留已删除因子 → 提交被后端 422 (unknown mining factors)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({ toast: () => {} }))
const mocks = vi.hoisted(() => ({
  factorColumns: vi.fn(), strategyList: vi.fn(), miningRuns: vi.fn(), miningAvailability: vi.fn(),
  miningConfig: vi.fn(), preferences: vi.fn(),
}))
vi.mock('@/lib/api', async (orig) => {
  const actual = await orig<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { MiningWorkbench } from './MiningWorkbench'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
const tick = async () => { for (let i = 0; i < 12; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
const DRAFT_KEY = 'mining_workbench_draft_v1'
const store = new Map<string, string>()
beforeEach(() => {
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
    removeItem: (k: string) => { store.delete(k) },
  })
  store.clear()
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  mocks.factorColumns.mockResolvedValue({ columns: [
    { id: 'momentum_5d', label: '5日动量', group: '动量', desc: '' },
    { id: 'momentum_20d', label: '20日动量', group: '动量', desc: '' },
  ] })
  mocks.strategyList.mockResolvedValue({ strategies: [] })
  mocks.miningRuns.mockResolvedValue({ items: [] })
  mocks.miningAvailability.mockResolvedValue({ eligible: true, trading_bars: 500, required_bars: 252 })
  mocks.miningConfig.mockResolvedValue({ mining_schedule_enabled: false, mining_schedule_weekday: 4, mining_budget_profile: 'balanced' })
  mocks.preferences.mockResolvedValue({ realtime_quotes_enabled: false })
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('drops factors from the saved draft that no longer exist', async () => {
  store.set(DRAFT_KEY, JSON.stringify({ factorNames: ['momentum_5d', 'uf_deleted'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><MiningWorkbench /></MemoryRouter></QueryClientProvider>) })
  await tick()
  expect(host.textContent).toContain('1/48')
  expect(JSON.parse(store.get(DRAFT_KEY)!).factorNames).toEqual(['momentum_5d'])
})

it('falls back to the default selection when every saved factor is gone', async () => {
  store.set(DRAFT_KEY, JSON.stringify({ factorNames: ['uf_deleted'] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><MiningWorkbench /></MemoryRouter></QueryClientProvider>) })
  await tick()
  expect(JSON.parse(store.get(DRAFT_KEY)!).factorNames).toEqual(['momentum_5d', 'momentum_20d'])
})
