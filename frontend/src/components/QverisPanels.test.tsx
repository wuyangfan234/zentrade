// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  qverisUsage: vi.fn(), qverisOptions: vi.fn(), qverisEstimates: vi.fn(), qverisTranscript: vi.fn(), qverisDigest: vi.fn(),
  qverisSetLimit: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { EarningsPanel, OptionsPanel } from './QverisPanels'
import { QverisUsageCard } from './QverisUsageCard'

const usage = {
  configured: true, day: '2026-10-02', spent_today: 3, daily_limit: 100, by_kind: { options_chain: { calls: 2, credits: 2, failed: 0 } },
  balance: { remaining: 9734.23, error: null }, recent: [{ at: '2026-10-02T15:00:00Z', kind: 'options_chain', symbol: 'NVDA', trigger: 'click', ok: true, charged: 1 }],
}
const analysis = {
  ok: true, spot: 234.8, as_of: '2026-10-01', contracts: 3836, expirations: 24,
  term: [{ expiration: '2026-10-09', dte: 7, atm_strike: 235, atm_iv: 0.2832, straddle: 8.52, expected_move_pct: 0.0363 }],
  pcr: { all: { volume: 0.636, open_interest: 0.867, put_volume: 1, call_volume: 2, put_oi: 1, call_oi: 2 },
         front: { volume: 0.613, open_interest: 0.74, put_volume: 1, call_volume: 2, put_oi: 1, call_oi: 2 }, front_expiration: '2026-10-02' },
  skew: { expiration: '2026-10-09', put25_iv: 0.2978, call25_iv: 0.288, put25_strike: 225, call25_strike: 237.5, risk_reversal: 0.0098 },
  oi_walls: { calls_above: [{ strike: 250, open_interest: 91577 }], puts_below: [{ strike: 200, open_interest: 79706 }], expirations: ['2026-10-02'] },
  unusual: [{ contract: 'NVDA261002P00230000', expiration: '2026-10-02', strike: 230, type: 'put', volume: 196966, open_interest: 22577, ratio: 8.7, iv: 0.317 }],
  earnings_move: { earnings_date: '2026-11-18', expiration: '2026-11-20', expected_move_pct: 0.1038, atm_iv: 0.35 },
}

let qc: QueryClient
let root: Root
let host: HTMLDivElement
async function render(node: React.ReactNode) {
  await act(async () => { root.render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>) })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
const click = async (el: Element) => {
  await act(async () => { (el as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}
const button = (text: string) => [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text)) as HTMLButtonElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.qverisUsage.mockResolvedValue(usage)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear(); vi.resetAllMocks() })

it('options panel reads cache only on open and spends only on click, showing cost and balance', async () => {
  mocks.qverisOptions.mockImplementation(async (sym: string, refresh: boolean) => refresh
    ? { symbol: sym, available: true, fetched_at: '2026-10-02T15:00:00Z', cached: false, charged: 1, fresh: true, cost: 1,
        spot: 234.8, spot_source: '实时报价', analysis, earnings_date: '2026-11-18' }
    : { symbol: sym, available: false, reason: '尚未查询', cost: 1 })
  await render(<OptionsPanel symbol="NVDA" />)
  expect(mocks.qverisOptions).toHaveBeenCalledTimes(1)
  expect(mocks.qverisOptions).toHaveBeenLastCalledWith('NVDA', false)
  expect(host.textContent).toContain('尚未查询该股期权')
  expect(host.textContent).toContain('约 1 积分')
  expect(host.textContent).toContain('今日 3/100')
  expect(host.textContent).toContain('余额 9,734.23')

  await click(button('查询期权'))
  expect(mocks.qverisOptions).toHaveBeenLastCalledWith('NVDA', true)
  const text = host.textContent ?? ''
  expect(text).toContain('3836 个合约')
  expect(text).toContain('±3.6%')            // 7 天预期波动
  expect(text).toContain('±10.4%')           // 财报预期波动
  expect(text).toContain('本次扣 1 积分')
  expect(text).toContain('缓存仍新鲜 · 再点免费')
  expect(text).toContain('不参与开仓')
  expect(host.querySelector('[data-term] tbody tr')?.textContent).toContain('2026-10-09')
})

it('shows quota errors from the server instead of silently failing', async () => {
  mocks.qverisOptions.mockImplementation(async (_s: string, refresh: boolean) => {
    if (refresh) throw new Error('今日 QVeris 积分已用 100/100，本次需约 1，已拒绝')
    return { symbol: 'NVDA', available: false, reason: '尚未查询', cost: 1 }
  })
  await render(<OptionsPanel symbol="NVDA" />)
  await click(button('查询期权'))
  expect(host.textContent).toContain('已拒绝')
})

it('disables paid buttons when no key is configured', async () => {
  mocks.qverisUsage.mockResolvedValue({ ...usage, configured: false, balance: { remaining: null, error: '未配置 Key' } })
  mocks.qverisOptions.mockResolvedValue({ symbol: 'NVDA', available: false, reason: '尚未查询', cost: 1 })
  await render(<OptionsPanel symbol="NVDA" />)
  expect(button('查询期权').disabled).toBe(true)
  expect(host.textContent).toContain('未配置 QVeris Key')
})

it('earnings panel: estimates + transcript with free AI digest', async () => {
  const next = { date: '2026-09-30', horizon: 'fiscal quarter', eps_avg: 4.2083, eps_high: 4.37, eps_low: 4.13, eps_analysts: 22,
    eps_avg_7d_ago: 4.2083, eps_avg_30d_ago: 4.2098, eps_up_7d: 19, eps_down_7d: 0, eps_up_30d: 20, eps_down_30d: 0,
    rev_avg: 1251861620, rev_high: 1261000000, rev_low: 1244900000, rev_analysts: 19, eps_change_30d_pct: -0.0004 }
  mocks.qverisEstimates.mockResolvedValue({ symbol: 'LITE', available: true, fetched_at: '2026-10-02T12:45:00Z', cached: true, charged: 0,
    fresh: true, cost: 1, earnings_date: '2026-11-04',
    estimates: { next, quarters: [next], years: [{ ...next, date: '2027-06-30', horizon: 'fiscal year' }], rows: 41 } })
  mocks.qverisTranscript.mockImplementation(async (_s: string, quarter: string, refresh: boolean) => refresh || mocks.qverisDigest.mock.calls.length
    ? { symbol: 'LITE', quarter, available: true, cost: 1, cached: false, charged: 1, fetched_at: '2026-10-02T15:00:00Z',
        segments: [{ speaker: 'CEO', title: 'Chief Executive', content: 'Demand remains strong.', sentiment: 0.6 }], avg_sentiment: 0.6,
        digest: mocks.qverisDigest.mock.calls.length ? { points: ['需求依然强劲'] } : null }
    : { symbol: 'LITE', quarter, available: false, reason: '尚未查询', cost: 1 })
  mocks.qverisDigest.mockResolvedValue({ symbol: 'LITE', quarter: '2026Q3', digest: { points: ['需求依然强劲'] } })
  await render(<EarningsPanel symbol="LITE" />)
  const text = host.textContent ?? ''
  expect(text).toContain('下次财报 2026-11-04')
  expect(text).toContain('4.2083')
  expect(text).toContain('20 上调 / 0 下调')
  expect(text).toContain('$12.52 亿')
  expect(text).toContain('缓存仍新鲜 · 再点免费')
  expect(mocks.qverisTranscript.mock.calls[0][2]).toBe(false)     // 打开只读缓存

  await click(button('查询电话会'))
  expect(host.textContent).toContain('1 段发言')
  await click(button('AI 提炼要点'))
  expect(mocks.qverisDigest).toHaveBeenCalledWith('LITE', expect.stringMatching(/^\d{4}Q[1-4]$/))
  expect(host.querySelector('[data-digest]')?.textContent).toContain('需求依然强劲')
})

it('usage card shows balance, spend vs limit and lets the user change the limit', async () => {
  mocks.qverisSetLimit.mockResolvedValue({ ...usage, daily_limit: 150 })
  await render(<QverisUsageCard />)
  expect(host.querySelector('[data-balance]')?.textContent).toContain('9,734.23')
  expect(host.querySelector('[data-spent]')?.textContent).toContain('/ 100 上限')
  expect(host.textContent).toContain('期权 2 次 / 2 积分')
  const input = host.querySelector('input') as HTMLInputElement
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(input, '150')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click(button('保存'))
  expect(mocks.qverisSetLimit).toHaveBeenCalledWith(150)
  expect(host.querySelector('[data-spent]')?.textContent).toContain('/ 150 上限')
})
