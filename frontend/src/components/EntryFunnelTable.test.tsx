// @vitest-environment jsdom
// 准入漏斗表 (2026-10-12): 逐层计数、回补单列、全局拦截在前
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EntryFunnelPayload } from '@/lib/api'

const mocks = vi.hoisted(() => ({ entryFunnel: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { EntryFunnelTable } from './EntryFunnelTable'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

async function render(p: EntryFunnelPayload) {
  mocks.entryFunnel.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><EntryFunnelTable /></QueryClientProvider>) })
  for (let i = 0; i < 20 && !host.querySelector('[data-funnel] table, [data-empty]:not([data-empty="pending"])'); i++) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
}

it('renders each stage, backfill separately, global reasons first', async () => {
  await render({
    as_of: '2026-10-12T16:00',
    days: [
      { day: '2026-10-12', active: 5, touched: 3, blocked: 2, filled: 1, backfill: 0, recorded: true,
        reasons: [{ reason: 'same_sector_cap', label: '同板块上限', n: 2 }],
        global: [{ reason: 'circuit_breaker_day_halt', label: '当日熔断', since: '11:20' }] },
      { day: '2026-10-09', active: 37, touched: 19, blocked: 0, filled: 0, backfill: 19, recorded: false, reasons: [], global: [] },
    ],
  })
  const row = host.querySelector('[data-funnel-day="2026-10-12"]')!
  expect(row.textContent).toContain('10-12')
  const chips = [...row.querySelectorAll('[data-funnel-reasons] [data-reason]')]
  expect(chips.map((c) => c.getAttribute('data-reason'))).toEqual(['circuit_breaker_day_halt', 'same_sector_cap'])
  expect(chips[0].textContent).toBe('全局：当日熔断11:20 起')
  expect(chips[1].textContent).toBe('同板块上限2')
  const old = host.querySelector('[data-funnel-day="2026-10-09"]')!
  expect(old.querySelector('[data-funnel-filled]')?.textContent).toBe('0 +19 回补')
  expect(old.querySelector('[data-funnel-reasons]')?.textContent).toBe('无记录')
})

// ── 0.9.1: 三种空态分开 ──
it('0.9.1 今天没卡: 已记录且激活 0 显示灰色「今天没有卡（正常）」', async () => {
  await render({ as_of: '2026-10-12T16:00', days: [
    { day: '2026-10-12', active: 0, touched: 0, blocked: 0, filled: 0, backfill: 0, recorded: true, reasons: [], global: [] }] })
  const r = host.querySelector('[data-funnel-day="2026-10-12"] [data-funnel-reasons]')!
  expect(r.getAttribute('data-empty')).toBe('none')
  expect(r.textContent).toContain('今天没有卡（正常）')
})
it('0.9.1 数据还没到: 无天数时不隐藏整块, 显示等待', async () => {
  await render({ as_of: '2026-10-12T16:00', days: [] })
  expect(host.querySelector('[data-funnel]')).not.toBeNull()
  expect(host.querySelector('[data-funnel] [data-empty="pending"]')!.textContent).toContain('数据还没到')
})
it('0.9.1 读取失败: 红色带重试', async () => {
  mocks.entryFunnel.mockRejectedValue(new Error('HTTP 500'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><EntryFunnelTable /></QueryClientProvider>) })
  for (let i = 0; i < 10; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const e = host.querySelector('[data-funnel] [data-empty="error"]')!
  expect(e.textContent).toContain('读取失败'); expect(e.textContent).toContain('HTTP 500')
  const n = mocks.entryFunnel.mock.calls.length
  await act(async () => { (e.querySelector('button') as HTMLButtonElement).click() })
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(mocks.entryFunnel.mock.calls.length).toBe(n + 1)
})

// ── 0.13.0: 原因标签化 + 英文原因码补中文 + 超 6 项折叠 ──
it('0.13.0 英文原因码补中文名, 带子项; 超过 6 项默认折叠可展开', async () => {
  const reasons = ['source_not_tradable', 'risk_zero', 'v4_book_only', 'not_in_watchlist', 'source_not_tradable:phantom_4h', 'sector_side_cap', 'mystery_code', '缺当日开盘价']
    .map((r, i) => ({ reason: r, label: r === '缺当日开盘价' ? r : r, n: 100 - i }))
  await render({ as_of: '2026-10-12T16:00', days: [
    { day: '2026-10-12', active: 21, touched: 3, blocked: 2142, filled: 3, backfill: 0, recorded: true, reasons, global: [] }] })
  const cell = host.querySelector('[data-funnel-day="2026-10-12"] [data-funnel-reasons]')!
  expect(cell.querySelectorAll('[data-reason]').length).toBe(6)
  expect(cell.querySelector('[data-reason="risk_zero"]')!.textContent).toBe('风险额为 099')
  expect(cell.querySelector('[data-reason="source_not_tradable:phantom_4h"]')!.textContent).toContain('该来源未开放下单 · phantom_4h')
  const btn = cell.querySelector('[data-reasons-toggle]') as HTMLButtonElement
  expect(btn.textContent).toBe('+2 项')
  await act(async () => { btn.click() })
  expect(cell.querySelectorAll('[data-reason]').length).toBe(8)
  expect(cell.querySelector('[data-reason="mystery_code"]')!.textContent).toContain('mystery_code')
  expect((cell.querySelector('[data-reasons-toggle]') as HTMLButtonElement).textContent).toBe('收起')
})
