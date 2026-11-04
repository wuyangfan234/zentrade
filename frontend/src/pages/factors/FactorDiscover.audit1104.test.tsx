// @vitest-environment jsdom
// 11-04 因子页审计: 发现页按钮 / 状态 / 计数与真实行为一致 (修复前全部失败)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage, IntakeSubmission } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({
  factorLabPage: vi.fn(), factorLabDecide: vi.fn(), intakeMerge: vi.fn(), intakeIgnore: vi.fn(),
  intakeRestore: vi.fn(), intakeRun: vi.fn(), intakeRetry: vi.fn(), intakeProcess: vi.fn(), factorLabRecalc: vi.fn(),
}))
const toasts = vi.hoisted(() => ({ calls: [] as [string, string | undefined][] }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/Toast', () => ({ toast: (msg: string, kind?: string) => { toasts.calls.push([msg, kind]) } }))
import { FactorDiscover } from './FactorDiscover'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); toasts.calls.length = 0 })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const base = fixture as unknown as FactorLabPage
const clone = (): FactorLabPage => JSON.parse(JSON.stringify(base))
const tick = async () => { for (let i = 0; i < 12; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
async function render(p: FactorLabPage) {
  mocks.factorLabPage.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorDiscover onGo={() => {}} /></QueryClientProvider>) })
  await tick()
  return qc
}
const btn = (scope: Element, text: string) => [...scope.querySelectorAll('button')].find(b => b.textContent === text) as HTMLButtonElement | undefined

function submission(p: FactorLabPage, extra: Partial<IntakeSubmission> = {}): IntakeSubmission {
  const f = p.intake!.cards[0].formulas[0]
  return { id: 'm1', title: 'manual pending', link: '', published: '2026-11-03T08:52:19+00:00', stage: 'pending', reason: null,
    failure: null, duplicate: false, attempts: 1, retry_count: 0, model_text: '', formulas: [f], ...extra }
}

it('submission 看回测 opens the detail and never merges', async () => {
  const p = clone()
  p.intake!.submissions = [submission(p)]
  await render(p)
  const open = host.querySelector('[data-submission="m1"] [data-act="open"]') as HTMLButtonElement
  expect(open.textContent).toBe('看回测')
  await act(async () => { open.click() })
  await tick()
  expect(mocks.intakeMerge).not.toHaveBeenCalled()
  expect(host.querySelector('[data-detail-name]')?.textContent).toBe(p.intake!.cards[0].formulas[0].name)
})

it('a multi-formula item lists every formula and merges the chosen index, never a duplicate', async () => {
  const p = clone()
  const card = p.intake!.cards[0]
  const second = JSON.parse(JSON.stringify(card.formulas[0]))
  card.formulas[0].name = '第一公式（重复）'; card.formulas[0].screen!.duplicate = true
  second.name = '第二公式（可并入）'; second.screen!.duplicate = false
  card.formulas.push(second); card.stage = 'pending'
  mocks.intakeMerge.mockResolvedValue({ id: 'uf_x', status: 'watch' })
  await render(p)
  const view = host.querySelector(`[data-intake="${card.id}"]`)!
  expect(view.textContent).toContain('第一公式（重复）')
  expect(view.textContent).toContain('第二公式（可并入）')
  await act(async () => { btn(view, '并入因子库')!.click() })
  await tick()
  expect(mocks.intakeMerge).toHaveBeenCalledWith(card.id, 1)
})

it('save that fails to create a candidate does not claim success', async () => {
  const p = clone()
  mocks.factorLabDecide.mockRejectedValue(Object.assign(new Error('没能加入候选方案 · RuntimeError'), { status: 409 }))
  await render(p)
  const card = host.querySelector('[data-cand]')!
  await act(async () => { btn(card, '保存')!.click() })
  await tick()
  expect(toasts.calls.some(([m]) => m.includes('已保存'))).toBe(false)
})

it('a saved candidate can be discarded and the toast says the candidate is withdrawn', async () => {
  const p = clone()
  const sig = p.week!.candidates[0].signature
  p.decisions[`${p.week!.week}|${sig}`] = { action: 'save', at: '2026-11-04T10:00:00+08:00', candidate_id: 'rc_1' }
  mocks.factorLabDecide.mockResolvedValue({ removed_candidate_id: 'rc_1' })
  await render(p)
  const discard = host.querySelector('[data-detail] [data-act="discard"]') as HTMLButtonElement
  expect(discard.disabled).toBe(false)
  await act(async () => { discard.click() })
  await tick()
  expect(mocks.factorLabDecide).toHaveBeenCalledWith(p.week!.week, sig, 'discard')
  expect(toasts.calls.some(([m]) => m.includes('撤销'))).toBe(true)
})

it('observe-only OOS pass is labelled 观察通过 with a note', async () => {
  const p = clone()
  ;(p.week!.candidates[0] as { oos_gate?: string }).oos_gate = 'observe'
  ;(p.week!.funnel as { oos_passed?: number }).oos_passed = 1
  await render(p)
  const flow = host.querySelector('[data-flow]')!.textContent!
  expect(flow).toContain('观察通过')
  expect(host.querySelector('[data-cand]')!.textContent).toContain('观察通过')
  expect(host.querySelector('[data-detail]')!.textContent).toMatch(/只过样本外夏普|未过完整门槛/)
})

it('validation step counts OOS passes before the watchlist backtest', async () => {
  const p = clone()
  ;(p.week!.funnel as { oos_passed?: number }).oos_passed = 3
  p.week!.funnel.passed = 1
  await render(p)
  const steps = [...host.querySelectorAll('[data-flow] [data-step]')]
  expect(steps[3].textContent).toContain('3')
})

it('a late recalc response for A is dropped after switching to the engine', async () => {
  const p = clone()
  let resolveA!: (v: unknown) => void
  mocks.factorLabRecalc.mockImplementationOnce(() => new Promise(r => { resolveA = r }))
  await render(p)
  await act(async () => { btn(host.querySelector('[data-detail]')!, '重算')!.click() })
  const refCard = host.querySelector('[data-ref] > div') as HTMLElement
  await act(async () => { refCard.click() })
  await tick()
  await act(async () => { resolveA({ total_return: 9.99, annual_return: 0.777, yearly: {} }) })
  await tick()
  expect(host.querySelector('[data-detail-name]')!.textContent).toBe('当前出卡引擎')
  expect(host.querySelector('[data-detail]')!.textContent).not.toContain('+77.7%')
})

it('recalc replaces curve and trades, not just the metric grid', async () => {
  const p = clone()
  const dates = p.week!.curves.dates as string[]
  mocks.factorLabRecalc.mockResolvedValue({
    total_return: 0.5, annual_return: 0.777, yearly: {},
    curve: { dates, values: dates.map(() => 3.21) },
    last_trades: [{ symbol: 'ZZZZ', entry: '2026-01-02', exit: '2026-01-05', pnl_pct: 0.01 }],
    checks: [[1, 'trades', 99]],
  })
  await render(p)
  await act(async () => { btn(host.querySelector('[data-detail]')!, '重算')!.click() })
  await tick()
  const detail = host.querySelector('[data-detail]')!.textContent!
  expect(detail).toContain('ZZZZ')
  expect(host.querySelector('[data-legend]')!.textContent).toContain('3.21')
})

it('section read errors render as 读取失败, not as zero', async () => {
  const p = clone()
  p.intake = null
  p.errors = { intake: 'OSError' }
  await render(p)
  expect(host.querySelector('[data-funnel]')!.textContent).toContain('读取失败')
  expect(host.textContent).not.toContain('0 源')
})

it('t check follows the factor direction', async () => {
  const p = clone()
  const f = p.intake!.cards[0].formulas[0]
  f.direction = 'high'; f.screen!.t = -3
  await render(p)
  const view = host.querySelector(`[data-intake="${p.intake!.cards[0].id}"]`)!
  await act(async () => { (host.querySelector(`[data-intake="${p.intake!.cards[0].id}"] [data-act="open"]`) as HTMLButtonElement).click() })
  await tick()
  const row = [...host.querySelectorAll('[data-detail] div')].find(d => d.textContent?.startsWith('!') && d.textContent.includes('t ≥ 2'))
  expect(row).toBeTruthy()
  expect(view).toBeTruthy()
})

it('daily limit 0 says model calls are off, not 明天自动继续', async () => {
  const p = clone()
  p.intake!.usage.limit = 0
  p.intake!.settings.daily_limit = 0
  p.intake!.submissions = []
  await render(p)
  const mine = host.querySelector('[data-mine]')!.textContent!
  expect(mine).toContain('模型调用已停用')
  expect(mine).not.toContain('明天自动继续')
})

it('reference count is 0 when the engine is absent', async () => {
  const p = clone()
  p.week!.engine = null
  await render(p)
  expect(host.querySelector('[data-ccount]')!.textContent).not.toContain('1 对照')
})

it('intake 看回测 works without a weekly run', async () => {
  const p = clone()
  p.week = null
  await render(p)
  const open = host.querySelector(`[data-intake="${base.intake!.cards[0].id}"] [data-act="open"]`) as HTMLButtonElement
  await act(async () => { open.click() })
  await tick()
  expect(host.querySelector('[data-detail-name]')?.textContent).toBe(base.intake!.cards[0].formulas[0].name)
})

it('screen error submission is a retryable failure, not 待审', async () => {
  const p = clone()
  const f = { ...p.intake!.cards[0].formulas[0], screen: { error: 'RuntimeError' } }
  p.intake!.submissions = [submission(p, { stage: 'archived', reason: '快筛没跑成 · RuntimeError', failure: 'screen_error', formulas: [f] })]
  await render(p)
  const row = host.querySelector('[data-submission="m1"]')!
  expect(row.textContent).toContain('快筛没跑成')
  expect(btn(row, '重试')).toBeTruthy()
  expect(btn(row, '并入因子库')).toBeUndefined()
})

it('recalc does not mutate the cached weekly curves', async () => {
  const p = clone()
  const dates = p.week!.curves.dates as string[]
  const engineBefore = JSON.stringify(p.week!.curves.engine)
  mocks.factorLabRecalc.mockResolvedValue({ total_return: 0.5, annual_return: 0.7, yearly: {}, curve: { dates: dates.slice(0, 3), values: [1, 2, 3] } })
  const qc = await render(p)
  const refCard = host.querySelector('[data-ref] > div') as HTMLElement
  await act(async () => { refCard.click() })
  await tick()
  await act(async () => { btn(host.querySelector('[data-detail]')!, '重算')!.click() })
  await tick()
  const cached = qc.getQueryData(['factor-lab-page']) as FactorLabPage
  expect(JSON.stringify(cached.week!.curves.engine)).toBe(engineBefore)
})
