// @vitest-environment jsdom
// 0.53.0 持仓页「账本表格」: 排序 / 提醒标签 / 待开盘平仓 / 出场原因短词 / 口径筛选 / 台账止损从→到 / 手机卡片
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  portfolioOverview: vi.fn(), portfolioLive: vi.fn(), tradingStatus: vi.fn(), setupStatus: vi.fn(), alpacaMirror: vi.fn(),
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

const AT = '2026-11-05T10:05:00-05:00'
const pos = (o: Record<string, unknown>) => ({
  qty: 10, setup_type: 'strategy:swing', setup_label: '机会板（多）', card_id: 'opp-x-book-swing-1', entry_date: '2026-11-03',
  price_source: 'last_close', price_age_s: null, accounting: 'contract', risk_pct: 0.005, entry_tier: 'rule',
  max_holding_days: 5, partial_taken: false, stop_moves: [], market_value: 0, notional: 1000,
  ...o,
})

function setMatchMedia(desktop: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: desktop, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }))
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  setMatchMedia(true)
  mocks.setupStatus.mockResolvedValue({ setups: [] })
  mocks.alpacaMirror.mockResolvedValue({ summary: { enabled: true, started_seq: 0, orders: 0, filled: 0, by_status: {}, avg_deviation_bps: null,
    worst_deviation_bps: null, positions: {}, last_error: null, max_orders_per_day: 30, rules: '规则' }, orders: [] })
  mocks.tradingStatus.mockResolvedValue({ booted: true, scheduled: true, in_session: false, notifications: { label: '成功' },
    protections: { earnings_auto: 'connected', corporate_actions_auto: 'connected', earnings_calendar: { source: 'nasdaq', fetched_at: AT } } })
  mocks.portfolioOverview.mockResolvedValue({
    cash: 101000, nav: 102000, open_risk: 400, positions: [], nav_daily: [],
    events: [
      { seq: 10, at: '2026-11-03T09:46:00-05:00', kind: 'fill', symbol: 'HOLD', side: 'buy', qty: 10, price: 100, stop_price: 95 },
      { seq: 11, at: '2026-11-04T10:05:00-05:00', kind: 'stop_move', symbol: 'HOLD', stop_price: 100, position_side: 'long', reason: 'trailing_1.0R' },
      { seq: 12, at: '2026-11-04T16:00:00-05:00', kind: 'exit_intent', symbol: 'EXITME', reason: 'invalidation', signal_price: 48 },
      { seq: 13, at: AT, kind: 'close', symbol: 'GONE', qty: 5, price: 20, r_multiple: -2.1, reason: 'invalidation', position_side: 'short' },
    ],
  })
  mocks.portfolioLive.mockResolvedValue({
    positions: [
      // 原始顺序: 普通 → 近目标 → 近止损 → 到期 → 待开盘平仓; 期望页面重排
      pos({ symbol: 'HOLD', side: 'long', entry_price: 100, avg_cost: 100, last_price: 104, stop_price: 100, stop_distance_pct: -3.85,
        target_zone: [112, 112], target_distance_pct: 7.7, days_held: 2, unrealized_pnl: 40, unrealized_pct: 4, r_now: 0.8 }),
      pos({ symbol: 'NEARTGT', side: 'long', entry_price: 50, avg_cost: 50, last_price: 59.5, stop_price: 55, stop_distance_pct: -7.56,
        target_zone: [60, 60], target_distance_pct: 0.84, days_held: 3, unrealized_pnl: 95, unrealized_pct: 19, r_now: 1.9 }),
      pos({ symbol: 'NEARSTOP', side: 'long', entry_price: 80, avg_cost: 80, last_price: 78.5, stop_price: 78, stop_distance_pct: -0.64,
        target_zone: [90, 90], target_distance_pct: 14.6, days_held: 1, unrealized_pnl: -15, unrealized_pct: -1.9, r_now: -0.75 }),
      pos({ symbol: 'DUE', side: 'short', setup_label: '机会板（空）', entry_price: 30, avg_cost: 30, last_price: 28, stop_price: 30,
        stop_distance_pct: 7.1, target_zone: [25, 26], target_distance_pct: -7.1, days_held: 5, unrealized_pnl: 20, unrealized_pct: 6.7, r_now: 1.0 }),
      pos({ symbol: 'EXITME', side: 'long', entry_price: 50, avg_cost: 50, last_price: 47.9, stop_price: 48, stop_distance_pct: 0.21,
        target_zone: [60, 60], target_distance_pct: 25, days_held: 2, unrealized_pnl: -21, unrealized_pct: -4.2, r_now: -1.05 }),
    ],
    closed: [
      { symbol: 'GONE', side: 'short', card_id: 'opp-1-GONE-book-swing-short-a', entry_date: '2026-11-03', entry_price: 18, exit_date: '2026-11-05',
        exit_price: 20, qty: 5, partial_exits: 0, exit_reason: 'invalidation', exit_reason_label: '跌破失效位', days_held: 3, fees: 2, pnl: -12, return_pct: -11.1, r_multiple: -2.1 },
      { symbol: 'OLD', side: 'long', card_id: 'opp-0-OLD-breakout-b', entry_date: '2026-10-20', entry_price: 10, exit_date: '2026-10-24',
        exit_price: 11, qty: 100, partial_exits: 1, exit_reason: 'time_stop', exit_reason_label: '持有到期时间止损', days_held: 5, fees: 3, pnl: 97, return_pct: 9.7, r_multiple: 0.97 },
    ],
    summary: {
      nav: 102000, cash: 101000, market_value: 119, initial_equity: 100000, total_return_pct: 2.0, realized_pnl: 1881, unrealized_pnl: 119,
      closed_trades: 2, win_rate: 0.5, avg_r: -0.565, open_risk: 400, refresh_seconds: 5, as_of: AT, margin_used: 2500, buying_power: 97000,
      by_scope: [
        { key: 'swing', label: '机会板', closed_trades: 1, win_rate: 0, avg_r: -2.1 },
        { key: 'legacy', label: '旧规则', closed_trades: 1, win_rate: 1, avg_r: 0.97 },
      ],
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
  vi.unstubAllGlobals()
})

const tags = (sym: string) => [...container.querySelectorAll(`[data-position="${sym}"] [data-tag]`)].map((t) => [t.textContent, t.getAttribute('data-tag')])

it('持仓按 待开盘平仓/到期 → 近止损 → 近目标 → 其余 排序 (同级保持原顺序)', async () => {
  await render()
  const order = [...container.querySelectorAll('tr[data-position]')].map((r) => r.getAttribute('data-position'))
  expect(order).toEqual(['DUE', 'EXITME', 'NEARSTOP', 'NEARTGT', 'HOLD'])
})

it('提醒标签: 到期/近止损/待开盘平仓 琥珀; 近目标/锁盈 红点; 保本 中性', async () => {
  await render()
  expect(tags('DUE')).toEqual([['今日到期', 'warn'], ['保本', 'plain']])
  expect(tags('NEARSTOP')).toEqual([['近止损', 'warn']])
  expect(tags('NEARTGT')).toEqual([['近目标', 'bull'], ['锁盈 +$50', 'bull']])
  expect(tags('HOLD')).toEqual([['保本', 'plain']])
  // exit_intent 未被 close 消掉 → 待开盘平仓 (原因短词)
  expect(tags('EXITME')[0]).toEqual(['待开盘平仓 · 止损', 'warn'])
})

it('组合风险格用琥珀而不是红色; 距止损/距目标为相对现价的绝对值', async () => {
  await render()
  const risk = container.querySelector('[data-kpi="risk"]')!
  expect(risk.querySelector('.text-warning')!.textContent).toBe('$400')
  expect(risk.querySelector('.text-danger')).toBeNull()
  const row = container.querySelector('tr[data-position="NEARSTOP"]')!
  expect(row.textContent).toContain('距 0.6%')
  expect(row.querySelector('.text-warning')!.textContent).toBe('距 0.6%')
  expect(container.querySelector('tr[data-position="DUE"]')!.textContent).toContain('$26.00')   // 空头取先触及的上沿
})

it('点行展开细节 (止损轨迹/打到止损/到目标), 再点收起', async () => {
  await render()
  const row = container.querySelector('tr[data-position="HOLD"]') as HTMLElement
  expect(container.querySelector('[data-position-detail]')).toBeNull()
  await act(async () => { row.click() })
  const d = container.querySelector('[data-position-detail="HOLD"]')!
  expect(d.textContent).toContain('打到止损$0')
  expect(d.textContent).toContain('到目标+$120')
  expect(d.querySelector('[data-stop-trail]')).not.toBeNull()
  expect(row.getAttribute('aria-expanded')).toBe('true')
  await act(async () => { row.click() })
  expect(container.querySelector('[data-position-detail]')).toBeNull()
})

it('已平仓: 出场原因按原因码给短词 (空头也写止损, 不写跌破); 亏超 1.5R 琥珀; 口径按卡 id 筛选', async () => {
  await render()
  const gone = container.querySelector('[data-closed="GONE"]')!
  expect(gone.textContent).toContain('止损')
  expect(gone.textContent).not.toContain('跌破')
  expect(gone.querySelector('[data-tag="warn"]')!.textContent).toBe('亏超 1.5R')
  const old = container.querySelector('[data-closed="OLD"]')!
  expect(old.textContent).toContain('到期')
  expect(old.textContent).not.toContain('时间止损')
  expect(old.textContent).toContain('分 2 次')
  expect(container.querySelector('[data-scope="swing"]')!.textContent).toBe('机会板 1')
  await act(async () => { (container.querySelector('[data-scope="swing"]') as HTMLButtonElement).click() })
  expect([...container.querySelectorAll('[data-closed]')].map((r) => r.getAttribute('data-closed'))).toEqual(['GONE'])
  expect(container.querySelector('[data-closed-stats]')!.textContent).toBe('胜率 0% · 平均 -2.10R · 合计 -$12')
})

it('台账: 止损调整写从→到, 平仓带 R 与原因短词, 按美东日期分组', async () => {
  await render()
  expect(container.querySelector('[data-event="11"]')!.textContent).toContain('$95.00 → $100.00')
  expect(container.querySelector('[data-event="11"]')!.textContent).toContain('浮盈 1.0R')
  const close = container.querySelector('[data-event="13"]')!.textContent ?? ''
  expect(close).toContain('-2.10R')
  expect(close).toContain('止损')
  const days = [...container.querySelectorAll('tbody tr td[colspan="5"]')].map((td) => td.textContent)
  expect(days).toEqual(['11-05 周四', '11-04 周三', '11-03 周二'])
})

it('状态一行胶囊: 正常时无红色告警框', async () => {
  await render()
  expect([...container.querySelectorAll('[data-chip]')].map((c) => c.getAttribute('data-chip'))).toEqual(
    ['paper', 'session', 'price', 'earnings', 'corp', 'notify'])
  expect(container.querySelector('[data-chip="price"]')!.textContent).toBe('价格 最近收盘')
  expect(container.querySelector('[data-alerts]')).toBeNull()
})

it('手机 (<768px) 持仓与已平仓改卡片, 不渲染宽表', async () => {
  setMatchMedia(false)
  await render()
  expect(container.querySelectorAll('tr[data-position]')).toHaveLength(0)
  const cards = [...container.querySelectorAll('div[data-position]')]
  expect(cards.map((c) => c.getAttribute('data-position'))).toEqual(['DUE', 'EXITME', 'NEARSTOP', 'NEARTGT', 'HOLD'])
  expect(cards[2].textContent).toContain('止损 · 距')
  expect(container.querySelectorAll('div[data-closed]')).toHaveLength(2)
})
