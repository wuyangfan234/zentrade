// @vitest-environment jsdom
// 审计复现 (先红): 发现页选择态 / 重算 / 已发布计数
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({ factorLabPage: vi.fn(), factorLabDecide: vi.fn(), intakeMerge: vi.fn(), intakeIgnore: vi.fn(), intakeRestore: vi.fn(), intakeRun: vi.fn(), factorLabRecalc: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FactorDiscover } from './FactorDiscover'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
// jsdom 无 scrollIntoView: 选中卡片后 rAF 里滚到详情, 测试结束后才触发会报未捕获错误
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
let root: Root
let host: HTMLDivElement
const errors: unknown[] = []
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); errors.length = 0 })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const base = fixture as unknown as FactorLabPage
const clone = (): FactorLabPage => JSON.parse(JSON.stringify(base))
const tick = async () => { for (let i = 0; i < 10; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

async function mount(qc: QueryClient) {
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorDiscover onGo={() => {}} /></QueryClientProvider>) })
  await tick()
}

it('selected candidate disappearing after a refetch does not crash the page', async () => {
  mocks.factorLabPage.mockResolvedValue(clone())
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await mount(qc)
  const card = host.querySelector('[data-cand] > div') as HTMLElement
  await act(async () => { card.click() })
  const next = clone()
  next.week!.candidates = []
  next.week!.week = '2026-W45'
  mocks.factorLabPage.mockResolvedValue(next)
  let thrown: unknown = null
  try {
    await act(async () => { await qc.refetchQueries() })
    await tick()
  } catch (e) { thrown = e }
  expect(thrown).toBeNull()
  expect(host.querySelector('[data-discover]')).not.toBeNull()
})

it('recalc result survives a background refetch when nothing was clicked', async () => {
  mocks.factorLabPage.mockResolvedValue(clone())
  mocks.factorLabRecalc.mockResolvedValue({ total_return: 9.99, annual_return: 0.777, yearly: {} })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await mount(qc)
  const btn = [...host.querySelectorAll('[data-detail] button')].find(b => b.textContent === '重算') as HTMLButtonElement
  await act(async () => { btn.click() })
  await tick()
  expect(host.querySelector('[data-detail]')!.textContent).toContain('+77.7%')
  const changed = clone()
  changed.intake!.usage.today += 1 // 60 秒轮询里任意一处数字变化
  mocks.factorLabPage.mockResolvedValue(changed)
  await act(async () => { await qc.refetchQueries() })
  await tick()
  expect(host.querySelector('[data-detail]')!.textContent).toContain('+77.7%')
})

it('published count is not a hard-coded zero', async () => {
  const p = clone()
  p.week!.candidates[0].published_strategy_id = 'mined_factor_x'
  mocks.factorLabPage.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await mount(qc)
  expect(host.querySelector('[data-flow]')!.textContent).toContain('已发布 1')
})

it('engine recalc updates the metric grid and the engine yearly column', async () => {
  mocks.factorLabPage.mockResolvedValue(clone())
  // backend shape for signature=engine (factor_discover._recalc_impl, flat like a candidate recalc)
  mocks.factorLabRecalc.mockResolvedValue({ total_return: 1.234, annual_return: 0.321, n_trades: 999, yearly: { '2024': 0.555 }, legs: [] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await mount(qc)
  await act(async () => { (host.querySelector('[data-ref] > div') as HTMLElement).click() })
  await tick()
  const btn = [...host.querySelectorAll('[data-detail] button')].find(b => b.textContent === '重算') as HTMLButtonElement
  await act(async () => { btn.click() })
  await tick()
  const text = host.querySelector('[data-detail]')!.textContent!
  expect(text).toContain('+32.1%')
  expect(text).toContain('+55.5%')
})

it('custom pool sends the typed symbols and invalid dates disable recalc', async () => {
  mocks.factorLabPage.mockResolvedValue(clone())
  mocks.factorLabRecalc.mockResolvedValue({ annual_return: 0.1, yearly: {} })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await mount(qc)
  const detail = host.querySelector('[data-detail]')!
  const custom = [...detail.querySelectorAll('button')].find(b => b.textContent === '自定义') as HTMLButtonElement
  await act(async () => { custom.click() })
  const recalcBtn = () => [...detail.querySelectorAll('button')].find(b => b.textContent === '重算') as HTMLButtonElement
  expect(recalcBtn().disabled).toBe(true)
  const input = detail.querySelector('input[aria-label="自定义股票"]') as HTMLInputElement
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => { setter.call(input, 'aapl, nvda'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(recalcBtn().disabled).toBe(false)
  await act(async () => { recalcBtn().click() })
  await tick()
  expect(mocks.factorLabRecalc.mock.calls[0][0]).toMatchObject({ pool: 'custom', symbols: ['AAPL', 'NVDA'] })
  const dates = [...detail.querySelectorAll('input')].filter(i => /^\d{4}-/.test((i as HTMLInputElement).value)) as HTMLInputElement[]
  await act(async () => { setter.call(dates[1], '2026-1'); dates[1].dispatchEvent(new Event('input', { bubbles: true })) })
  expect(recalcBtn().disabled).toBe(true)
})
