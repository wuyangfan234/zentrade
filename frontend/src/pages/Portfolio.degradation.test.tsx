// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  portfolioOverview: vi.fn(), tradingStatus: vi.fn(), setupStatus: vi.fn(), restoreSetup: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { Portfolio } from './Portfolio'

let qc: QueryClient
let root: Root
let container: HTMLDivElement

async function flush() {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  mocks.portfolioOverview.mockResolvedValue({ cash: 1, nav: 1, open_risk: 0, positions: [], events: [], nav_daily: [] })
  mocks.tradingStatus.mockResolvedValue({ booted: true, scheduled: true, in_session: false, protections: {} })
  mocks.setupStatus.mockResolvedValue({ setups: [
    { setup: 'breakout', status: 'degraded', degraded_at: '2026-10-04T09:00:00-04:00',
      evidence: { samples: 12, avg_r: -0.41, window_start: '2026-09-07', window_end: '2026-10-04' } },
    { setup: 'pullback', status: 'active' },
  ] })
  mocks.restoreSetup.mockResolvedValue({ setup: { setup: 'breakout', status: 'active' } })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => { root.render(<QueryClientProvider client={qc}><Portfolio /></QueryClientProvider>) })
  await flush()
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  qc.clear()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

it('shows a degraded setup with its evidence', () => {
  const text = container.textContent ?? ''
  expect(text).toContain('突破 已降级为只观察')
  expect(text).toContain('12 笔')
  expect(text).toContain('-0.41R')
  expect(text).not.toContain('回踩 已降级')
})

it('restore requires confirmation and then calls the API', async () => {
  const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true)
  vi.stubGlobal('confirm', confirm)
  const button = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('恢复开仓'))!
  await act(async () => { button.click() })
  expect(mocks.restoreSetup).not.toHaveBeenCalled()
  await act(async () => { button.click() })
  await flush()
  expect(mocks.restoreSetup).toHaveBeenCalledWith('breakout', expect.any(String))
})
