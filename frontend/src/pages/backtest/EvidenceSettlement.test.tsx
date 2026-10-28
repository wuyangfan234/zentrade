// @vitest-environment jsdom
// 卡结局 (方向 A, 2026-10-11): 能成交 / 信号准 × live / 全部; n<20 灰且 —, 20≤n<30 不着色, n≥30 且区间不跨 0 着色
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ cardSettlement: vi.fn(), settlementCards: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { EvidenceSettlement } from './EvidenceSettlement'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const cell = (o: Record<string, unknown>) => ({ n: 0, settled: 0, exp: 0, pass: 0, unsettled: 0, missing: 0, same_bar: 0, trigger_rate: null,
  win_rate: null, mean_r: null, mean_excess_r: null, ci: null, worst_r: null, tone: 'gray', ...o })
const tab = (kind: string, scope: string, cells: ReturnType<typeof cell>[]) => ({ kind, scope, m: cells.length, cells, all: cell({ n: 99, tone: 'plain' }) })
const fill = [
  cell({ rule: 'macd_golden', side: 'long', n: 12, exp: 3, unsettled: 4 }),
  cell({ rule: 'tv_ut_bot', side: 'long', n: 24, trigger_rate: 0.4, win_rate: 0.5, mean_r: 0.12, mean_excess_r: 0.05, ci: [-0.1, 0.2], worst_r: -1.02, tone: 'plain' }),
  cell({ rule: 'tv_supertrend', side: 'short', n: 41, win_rate: 0.6, mean_r: 0.3, mean_excess_r: 0.21, ci: [0.05, 0.4], worst_r: -1.0, same_bar: 2, tone: 'pos' }),
  cell({ rule: 'phantom_4h_short', side: 'short', n: 35, mean_r: -0.4, mean_excess_r: -0.3, ci: [-0.5, -0.1], tone: 'neg' }),
]
const payload = {
  available: true, contract: 'zt-settle-v1', data_asof: '2026-10-09', generated_at: '2026-10-10T07:00:00+08:00',
  tables: { fill: { live: tab('fill', 'live', fill), all: tab('fill', 'all', [cell({ rule: 'x', side: 'long', n: 50, tone: 'plain' })]) },
            signal: { live: tab('signal', 'live', [cell({ rule: 'macd_golden', side: 'long', n: 14, pass: 2 })]), all: tab('signal', 'all', []) } },
}

async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><EvidenceSettlement /></QueryClientProvider>) })
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.cardSettlement.mockResolvedValue(payload)
  mocks.settlementCards.mockResolvedValue({ available: false, contract: 'zt-settle-v1', total: 0, rows: [], rules: [], exit_reasons: [] })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

it('three tone bands: gray hides numbers, plain shows uncolored, colored only when n ≥ 30 and CI clear of 0', async () => {
  await render()
  const row = (k: string) => host.querySelector(`[data-cell="${k}"]`)!
  expect(row('macd_golden 多').getAttribute('data-tone')).toBe('gray')
  expect(row('macd_golden 多').className).toContain('text-muted')
  expect([...row('macd_golden 多').querySelectorAll('td')].map((e) => e.textContent))
    .toEqual(['macd_golden 多', '12', '—', '3', '0', '—', '—', '—', '—', '—', '0', '4', '0'])
  expect(row('tv_ut_bot 多').className).toContain('text-foreground')
  expect(row('tv_ut_bot 多').textContent).toContain('-0.10 ~ +0.20')
  expect(row('tv_supertrend 空').className).toContain('text-bull')
  expect(row('phantom_4h_short 空').className).toContain('text-bear')
  expect(row('全部').textContent).toContain('99')
  expect(host.querySelector('[data-settle-meta]')!.textContent).toBe('zt-settle-v1 · 2026-10-09 · m 4')
  expect(host.textContent).not.toContain('。')
})

it('switches 能成交/信号准 and live/全部', async () => {
  await render()
  await act(async () => { (host.querySelector('[data-seg="kind"] [data-seg-opt="signal"]') as HTMLElement).click() })
  expect([...host.querySelectorAll('tbody [data-cell]')].map((e) => e.getAttribute('data-cell'))).toEqual(['macd_golden 多'])
  await act(async () => { (host.querySelector('[data-seg="scope"] [data-seg-opt="all"]') as HTMLElement).click() })
  expect(host.querySelectorAll('tbody [data-cell]').length).toBe(0)
  await act(async () => { (host.querySelector('[data-seg="kind"] [data-seg-opt="fill"]') as HTMLElement).click() })
  expect([...host.querySelectorAll('tbody [data-cell]')].map((e) => e.getAttribute('data-cell'))).toEqual(['x 多'])
})

it('not yet settled → a single dash, no table', async () => {
  mocks.cardSettlement.mockResolvedValue({ available: false, contract: 'zt-settle-v1', tables: null })
  await render()
  expect(host.querySelector('[data-settle-empty]')!.textContent).toBe('—')
  expect(host.querySelector('table')).toBeNull()
})
