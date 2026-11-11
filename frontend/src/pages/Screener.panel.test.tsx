// @vitest-environment jsdom
// 策略页 (详情台) 页面级行为: 进页默认选中、点列表不乱扫全市场、策略池开关写服务器。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const side = (over: Record<string, unknown> = {}) => ({ selected: false, replay: null, hits: [], hist: [0, 0], board: [], paper: null, ...over })
const STRATS = [
  { id: 'boll_breakout', name: '布林突破' },
  { id: 'ibs_mean_reversion', name: 'QS-IBS' },
  { id: 'macd_golden', name: 'MACD 金叉放量' },
  { id: 'tv_halftrend', name: 'HalfTrend 翻多' },
].map(s => ({
  ...s, description: '', tags: [], source: 'builtin', asset_types: ['stock', 'etf'], timeframes: ['1d'], params: [], params_defaults: {},
}))
const PANEL = {
  contract: 'strategy-panel-v1', generated_at: '2026-11-11T00:00:00-05:00',
  selected: { long: ['ibs_mean_reversion'], short: ['tv_halftrend'] },
  replay_meta: { window: ['2023-01-03', '2026-10-30'], universe: 'watchlist 46', data_end: '2026-11-09', generated: null },
  signals: { day: '2026-11-10', requested: null, days: ['2026-11-09', '2026-11-10'], universe: 46 },
  paper_since: '2026-10-15', errors: {},
  strategies: {
    boll_breakout: { short_capable: false, name_short: null, sides: { long: side() } },
    ibs_mean_reversion: { short_capable: false, name_short: null, sides: { long: side({ selected: true, hits: ['MRNA'], replay: { n: 1323, fill: 0.61, mean_r: 0.31, excess: 0.06, p: 0.035 } }) } },
    macd_golden: { short_capable: false, name_short: null, sides: { long: side() } },
    tv_halftrend: { short_capable: true, name_short: 'HalfTrend 翻空', sides: { long: side(), short: side({ selected: true, replay: { n: 164, fill: 0.65, mean_r: 0.25, excess: 0.25, p: 0 } }) } },
  },
}

const calls: Record<string, unknown[][]> = {}
let scanned: Record<string, number> = {}
const handlers: Record<string, (...a: any[]) => unknown> = {}
vi.mock('@/lib/api', async importOriginal => {
  const mod = await importOriginal<Record<string, unknown>>()
  const api = new Proxy({}, {
    get: (_t, k: string) => (...args: unknown[]) => {
      (calls[k] ||= []).push(args)
      return Promise.resolve(handlers[k] ? handlers[k](...args) : {})
    },
  })
  return { ...mod, api }
})

import { Screener } from './Screener'

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
const q = (sel: string) => container.querySelector(sel)
const qa = (sel: string) => [...container.querySelectorAll(sel)]
const flush = (ms = 0) => act(async () => { await new Promise(r => setTimeout(r, ms)) })
const click = async (el: Element | null) => {
  await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await flush()
}

async function mount(path = '/screener') {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}><Screener /></MemoryRouter>
      </QueryClientProvider>,
    )
  })
  for (let i = 0; i < 6; i++) await flush(5)
}

beforeEach(() => {
  localStorage.clear()
  for (const k of Object.keys(calls)) delete calls[k]
  scanned = { boll_breakout: 24 }
  for (const k of Object.keys(handlers)) delete handlers[k]
  Object.assign(handlers, {
    screenerStrategies: () => ({ presets: STRATS, load_errors: [] }),
    dataStatus: () => ({ enriched: { latest_date: '2026-11-10', earliest_date: '2023-01-03' } }),
    preferences: () => ({ screener_auto_run: true }),
    capabilities: () => ({ capabilities: {} }),
    quoteStatus: () => ({ running: false }),
    minuteRefreshStatus: () => ({ healthy: false }),
    watchlistList: () => ({ symbols: [] }),
    monitorRulesList: () => ({ rules: [] }),
    screenerResultColumns: () => ({ columns: null }),
    strategyPanel: () => PANEL,
    strategyPoolGet: () => ({ pool: ['boll_breakout'], saved: true }),
    strategyPoolSave: (pool: string[]) => ({ pool, saved: true }),
    // 服务端缓存: 扫过的策略进摘要 (与后端一致, 否则自动扫描会把同一个策略再扫一遍)
    screenerCachedSummary: () => ({
      as_of: '2026-11-10',
      results: Object.fromEntries(Object.entries(scanned).map(([id, total]) => [id, { total, as_of: '2026-11-10', computed_at: 1 }])),
      today_ever_counts: { ...scanned }, updated_at: 1,
    }),
    screenerCachedResult: (id: string) => ({ result: { as_of: '2026-11-10', strategy: id, rows: [], total: scanned[id] ?? 0, elapsed_ms: 0 }, strategy_ids_by_symbol: {}, today_ever_rows: {} }),
    screenerRunPreset: (id: string) => {
      scanned[id] = 7
      return { as_of: '2026-11-10', strategy: id, rows: [], total: 7, elapsed_ms: 1 }
    },
    screenerRunAll: (_date: string, ids: string[]) => {
      for (const id of ids) scanned[id] = 5
      return { as_of: '2026-11-10', results: Object.fromEntries(ids.map(id => [id, { total: 5, as_of: '2026-11-10' }])), pending: [] }
    },
  })
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('进页: 一个策略一行, 默认看已启用并选中第一条入选策略; 不因为默认选中去扫全市场', async () => {
  await mount()
  // 已启用 = 2 条入选 + 1 条在策略池; 可做空的 HalfTrend 只占一行
  expect(qa('[data-rail-item]').map(e => e.getAttribute('data-rail-item'))).toEqual(['tv_halftrend', 'ibs_mean_reversion', 'boll_breakout'])
  expect(q('[data-rail-filter="active"]')!.getAttribute('aria-selected')).toBe('true')
  expect(q('[data-rail-filter="all"]')!.textContent).toBe('全部4')
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('tv_halftrend')
  expect(q('[data-side="short"]')!.getAttribute('aria-selected')).toBe('true')      // 入选的是做空那一面
  expect(q('[data-market-idle]')!.textContent).toContain('未扫描')
  expect(calls.screenerRunPreset).toBeUndefined()
  expect(calls.screenerRunAll).toBeUndefined()                                       // 缓存已覆盖策略池
  expect(calls.strategyPoolSave).toBeUndefined()
  expect(container.textContent).toContain('入选 2 · 策略池 1 · 共 4 · 信号日 11-10')
})

it('点不在策略池里的策略只切换详情, 不扫全市场; 点「扫描」才扫', async () => {
  await mount()
  await click(q('[data-rail-item="ibs_mean_reversion"]'))
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('ibs_mean_reversion')
  expect(q('[data-hits]')!.textContent).toContain('MRNA')
  expect(calls.screenerRunPreset).toBeUndefined()
  await click(q('[data-market-scan]'))
  expect(calls.screenerRunPreset).toHaveLength(1)
  expect(calls.screenerRunPreset[0][0]).toBe('ibs_mean_reversion')
  expect(q('[data-rail-item="ibs_mean_reversion"]')!.textContent).toContain('全市场7')
})

it('点策略池里已有缓存的策略: 读缓存, 不重算', async () => {
  await mount()
  await click(q('[data-rail-item="boll_breakout"]'))
  for (let i = 0; i < 3; i++) await flush(5)
  expect(calls.screenerCachedResult?.[0]?.[0]).toBe('boll_breakout')
  expect(calls.screenerRunPreset).toBeUndefined()
  expect(q('[data-rail-item="boll_breakout"]')!.textContent).toContain('全市场24')
})

it('「全市场扫描」开关 = 进出策略池: 写服务器, 新进池的日线策略立即扫一次', async () => {
  await mount()
  await click(q('[data-rail-item="ibs_mean_reversion"]'))
  expect(q('[data-switch="pool"]')!.getAttribute('aria-checked')).toBe('false')
  await click(q('[data-switch="pool"]'))
  expect(calls.strategyPoolSave.at(-1)![0]).toEqual(['boll_breakout', 'ibs_mean_reversion'])
  expect(q('[data-switch="pool"]')!.getAttribute('aria-checked')).toBe('true')
  expect(calls.screenerRunAll).toHaveLength(1)
  expect(calls.screenerRunAll[0][1]).toEqual(['ibs_mean_reversion'])
  await click(q('[data-switch="pool"]'))
  expect(calls.strategyPoolSave.at(-1)![0]).toEqual(['boll_breakout'])
})

it('地址里的 ?s= 直达某个策略; 不存在的策略退回列表第一项', async () => {
  await mount('/screener?s=boll_breakout')
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('boll_breakout')
  act(() => { root!.unmount() }); root = null
  container.innerHTML = ''
  await mount('/screener?s=no_such_strategy')
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('tv_halftrend')
})

it('页面没有修改入选策略的入口, 只有跳去机会页规则设置的链接', async () => {
  await mount()
  expect((q('[data-detail-rules]') as HTMLAnchorElement).getAttribute('href')).toBe('/?rules=1')
  expect(Object.keys(calls).filter(k => /oppSettings|rulesSave|saveWatchlistRules|oppRules/i.test(k))).toEqual([])
})
