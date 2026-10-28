// @vitest-environment jsdom
// 0.18.0: 已结算卡逐张明细 (只读 cards.zt-settle-v1.jsonl), 按规则 / 退出原因筛选
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ settlementCards: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { SettlementCards } from './SettlementCards'

const rows = [
  { key: 'k1', card_id: 'c1', record_kind: 'settled', outcome: 'HIT', rule: 'r1', symbol: 'AAA', side: 'long', signal_day: '2026-09-30',
    fill_date: '2026-10-01', exit_date: '2026-10-09', exit_reason: 'time_stop', hold_days: 7, r: 0.4, excess_r: 0.1, provenance: 'live' },
  { key: 'k2', card_id: 'c2', record_kind: 'settled', outcome: 'HIT', rule: 'r2', symbol: 'BBB', side: 'short', signal_day: '2026-09-29',
    fill_date: '2026-09-30', exit_date: '2026-10-01', exit_reason: 'invalidation', hold_days: 2, r: -1.2, excess_r: null, provenance: 'backfill' },
  { key: 'k3', card_id: 'c3', record_kind: 'pass_void', outcome: 'PASS', rule: 'r1', symbol: 'CCC', side: 'long', signal_day: '2026-09-29', provenance: 'live' },
]
let qc: QueryClient
let root: Root
let host: HTMLDivElement
async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><SettlementCards /></QueryClientProvider>) })
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.settlementCards.mockResolvedValue({ available: true, contract: 'zt-settle-v1', total: 3, rows,
    rules: ['r1', 'r2'], exit_reasons: ['invalidation', 'time_stop'] })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks() })

const shown = () => [...host.querySelectorAll('[data-card-row]')].map((e) => e.getAttribute('data-card-row'))
const pick = async (name: string, v: string) => {
  const s = host.querySelector(`select[data-filter="${name}"]`) as HTMLSelectElement
  await act(async () => { s.value = v; s.dispatchEvent(new Event('change', { bubbles: true })) })
}

it('lists every jsonl row and the count equals total', async () => {
  await render()
  expect(shown()).toEqual(['c1', 'c2', 'c3'])
  expect(host.querySelector('[data-cards-count]')!.textContent).toBe('3 / 3')
  const c2 = host.querySelector('[data-card-row="c2"]')!
  expect(c2.textContent).toContain('-1.20')
  expect(c2.textContent).toContain('invalidation')
})

it('filters by rule and exit reason', async () => {
  await render()
  await pick('rule', 'r1')
  expect(shown()).toEqual(['c1', 'c3'])
  await pick('exit_reason', 'time_stop')
  expect(shown()).toEqual(['c1'])
  expect(host.querySelector('[data-cards-count]')!.textContent).toBe('1 / 3')
})

it('missing file shows empty state', async () => {
  mocks.settlementCards.mockResolvedValue({ available: false, contract: 'zt-settle-v1', total: 0, rows: [], rules: [], exit_reasons: [] })
  await render()
  expect(host.querySelector('[data-cards-empty]')).not.toBeNull()
})
