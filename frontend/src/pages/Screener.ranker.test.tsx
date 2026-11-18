// @vitest-environment jsdom
// 2026-11-18 清查复现: 入选的排序因子在策略页有一条只读条目; 名称里的英文因子 id 换成中文名。
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
  { id: 'mined_x', name: '因子组合 · annual_vol_20d' },
].map(s => ({
  ...s, description: '', tags: [], source: 'builtin', asset_types: ['stock', 'etf'], timeframes: ['1d'], params: [], params_defaults: {},
}))
const PANEL = {
  contract: 'strategy-panel-v1', generated_at: '2026-11-11T00:00:00-05:00',
  selected: { long: ['ibs_mean_reversion', 'rank_59ae3b44ef'], short: ['tv_halftrend'] },
  replay_meta: { window: ['2023-01-03', '2026-10-30'], universe: 'watchlist 46', data_end: '2026-11-09', generated: null },
  signals: { day: '2026-11-10', requested: null, days: ['2026-11-09', '2026-11-10'], universe: 46 },
  paper_since: '2026-10-15', errors: {},
  strategies: {
    boll_breakout: { short_capable: false, name_short: null, sides: { long: side() } },
    ibs_mean_reversion: { short_capable: false, name_short: null, sides: { long: side({ selected: true, hits: ['MRNA'], replay: { n: 1323, fill: 0.61, mean_r: 0.31, excess: 0.06, p: 0.035 } }) } },
    macd_golden: { short_capable: false, name_short: null, sides: { long: side() } },
    rank_59ae3b44ef: { short_capable: false, name_short: null, ranker: { name: '排序 · 波动扩张', formula: 'ts_mean(high/low-1,5)', direction: 'high' },
      sides: { long: side({ selected: true, hits: ['CBRS', 'META'], hist: [0, 2], replay: { n: 1196, mean_r: 0.31, excess: 0.9, p: 0.044 } }) } },
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
    factorLibrary: () => ({ factors: [{ id: 'annual_vol_20d', label: '20日波动率' }] }),
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

it('入选的排序因子占一行只读条目: 计入入选数, 有自己的回放与命中, 没有扫描 / 监控 / 参数 / 回测入口', async () => {
  await mount()
  expect(qa('[data-rail-item]').map(e => e.getAttribute('data-rail-item'))).toContain('rank_59ae3b44ef')
  expect(container.textContent).toContain('入选 3 · 策略池 1 · 共 6')                    // 「共」与列表「全部」同数, 含只读条目
  expect(q('[data-rail-filter="all"]')!.textContent).toBe('全部6')
  await click(q('[data-rail-item="rank_59ae3b44ef"]'))
  const d = q('[data-strategy-detail]')!
  expect(d.getAttribute('data-strategy-detail')).toBe('rank_59ae3b44ef')
  expect(q('[data-detail-name]')!.textContent).toBe('排序 · 波动扩张')
  expect(d.textContent).toContain('排序因子')
  expect(q('[data-hits]')!.textContent).toContain('CBRS')
  expect(q('[data-kpi="replay-excess"]')!.textContent).toContain('+0.90R')
  expect(q('[data-ranker-formula]')!.textContent).toContain('ts_mean(high/low-1,5)')
  for (const sel of ['[data-switch="pool"]', '[data-switch="monitor"]', '[data-detail-settings]', '[data-detail-backtest]', '[data-market-scan]', '[data-market-idle]']) {
    expect(q(sel), sel).toBeNull()
  }
  expect((q('[data-detail-rules]') as HTMLAnchorElement).getAttribute('href')).toBe('/?rules=1')
  expect(calls.screenerRunPreset).toBeUndefined()
  expect(calls.screenerCachedResult).toBeUndefined()
  expect(calls.strategyPoolSave).toBeUndefined()
})

it('排序因子排在列表第一项时默认选中它, 地址 ?s= 也能直达', async () => {
  await mount()
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('rank_59ae3b44ef')     // 回放超额最高
  act(() => { root!.unmount() }); root = null
  container.innerHTML = ''
  await mount('/screener?s=rank_59ae3b44ef')
  expect(q('[data-strategy-detail]')!.getAttribute('data-strategy-detail')).toBe('rank_59ae3b44ef')
})

it('策略名里的英文因子 id 换成因子库中文名', async () => {
  await mount()
  await click(q('[data-rail-filter="all"]'))
  expect(q('[data-rail-item="mined_x"]')!.textContent).toContain('因子组合 · 20日波动率')
  expect(container.textContent).not.toContain('annual_vol_20d')
})
