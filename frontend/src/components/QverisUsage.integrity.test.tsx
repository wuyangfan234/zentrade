// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it, vi } from 'vitest'
import { QverisUsageCard } from './QverisUsageCard'
import { PaidFetchBar } from './QverisPanels'

const state = vi.hoisted(() => ({ data: {} as Record<string, unknown> }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: state.data, isLoading: false }),
  useQueryClient: () => ({ setQueryData: vi.fn() }),
  useMutation: () => ({ data: undefined, isPending: false, mutate: vi.fn() }),
}))

const normal = { configured: true, day: '2026-10-04', spent_today: 5, daily_limit: 100,
  balance: { remaining: 9000, error: null }, by_kind: {}, recent: [] }

it('renders corrupt-ledger spending as unknown, not zero, with no usage percentage or fake counts', () => {
  state.data = { ...normal, spent_today: null, error: 'ledger.jsonl 第 2 行损坏，收费已停用' }
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(<QverisUsageCard />)
  expect(host.querySelector('[data-spent]')?.textContent).toContain('未知')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('第 2 行损坏')
  expect(host.innerHTML).not.toContain('width:0%')
  expect(host.textContent).not.toContain('期权 0 次')
})

it('disables paid query and explains the lock when spend cannot be established', () => {
  state.data = { ...normal, spent_today: null, error: 'ledger.jsonl 有未确认扣费' }
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(<PaidFetchBar pending={false} onFetch={() => {}} label="查询期权" />)
  expect(host.querySelector('button')?.disabled).toBe(true)
  expect(host.textContent).toContain('额度未知')
  expect(host.textContent).toContain('暂停收费查询')
})

it('retains normal known-spending and free balance display', () => {
  state.data = normal
  const html = renderToStaticMarkup(<QverisUsageCard />)
  expect(html).toContain('width:5%')
  expect(html).toContain('9,000')
  expect(html).not.toContain('额度未知')
})
