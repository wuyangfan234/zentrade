// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { api } from '@/lib/api'
import { StockPreviewDialog } from './StockPreviewDialog'

vi.mock('@/lib/api', () => ({ api: {
  watchlistList: vi.fn().mockResolvedValue({ symbols: [{ symbol: 'NVDA', name: 'NVIDIA' }] }),
  monitorRulesList: vi.fn().mockResolvedValue({ rules: [] }),
  watchlistGroups: vi.fn().mockResolvedValue({ groups: [] }),
} }))
vi.mock('@/components/StockPanel', () => ({
  StockPanel: () => <div data-testid="price-chart">price chart</div>,
  getDefaultRange: () => ({ start: '2026-03-30', end: '2026-09-30' }),
}))
vi.mock('@/components/StockMultiDayIntradayChart', () => ({ StockMultiDayIntradayChart: () => null }))
vi.mock('@/components/monitor/RuleEditor', () => ({ RuleEditor: () => null }))
vi.mock('@/components/stock-analysis/PriceAlertDialog', () => ({ PriceAlertDialog: () => null }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useQuoteStream', () => ({ setFocusSymbol: vi.fn(), clearFocusSymbol: vi.fn() }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))

let cleanup = async () => {}
afterEach(async () => { await cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('opening a stock preview requests live contracts, never retired A-share abnormal overview', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); client.clear(); host.remove() }
  await act(async () => {
    root.render(<QueryClientProvider client={client}><StockPreviewDialog symbol="NVDA" name="NVIDIA" onClose={() => {}} /></QueryClientProvider>)
  })
  expect(host.querySelector('[data-testid="price-chart"]')).not.toBeNull()
  expect(api.watchlistList).toHaveBeenCalled()
  expect(api.monitorRulesList).toHaveBeenCalled()
  expect(Object.keys(api)).not.toContain('abnormalOverview')
})

it('paste-code help uses US symbols rather than Shanghai/Shenzhen examples', () => {
  const source = readFileSync(resolve('src/components/WatchlistImportDialog.tsx'), 'utf8')
  expect(source).not.toMatch(/600519|000001 平安银行|515880 通信ETF/)
  expect(source).toContain('NVDA')
  expect(source).toContain('AAPL')
})
