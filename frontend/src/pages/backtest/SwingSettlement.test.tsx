// @vitest-environment jsdom
// 0.26.0 机会板卡结局: 实际 vs 回放, 多空分开, 每个数带随时有效区间; 区间不重叠的行标红; 最近对账状态
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ swingSettlement: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { SwingSettlement, reconcileTitle } from './SwingSettlement'

const prop = (p: number | null, ci: number[], k = 0, n = 0) => ({ p, ci, k, n })
const stats = (o: Record<string, unknown> = {}) => ({
  cards: 0, filled: 0, fill: prop(null, [0, 1]), win: prop(null, [0, 1]), target: prop(null, [0, 1]),
  stop: prop(null, [0, 1]), time: prop(null, [0, 1]), mean_r: { v: null, ci: null, n: 0 }, ...o,
})
const replay = stats({ cards: 2326, filled: 1383, fill: prop(0.5946, [0.5598, 0.6294]), win: prop(0.5098, [0.46, 0.56]),
  target: prop(0.2025, [0.16, 0.25]), stop: prop(0.3261, [0.28, 0.37]), time: prop(0.4714, [0.42, 0.52]),
  mean_r: { v: 0.2652, ci: [-0.0235, 0.5067], n: 1383 } })

let qc: QueryClient
let root: Root
let host: HTMLDivElement

async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><SwingSettlement /></QueryClientProvider>) })
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear() })

it('实际与回放并排, 区间不重叠的行标红, 对账不一致显示', async () => {
  mocks.swingSettlement.mockResolvedValue({
    contract: 'swing-settle-v1', card_contract: 'swing-desk-tight-v1', alpha: 0.05, base_window: ['2023-01-03', '2026-09-18'],
    sides: {
      long: { actual: stats({ cards: 300, filled: 0, fill: prop(0, [0, 0.09], 0, 300) }), replay, mismatch: ['fill'] },
      short: { actual: stats(), replay: null, mismatch: [] },
    },
    recent: [{ card_id: 'c1', symbol: 'AAPL', side: 'long', signal_day: '2026-10-15', zone: [338.74, 340.37], stop: 333.85,
      target: 353.41, status: 'no_fill' }],
    n_total: 300, reconcile: [{ day: '2026-10-19', ok: false, issues: [{ kind: 'book_card_not_recomputed', symbol: 'X' }],
      zero_hit_rules: { 'tv_halftrend:short': { days: 14, history_max: 10 } } }],
  })
  await render()
  const long = host.querySelector('[data-swing-settle-side="long"]')!
  const fill = long.querySelector('[data-settle-row="fill"]')!
  expect(fill.hasAttribute('data-mismatch')).toBe(true)
  expect(fill.textContent).toContain('0%')
  expect(fill.textContent).toContain('59%')
  expect(long.querySelector('[data-settle-row="win"]')!.hasAttribute('data-mismatch')).toBe(false)
  expect(host.querySelector('[data-settle-card="AAPL"]')!.textContent).toContain('未回区')
  expect(host.querySelector('[data-reconcile]')!.getAttribute('data-reconcile')).toBe('bad')
  expect(host.querySelector('[data-reconcile]')!.textContent).toContain('1 处不一致')
  expect(host.querySelector('[data-zero-hit]')!.textContent).toContain('tv_halftrend:short 连续 14 天 0 命中（历史最长 10）')
  expect(host.querySelector('[data-swing-settle-side="short"]')!.textContent).toContain('实际 0 张')
})

it('对账: 超过历史最长的连续 0 命中单独显示; 悬停逐条列信号层不一致', async () => {
  const rec = { day: '2026-10-19', ok: false, board_day: '2026-10-16',
    issues: [{ kind: 'signal_missing', rule: 'ibs_mean_reversion', symbol: 'AMZN', side: 'long' },
      { kind: 'signal_extra', rule: 'tv_smc_structure', symbol: 'WDC', side: 'short' }],
    signal_check: { ibs_mean_reversion: { prod: 0, replay: 7 } } }
  const t = reconcileTitle(rec)
  expect(t).toContain('回放有、生产没出 ibs_mean_reversion AMZN')
  expect(t).toContain('生产出了、回放没有 tv_smc_structure WDC 空')
  expect(t).toContain('板层：2026-10-16 的簿')
  expect(reconcileTitle({ day: '2026-10-19', ok: true, issues: [], signal_check: 'unavailable' })).toContain('信号层未核')
})
