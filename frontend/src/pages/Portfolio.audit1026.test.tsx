// @vitest-environment jsdom
// audit1026 b3c: L-15 已平仓 R 保留 2 位 / L-16 来源列中文 / L-17 台账事件区去机会流水 + 空头止损下移
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  portfolioOverview: vi.fn(), portfolioLive: vi.fn(), tradingStatus: vi.fn(), setupStatus: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { Portfolio } from './Portfolio'

let qc: QueryClient
let root: Root
let container: HTMLDivElement

async function render() {
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><Portfolio /></QueryClientProvider>)
  })
  for (let i = 0; i < 4; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
}

const AT = '2026-10-22T10:32:00-04:00'

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.setupStatus.mockResolvedValue({ setups: [] })
  mocks.tradingStatus.mockResolvedValue({ booted: true, scheduled: true, in_session: false, protections: {} })
  mocks.portfolioOverview.mockResolvedValue({
    cash: 100000, nav: 100000, open_risk: 0, positions: [], nav_daily: [],
    events: [
      { seq: 1, at: AT, kind: 'fill', symbol: 'IREN', qty: 198, price: 27.69 },
      { seq: 2, at: AT, kind: 'stop_move', symbol: 'IREN', position_side: 'short', reason: 'trailing_1.2R' },
      { seq: 3, at: AT, kind: 'stop_move', symbol: 'CRCL', position_side: 'long', reason: 'trailing_1.0R' },
      // 旧后端仍可能返回机会卡状态流水
      { seq: 4, at: AT, kind: 'opportunity', symbol: 'SQQQ', reason: '最新流动性/波动门槛未通过' },
    ],
  })
  mocks.portfolioLive.mockResolvedValue({
    positions: [{
      symbol: 'IREN', side: 'short', qty: 198, setup_type: 'strategy:swing', setup_label: null,
      card_id: 'opp-2026-10-16-IREN-book-swing-short-f68caf03', entry_date: '2026-10-20', entry_price: 27.69,
      avg_cost: 27.68, last_price: 24.91, price_source: 'last_close', price_age_s: null, market_value: 549.68,
      unrealized_pnl: 549.68, unrealized_pct: 10.03, r_now: 1.2, stop_price: 27.68, stop_distance_pct: 11.15,
      target_zone: [22.37, 22.37], target_distance_pct: -10.18, days_held: 4, max_holding_days: 5, partial_taken: false,
    }],
    closed: [{
      symbol: 'AVGO', side: 'short', setup_type: 'strategy:swing', entry_date: '2026-10-19', entry_price: 334.12,
      exit_date: '2026-10-23', exit_price: 323.09, qty: 50, partial_exits: 0, exit_reason: 'time_stop',
      exit_reason_label: '持有到期时间止损', days_held: 5, fees: 2, pnl: 549.57, return_pct: 3.29,
      r_multiple: 1.0645423728813554,
    }],
    summary: {
      nav: 100000, cash: 100000, market_value: 0, initial_equity: 100000, total_return_pct: 0,
      realized_pnl: 0, unrealized_pnl: 0, closed_trades: 1, win_rate: 1, avg_r: 1.06, open_risk: 0,
      refresh_seconds: 5, as_of: AT,
    },
  })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  qc.clear()
})

it('L-15 已平仓 R 保留 2 位', async () => {
  await render()
  const row = container.querySelector('[data-closed="AVGO"]')!
  expect(row.textContent).toContain('1.06R')
  expect(row.textContent).not.toContain('1.0645423728813554')
})

it('L-16 来源列不显示 strategy:swing 原始码', async () => {
  await render()
  const setup = container.querySelector('[data-position="IREN"] [data-setup]')!
  expect(setup.textContent).toBe('机会板')
  expect(container.textContent).not.toContain('strategy:swing')
})

it('L-17 台账事件区不含机会流水, 空头止损写成止损下移', async () => {
  await render()
  const text = container.textContent ?? ''
  expect(text).toContain('台账事件 (3)')
  expect(text).not.toContain('SQQQ')
  expect(text).toContain('#2 止损下移 IREN')
  expect(text).toContain('#3 止损上移 CRCL')
})
