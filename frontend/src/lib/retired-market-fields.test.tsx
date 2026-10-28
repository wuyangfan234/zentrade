// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { BUILTIN_COLUMNS, COLUMN_GROUPS, loadColumnConfig } from './watchlist-columns'
import { SCREENER_BUILTIN_COLUMNS, SCREENER_COLUMN_GROUPS, loadScreenerColumnConfig } from './screener-columns'
import { BUILTIN_INFO_FIELDS, INFO_GROUPS, loadInfoFields } from './stock-info-fields'
import { createExtColumn, mergeColumns, type ColumnConfig } from './list-columns'
import { getSortValue, getSignals } from './stock-table'
import { storage } from './storage'
import { BUILTIN_SIGNAL_DEFINITIONS } from './signals'
import { defaultFilter, filterActive, countActiveFilters, applyFilter, FilterPanel } from '@/components/screener/ScreenerFilter'
import { loadDefaultBasicFilter } from '@/components/screener/DefaultStrategyParamsDialog'
import { renderBasicFilterPy } from '@/components/screener/StrategyBuilderDialog'
import { renderBuiltinDataCell } from '@/components/stock-table/primitives'
import { StockInfoBar } from '@/components/StockInfoBar'
import { ScoringEditor } from '@/components/ScoringEditor'
import { QK } from './queryKeys'
import { buildDefaultOverrides } from './strategyOverrides'
import type { StrategyDetail } from './api'
import { StrategySettingsDialog } from '@/components/screener/StrategySettingsDialog'
import { CustomSignalDialog } from '@/components/signals/CustomSignalDialog'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
const savedConfig = vi.hoisted(() => vi.fn(async (_id: string, _payload: Record<string, any>) => ({ ok: true })))
vi.mock('@/lib/api', () => ({ api: {
  watchlistColumns: async () => ({ columns: [] }),
  screenerResultColumns: async () => ({ columns: [] }),
  factorColumns: async () => ({ columns: [] }),
  extDataSchemaAll: async () => ({ schemas: [] }),
  customSignalsList: async () => ({ signals: [] }),
  customSignalsOptions: async () => ({ fields: [], operators: [] }),
  strategyAiStatus: async () => ({ configured: false }),
  strategyGet: async () => ({ id: 'legacy', name: 'Legacy', source: 'builtin', params: [], params_defaults: {}, basic_filter: { price_min: 7, amount_min: 42, ...retiredBasic }, scoring: { turnover_rate: .15, momentum_60d: .85 }, scoring_directions: {}, entry_signals: [], exit_signals: [], stop_loss: null, max_hold_days: null }),
  strategySaveConfig: savedConfig,
} }))
const retiredKeys = ['turnover', 'float_val', 'market_cap', 'float_market_cap', 'deviate_3d', 'deviate_10d', 'deviate_30d']
const oldColumns: ColumnConfig[] = retiredKeys.map(key => ({ id: `builtin:${key}`, source: { type: 'builtin', key }, label: key, visible: true }))
const ext = { ...createExtColumn('my_metrics', 'Custom metrics', 'turnover_rate', 'Custom score'), visible: true }
const oldFilter = { ...defaultFilter, marketCapMin: '10', marketCapMax: '50', floatCapMin: '5', floatCapMax: '25' }
const retiredBasic = { market_cap_min: 9e8, market_cap_max: 1e12, float_cap_min: 7e8, float_cap_max: 4e10, turnover_min: 2, turnover_max: 10 }
const source = (path: string) => readFileSync(resolve('src', path), 'utf8')
let root: Root | undefined
let host: HTMLDivElement | undefined
let qc: QueryClient | undefined
afterEach(() => {
  if (root) act(() => root!.unmount())
  root = undefined
  host?.remove()
  qc?.clear()
  localStorage.clear()
})

it.each([
  ['watchlist', BUILTIN_COLUMNS, COLUMN_GROUPS],
  ['screener', SCREENER_BUILTIN_COLUMNS, SCREENER_COLUMN_GROUPS],
  ['info bar', BUILTIN_INFO_FIELDS, INFO_GROUPS],
] as const)('%s offers no retired builtins and merge drops old saved columns without deleting ext', (_name, columns, groups) => {
  for (const key of retiredKeys) {
    expect(columns.map(c => c.id)).not.toContain(`builtin:${key}`)
    expect(groups.flatMap(g => g.keys)).not.toContain(key)
  }
  const merged = mergeColumns([...oldColumns, ext], columns)
  expect(merged.filter(c => oldColumns.some(old => old.id === c.id))).toEqual([])
  expect(merged).toContainEqual(ext)
})

it('actual localStorage loaders discard retired builtins and preserve same-named custom fields', async () => {
  storage.watchlistColumns.set([...oldColumns, ext])
  storage.screenerResultColumns.set([...oldColumns, ext])
  storage.stockInfoBarFields.set([...oldColumns, ext])
  for (const columns of [await loadColumnConfig(), await loadScreenerColumnConfig(), loadInfoFields()]) {
    expect(columns.filter(c => oldColumns.some(old => old.id === c.id))).toEqual([])
    expect(columns).toContainEqual(ext)
  }
})

it('retired builtins have no live data renderer or sort values', () => {
  const row = { close: 100, float_shares: 1e8, turnover_rate: 5, deviate_3d: .5, deviate_10d: .2, deviate_30d: .3 }
  for (const col of oldColumns) {
    expect(getSortValue(row, col)).toBeNull()
    const html = renderToStaticMarkup(<table><tbody><tr>{renderBuiltinDataCell(row, col)}</tr></tbody></table>)
    expect(html).not.toMatch(/5\.00%|50\.00%|20\.00%|30\.00%|偏离值/)
  }
})

it('stale market cap filter values neither activate the badge nor silently remove every row', () => {
  const rows = [{ symbol: 'AAPL', close: 100 }, { symbol: 'NVDA', close: 200 }]
  expect(filterActive(oldFilter)).toBe(false)
  expect(countActiveFilters(oldFilter)).toBe(0)
  expect(applyFilter(rows, oldFilter)).toEqual(rows)
  expect(applyFilter(rows, { ...oldFilter, priceMin: '150' })).toEqual([rows[1]])
  const html = renderToStaticMarkup(<FilterPanel value={oldFilter} onChange={() => {}} onClose={() => {}} onReset={() => {}} />)
  expect(html).not.toMatch(/总市值|流通市值/)
})

it('old non-null default basic filters are removed and never reach a newly generated strategy', () => {
  storage.defaultStrategyBasicFilter.set({ price_min: 7, price_max: null, amount_min: 42, amount_max: null, exclude_st: false, boards: [], ...retiredBasic })
  const bf = loadDefaultBasicFilter()
  for (const key of Object.keys(retiredBasic)) expect(bf).not.toHaveProperty(key)
  expect(bf).toMatchObject({ price_min: 7, amount_min: 42 })
  expect(renderBasicFilterPy(bf)).not.toMatch(/market_cap|float_cap|turnover/)
})

it('stock info bar does not revive retired fields even with historical share data', async () => {
  qc = new QueryClient()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  await act(async () => root!.render(<QueryClientProvider client={qc!}><StockInfoBar symbol="AAPL" stockInfo={{ total_shares: 1e8, float_shares: 1e8 }} rows={[{ date: '2026-10-01', open: 100, close: 101, high: 102, low: 99, volume: 1000 }]} fields={oldColumns} onFieldsChange={() => {}} /></QueryClientProvider>))
  expect(host.textContent).not.toMatch(/market_cap|float_market_cap|turnover/)
})

it.each(['pages/Watchlist.tsx', 'components/EChartsCandlestick.tsx', 'components/DimensionMembersDialog.tsx', 'pages/factors/FactorEditor.tsx'])( '%s no longer has share-based display or turnover formula templates', path => {
  expect(/turnover_rate|turnoverRate|turnoverColor/.test(source(path))).toBe(false)
})

it.each(['components/screener/DefaultStrategyParamsDialog.tsx', 'components/screener/StrategySettingsDialog.tsx', 'pages/backtest/StrategyBacktest.tsx', 'components/monitor/RuleEditor.tsx'])('%s does not offer unavailable basic filter inputs', path => {
  expect(/(?:label[:=]\s*['"](?:流通市值|总市值|换手率)|>总市值下限|>流通市值[上下]限)/.test(source(path))).toBe(false)
})

it('deviation signals cannot be newly selected or appear as live hits', () => {
  expect(BUILTIN_SIGNAL_DEFINITIONS.filter(s => /deviate/.test(s.id))).toEqual([])
  expect(getSignals({ signal_deviate_3d: true, signal_deviate_10d: true, signal_deviate_30d: true })).toEqual([])
})

it('runtime overrides neutralize inherited retired filters without mutating strategy scoring or source config', () => {
  const detail = { basic_filter: { price_min: 7, ...retiredBasic }, entry_signals: [], exit_signals: [], scoring: { turnover_rate: .15, momentum_60d: .85 } } as unknown as StrategyDetail
  const overrides = buildDefaultOverrides(detail)
  for (const key of Object.keys(retiredBasic)) expect(overrides.basic_filter[key]).toBeNull()
  expect(overrides.basic_filter.price_min).toBe(7)
  expect(overrides.scoring).toEqual(detail.scoring)
  expect(detail.basic_filter).toEqual({ price_min: 7, ...retiredBasic })
})

it('strategy settings clean hidden inherited filters on load/save without changing existing weights', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  await act(async () => root!.render(<QueryClientProvider client={qc!}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><StrategySettingsDialog strategyId="legacy" onClose={() => {}} /></MemoryRouter></QueryClientProvider>))
  expect(host.textContent).not.toMatch(/流通市值|总市值/)
  await act(async () => [...host!.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存设置')!.click())
  const payload = savedConfig.mock.calls[0][1] as Record<string, any>
  for (const key of Object.keys(retiredBasic)) expect(payload.basic_filter[key]).toBeNull()
  expect(payload.basic_filter).toMatchObject({ price_min: 7, amount_min: 42 })
  expect(payload.scoring).toEqual({ turnover_rate: .15, momentum_60d: .85 })
})

it.each([false, true])('custom signal field picker excludes retired fields in stale API options, grouped=%s', async grouped => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const fields = ['close', 'ma20', 'volume', 'turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_3d', 'deviate_10d', 'deviate_30d', 'ext:custom.turnover_rate'].map(key => ({ key, label: key }))
  qc.setQueryData(QK.customSignalsOptions, { fields, groups: grouped ? [{ key: 'test', label: 'test', fields }] : undefined })
  await act(async () => root!.render(<QueryClientProvider client={qc!}><CustomSignalDialog open onClose={() => {}} /></QueryClientProvider>))
  await act(async () => [...host!.querySelectorAll('button')].find(b => b.textContent === 'close')!.click())
  const labels = [...document.querySelectorAll('button')].map(b => b.textContent)
  expect(labels).toContain('volume')
  expect(labels).toContain('ext:custom.turnover_rate')
  for (const field of fields.slice(3, -1)) expect(labels).not.toContain(field.label)
})

it('new scoring options omit retired fields while existing scoring weights remain visible and untouched', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(QK.factorColumns, { columns: ['turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_3d', 'deviate_10d', 'deviate_30d', 'volume', 'ext_custom_turnover_rate'].map(id => ({ id, label: id, group: 'test' })) })
  const onChange = vi.fn()
  const value = { turnover_rate: .15, momentum_60d: .85 }
  await act(async () => root!.render(<QueryClientProvider client={qc!}><ScoringEditor value={value} directions={{}} onChange={onChange} /></QueryClientProvider>))
  expect(host.textContent).toContain('turnover_rate')
  expect(host.textContent).toContain('15%')
  await act(async () => [...host!.querySelectorAll('button')].find(b => b.textContent === '编辑方案')!.click())
  const options = [...host.querySelectorAll('option')].map(o => o.value)
  for (const key of ['total_shares', 'float_shares', 'market_cap', 'float_market_cap', 'deviate_3d', 'deviate_10d', 'deviate_30d']) expect(options).not.toContain(key)
  expect(options).toContain('volume')
  expect(options).toContain('ext_custom_turnover_rate')
  await act(async () => host!.querySelector<HTMLButtonElement>('[aria-label="移除评分因子turnover_rate"]')!.click())
  expect([...host.querySelectorAll('option')].map(o => o.value)).not.toContain('turnover_rate')
  expect(onChange).not.toHaveBeenCalled()
  expect(value).toEqual({ turnover_rate: .15, momentum_60d: .85 })
})
