// @vitest-environment jsdom
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

import { Portfolio, LIVE_REFRESH_MS } from './Portfolio'

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

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.setupStatus.mockResolvedValue({ setups: [] })
  mocks.tradingStatus.mockResolvedValue({ booted: true, scheduled: true, in_session: true, protections: {} })
  mocks.portfolioOverview.mockResolvedValue({ cash: 16133.65, nav: 101766.7, open_risk: 3706, positions: [], events: [], nav_daily: [] })
  mocks.portfolioLive.mockResolvedValue({
    positions: [{
      symbol: 'LITE', qty: 9, setup_type: 'breakout', card_id: 'opp-2026-09-28-LITE-breakout-817fed3b',
      entry_date: '2026-10-01', entry_price: 1010.52, avg_cost: 1010.63, last_price: 1048.9, price_source: 'tickflow',
      price_age_s: 6.2, market_value: 9440.1, unrealized_pnl: 344.43, unrealized_pct: 3.787, r_now: 0.38,
      stop_price: 911.99, stop_distance_pct: -13.05, target_zone: [1360.86, 1388.63], target_distance_pct: 29.74,
      days_held: 2, max_holding_days: 7, partial_taken: false,
    }],
    closed: [{
      symbol: 'BBB', setup_type: 'pullback', entry_date: '2026-09-30', entry_price: 20, exit_date: '2026-10-01',
      exit_price: 22.5, qty: 50, partial_exits: 0, exit_reason: 'target_zone_upper', exit_reason_label: '到达目标上沿',
      days_held: 2, fees: 2, pnl: 123, return_pct: 12.29, r_multiple: 2.46,
    }],
    summary: {
      nav: 101766.7, cash: 16133.65, market_value: 85633.05, initial_equity: 100000, total_return_pct: 1.767,
      realized_pnl: 123, unrealized_pnl: 1643.05, closed_trades: 1, win_rate: 1, avg_r: 2.46, open_risk: 3706,
      refresh_seconds: 5, as_of: '2026-10-02T11:00:00-04:00',
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

it('short holdings show the near cover target and closed rounds say buy-to-cover', async () => {
  const base = await mocks.portfolioLive()
  mocks.portfolioLive.mockResolvedValue({
    ...base,
    positions: [{ ...base.positions[0], symbol: 'SHORT', side: 'short', qty: 10, entry_price: 100,
      last_price: 95, market_value: -950, target_zone: [88, 89.76], target_distance_pct: -5.516,
      unrealized_pnl: 50, unrealized_pct: 5 }],
    closed: [{ ...base.closed[0], symbol: 'COVERED', side: 'short', partial_exits: 1 }],
  })
  await render()
  const row = container.querySelector('[data-position="SHORT"]')!
  expect(row.textContent).toContain('$89.76')
  expect(row.textContent).not.toContain('$88.00')
  expect(row.querySelector('[data-side="short"]')!.textContent).toBe('空')
  // 细节点开才显示: 股票式空头的回补负债
  expect(container.querySelector('[data-position-detail="SHORT"]')).toBeNull()
  await act(async () => { (row as HTMLElement).click() })
  expect(container.querySelector('[data-position-detail="SHORT"]')!.textContent).toContain('回补负债 $950')
  expect(container.querySelector('[data-closed="COVERED"]')?.textContent).toContain('分 2 次')
})

it('contract-style holdings show notional (no cover liability), risk and margin (2026-10-10)', async () => {
  const base = await mocks.portfolioLive()
  mocks.portfolioLive.mockResolvedValue({
    ...base,
    positions: [{ ...base.positions[0], symbol: 'MU', side: 'short', qty: 38, entry_price: 99.6, last_price: 98,
      market_value: 60.8, notional: 3724, accounting: 'contract', risk_pct: 0.0025, entry_tier: 'trial',
      unrealized_pnl: 60.8, unrealized_pct: 1.6 }],
    summary: { ...base.summary, margin_used: 1862, buying_power: 98200 },
  })
  await render()
  const row = container.querySelector('[data-position="MU"]')!
  await act(async () => { (row as HTMLElement).click() })
  const detail = container.querySelector('[data-position-detail="MU"]')!
  expect(detail.textContent).toContain('空 38 股 · 名义 $3,724')
  expect(detail.textContent).not.toContain('回补负债')
  expect(detail.querySelector('[data-risk]')!.textContent).toBe('0.25% · 试用')
  expect(container.querySelector('[data-margin]')!.textContent).toBe('保证金 $1,862')
  expect(container.querySelector('[data-kpi="buying"]')!.textContent).toContain('$98,200')
})

it('refreshes holdings every 5 seconds', () => {
  expect(LIVE_REFRESH_MS).toBe(5000)
})

it('shows entry price, live price, P&L, distances and days held for each position', async () => {
  await render()
  const row = container.querySelector('[data-position="LITE"]')!
  const t = row.textContent ?? ''
  expect(t).toContain('$1,010.52')
  expect(t).toContain('$1,048.90')
  expect(t).toContain('+$344.43')
  expect(t).toContain('+3.79%')
  expect(t).toContain('$911.99')
  expect(t).toContain('距 13.1%')     // 距离一律相对现价、取绝对值, 方向由列名表达
  expect(t).toContain('$1,360.86')
  expect(t).toContain('2/7')
  expect(t).toContain('突破')
  expect(t).toContain('实时 6s')
  expect(t).toContain('+0.38R')
})

it('lists closed trades with entry, exit, return and reason', async () => {
  await render()
  const row = container.querySelector('[data-closed="BBB"]')!
  const cells = [...row.querySelectorAll('td')].map((td) => td.textContent)
  expect(cells).toContain('$20.00')
  expect(cells).toContain('$22.50')
  expect(cells).toContain('+12.29%')
  expect(cells).toContain('+$123.00')
  expect(cells).toContain('+2.46R')
  expect(row.textContent).toContain('到达目标')
  expect(row.textContent).not.toContain('上沿')
})

it('summary shows total return, realized and unrealized P&L, win rate', async () => {
  await render()
  const kpi = (k: string) => container.querySelector(`[data-kpi="${k}"]`)!.textContent ?? ''
  expect(kpi('total')).toContain('+1.77%')
  expect(kpi('realized')).toContain('+$123')
  expect(kpi('unrealized')).toContain('+$1,643')
  expect(kpi('nav')).toContain('$101,767')
  expect(container.querySelector('[data-closed-stats]')!.textContent).toContain('胜率 100%')
})

it('0.26.0: summary splits win rate by scope (board cards vs legacy rules)', async () => {
  const base = await mocks.portfolioLive()
  mocks.portfolioLive.mockResolvedValue({ ...base, summary: { ...base.summary, by_scope: [
    { key: 'swing', label: '机会板', closed_trades: 1, win_rate: 1, avg_r: 0.8 },
    { key: 'legacy', label: '旧规则', closed_trades: 12, win_rate: 0.667, avg_r: 0.26 },
  ] } })
  await render()
  const stats = () => container.querySelector('[data-closed-stats]')!.textContent
  await act(async () => { (container.querySelector('[data-scope="swing"]') as HTMLButtonElement).click() })
  expect(stats()).toContain('胜率 100% · 平均 +0.80R')
  await act(async () => { (container.querySelector('[data-scope="legacy"]') as HTMLButtonElement).click() })
  expect(stats()).toContain('胜率 67% · 平均 +0.26R')
})

it('shows the Alpaca paper mirror with per-order deviation and a kill switch', async () => {
  ;(mocks as unknown as Record<string, ReturnType<typeof vi.fn>>).alpacaMirror = vi.fn()
  const { api } = await import('@/lib/api')
  ;(api as unknown as Record<string, unknown>).alpacaMirror = vi.fn().mockResolvedValue({
    summary: { enabled: true, started_seq: 30, orders: 1, filled: 1, by_status: { filled: 1 }, avg_deviation_bps: 10,
      worst_deviation_bps: 10, positions: { AAA: 100 }, last_error: null, max_orders_per_day: 30, rules: '规则' },
    orders: [{ cid: 'zt-31', seq: 31, symbol: 'AAA', side: 'buy', status: 'filled', ledger_qty: 100, ledger_price: 50,
      ledger_kind: 'fill', qty: 100, filled_qty: 100, filled_price: 50.05, deviation_bps: 10 }],
  })
  await render()
  await act(async () => { (container.querySelector('[data-hist="log"]') as HTMLButtonElement).click() })   // 记录在第二个页签
  expect(container.querySelector('[data-mirror]')).toBeNull()   // 镜像收在「记录」第二个页签
  await act(async () => { (container.querySelector('[data-tab="mirror"]') as HTMLButtonElement).click() })
  const t = container.querySelector('[data-mirror]')?.textContent ?? ''
  expect(t).toContain('Alpaca 模拟镜像 · 运行中')
  expect(t).toContain('内部 $50.00 → Alpaca $50.05')
  expect(t).toContain('+10 基点')   // ui-audit-1011: bps 改中文基点
  expect(t).toContain('停用')
  expect(t).not.toContain('一键')
})

it('holding source shows the Chinese rule name with direction, raw id only as tooltip / fallback (2026-10-11)', async () => {
  const base = await mocks.portfolioLive()
  mocks.portfolioLive.mockResolvedValue({
    ...base,
    positions: [
      { ...base.positions[0], symbol: 'ADI', side: 'short', setup_type: 'strategy:tv_ut_bot', setup_label: 'UT Bot 翻空（空）' },
      { ...base.positions[0], symbol: 'ZZZ', side: 'long', setup_type: 'strategy:unknown_rule_x', setup_label: null },
    ],
  })
  await render()
  const adi = container.querySelector('[data-position="ADI"] [data-setup]')!
  expect(adi.textContent).toBe('UT Bot 翻空（空）')
  expect(adi.getAttribute('title')).toBe('strategy:tv_ut_bot')
  expect(container.querySelector('[data-position="ADI"]')!.textContent).not.toContain('strategy:tv_ut_bot')
  expect(container.querySelector('[data-position="ZZZ"] [data-setup]')!.textContent).toBe('strategy:unknown_rule_x')
})
