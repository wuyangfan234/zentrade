// @vitest-environment jsdom
// audit1026 b3c: L-18 行 key 唯一 (pass_void 与 pass_signal 同 key) / L-19 pass_signal 行读 signal_* 字段
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

// 与生产同形: 同一张卡 c2 有 pass_void + pass_signal 两条记录, key 相同
const rows = [
  { key: 'k1', card_id: 'c1', record_kind: 'settled', outcome: 'HIT', rule: 'r1', symbol: 'AAA', side: 'long', signal_day: '2026-10-14',
    fill_date: '2026-10-15', exit_date: '2026-10-23', exit_reason: 'time_stop', hold_days: 7, r: 0.4, excess_r: 0.1, provenance: 'live' },
  { key: 'k2', card_id: 'c2', record_kind: 'pass_void', outcome: 'PASS', rule: 'r2', symbol: 'WDC', side: 'short', signal_day: '2026-10-14', provenance: 'live' },
  { key: 'k2', card_id: 'c2', record_kind: 'pass_signal', outcome: 'PASS', rule: 'r2', symbol: 'WDC', side: 'short', signal_day: '2026-10-14',
    provenance: 'live', signal_r: -0.0217, signal_exit_date: '2026-10-23', signal_exit_reason: 'time_stop', signal_excess_r: 0.05 },
  { key: 'k3', card_id: 'c3', record_kind: 'settled', outcome: 'HIT', rule: 'r1', symbol: 'BBB', side: 'long', signal_day: '2026-10-13',
    fill_date: '2026-10-14', exit_date: '2026-10-15', exit_reason: 'invalidation', hold_days: 2, r: -1, excess_r: null, provenance: 'live' },
]
let qc: QueryClient
let root: Root
let host: HTMLDivElement
const errors: string[] = []
async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><SettlementCards /></QueryClientProvider>) })
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  errors.length = 0
  vi.spyOn(console, 'error').mockImplementation((...a: unknown[]) => { errors.push(a.map(String).join(' ')) })
  mocks.settlementCards.mockResolvedValue({ available: true, contract: 'zt-settle-v1', total: 4, rows,
    rules: ['r1', 'r2'], exit_reasons: ['invalidation', 'time_stop'] })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks() })

const shownRows = () => [...host.querySelectorAll('[data-card-row]')]
const pick = async (name: string, v: string) => {
  const s = host.querySelector(`select[data-filter="${name}"]`) as HTMLSelectElement
  await act(async () => { s.value = v; s.dispatchEvent(new Event('change', { bubbles: true })) })
}

it('L-18 筛选来回后 DOM 行数与数据一致, 无重复 key 告警', async () => {
  await render()
  expect(shownRows()).toHaveLength(4)
  await pick('rule', 'r1')
  expect(shownRows().map((e) => e.getAttribute('data-card-row'))).toEqual(['c1', 'c3'])
  await pick('rule', 'r2')
  expect(shownRows()).toHaveLength(2)
  await pick('rule', '')
  expect(shownRows()).toHaveLength(4)
  expect(errors.some((e) => e.includes('same key'))).toBe(false)
})

it('L-19 pass_signal 行显示 signal_r / signal_exit_date / signal_exit_reason, 并参与退出原因筛选', async () => {
  await render()
  const sig = shownRows().find((e) => e.textContent?.includes('pass_signal'))!
  expect(sig.textContent).toContain('2026-10-23')
  expect(sig.textContent).toContain('time_stop')
  expect(sig.textContent).toContain('-0.02')
  await pick('exit_reason', 'time_stop')
  expect(shownRows().map((e) => e.getAttribute('data-card-row'))).toEqual(['c1', 'c2'])
  expect(host.querySelector('[data-cards-count]')!.textContent).toBe('2 / 4')
})
