// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ portfolioOverview: vi.fn(), tradingStatus: vi.fn(), setupStatus: vi.fn() }))
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
  for (let i = 0; i < 3; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.setupStatus.mockResolvedValue({ setups: [] })
  mocks.tradingStatus.mockResolvedValue({
    booted: true, scheduled: true, in_session: false,
    protections: {
      earnings_auto: 'connected', corporate_actions_auto: 'connected',
      earnings_calendar: { status: 'connected', source: 'nasdaq', fetched_at: '2026-09-30T17:10:38.785475-04:00' },
    },
  })
  // Real production NAV series on 2026-10-01: two flat days, then the first positions.
  mocks.portfolioOverview.mockResolvedValue({
    cash: 46264.07, nav: 100648.27, open_risk: 2998.44, positions: [], events: [],
    nav_daily: [
      { date: '2026-09-28', nav: 100000 },
      { date: '2026-09-29', nav: 100000 },
      { date: '2026-09-30', nav: 100659.47 },
    ],
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

it('earnings freshness is shown as an ET clock time, not a raw ISO string', async () => {
  await render()
  const text = container.textContent ?? ''
  expect(text).toContain('财报保护：已接入（来源 nasdaq，更新于 09-30 17:10 ET）')
  expect(text).not.toContain('17:10:38.785475')
})

it('equity curve is a red line through every day with a principal baseline, not bars', async () => {
  await render()
  expect(container.querySelectorAll('[data-nav-bar]')).toHaveLength(0)
  const curve = container.querySelector('[data-equity-curve]') as SVGElement
  expect(curve).not.toBeNull()
  const line = curve.querySelector('[data-equity-line]') as SVGPathElement
  // 每个交易日一个顶点 (M + 2 段 L)
  expect((line.getAttribute('d') ?? '').match(/[ML]/g)).toHaveLength(3)
  expect(line.getAttribute('stroke')).toBe('hsl(var(--bull))')
  expect(curve.querySelectorAll('[data-equity-point]')).toHaveLength(3)
  expect(curve.querySelector('[data-equity-baseline]')).not.toBeNull()
  expect(curve.textContent).toContain('本金 $100,000')
  // 日期空间足够时每个交易日都标, 不漏标
  const labels = [...container.querySelectorAll('[data-equity-label]')].map(x => x.textContent)
  expect(labels).toEqual(['09-28', '09-29', '09-30'])
  // 静止时读数给最新一日: 当日与累计两个口径
  const readout = container.querySelector('[data-equity-readout]')?.textContent ?? ''
  expect(readout).toContain('最新 09-30')
  expect(readout).toContain('$100,659')
  expect(readout).toContain('当日 +0.66%')
  expect(readout).toContain('累计 +0.66%')
  // 只有一个高亮点 (最新)
  expect(curve.querySelectorAll('[data-equity-point][data-active]')).toHaveLength(1)
})

it('hovering a day updates the readout (day vs cumulative) and moves the single highlight', async () => {
  await render()
  const hit = container.querySelector('[data-equity-hit="1"]') as SVGRectElement
  await act(async () => { hit.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })) })
  const readout = container.querySelector('[data-equity-readout]')?.textContent ?? ''
  expect(readout).toContain('09-29')
  expect(readout).not.toContain('最新')
  expect(readout).toContain('当日 +0.00%')
  expect(readout).toContain('累计 +0.00%')
  const active = [...container.querySelectorAll('[data-equity-point][data-active]')]
  expect(active).toHaveLength(1)
  expect(container.querySelector('[data-equity-crosshair]')).not.toBeNull()
})
