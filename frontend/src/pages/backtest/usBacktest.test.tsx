// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderToStaticMarkup } from 'react-dom/server'
import { StrategyBacktest } from './StrategyBacktest'
import { strategyResultCandidate } from './researchCandidates'
import { stopBacktest, tryReconnect, clearBacktest } from '@/lib/backtestTask'
import { startBacktest } from './backtestRequest'
import type { StrategyBacktestResult } from '@/lib/api'

vi.mock('@/components/WarmupBadge', () => ({ WarmupBadge: () => null }))
vi.mock('@/components/DatePicker', () => ({ DatePicker: () => <input /> }))

const source = (name: string) => readFileSync(resolve('src/pages/backtest', name), 'utf8')
afterEach(() => { clearBacktest(); localStorage.clear(); vi.unstubAllGlobals() })

it('new forms expose USD per-share commissions, not legacy tax or lot inputs', () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const html = renderToStaticMarkup(<QueryClientProvider client={qc}><StrategyBacktest /></QueryClientProvider>)
  expect(html).toContain('每股佣金 (USD)')   // ui-audit-1011: 去掉「/股」免换行, 每股已表明按股计
  expect(html).toContain('每笔最低 (USD)')
  expect(html).toContain('初始资金 (USD)')
  expect(html).not.toMatch(/印花税|佣金 ‱/)
  qc.clear()
})

it('strategy settings and results have no China-only boards, ST, lots, or benchmark labels', () => {
  expect(/沪主板|深主板|创业板|科创板|北交所|排除 ST|同期上证|基准\(上证\)|手数|fmtLots/.test(source('StrategyBacktest.tsx'))).toBe(false)
  expect(/上证|connectNulls: true/.test(source('charts/StrategyNavChart.tsx'))).toBe(false)
})

it('candidate snapshots retain explicit share costs and untouched legacy costs', () => {
  for (const costs of [
    { cost_model: 'us_per_share_v1', commission_per_share: 0.02, commission_min: 2 },
    { cost_model: 'legacy_pct', fees_pct: 0.0007, commission_pct: 0.0003, stamp_tax_pct: 0.001 },
  ]) {
    const result = { config: { strategy_id: 's', ...costs }, stats: {} } as unknown as StrategyBacktestResult
    expect(strategyResultCandidate(result).config).toMatchObject(costs)
    expect(result.config).toMatchObject(costs)
  }
})

it('actual SSE request, reconnect and cancel keep explicit share costs and fill fields', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, body: { cancel: vi.fn().mockResolvedValue(undefined) } })
  vi.stubGlobal('fetch', fetcher)
  const sources: string[] = []
  vi.stubGlobal('EventSource', class {
    constructor(url: string) { sources.push(url) }
    close() {}
    addEventListener() {}
  })
  const params = {
    strategy_id: 's', cost_model: 'us_per_share_v1' as const, commission_per_share: 0.02, commission_min: 2,
    minute_fill: true, entry_fill: 'open_t+1' as const, exit_fill: 'signal_next_minute' as const, regime_filter: { states: ['up'] },
  }
  startBacktest(params)
  await vi.waitFor(() => expect(sources.length).toBe(1))
  const qs = new URL(sources[0], 'http://localhost').searchParams
  expect(qs.get('cost_model')).toBe('us_per_share_v1')
  expect(qs.get('commission_per_share')).toBe('0.02')
  expect(qs.get('commission_min')).toBe('2')
  expect(qs.has('fees_pct')).toBe(false)
  expect(qs.has('stamp_tax_pct')).toBe(false)
  expect(qs.get('minute_fill')).toBe('true')
  expect(tryReconnect()).toBe(true)
  await vi.waitFor(() => expect(sources.length).toBe(2))
  expect(sources[1]).toBe(sources[0])
  await stopBacktest()
  const cancel = fetcher.mock.calls.find(([url]) => url === '/api/backtest/strategy/cancel')!
  expect(JSON.parse(cancel[1].body)).toEqual({ qs: qs.toString() })
})
