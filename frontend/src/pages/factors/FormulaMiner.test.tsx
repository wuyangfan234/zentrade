// @vitest-environment jsdom
// 公式挖掘面板: 纯函数口径 + 渲染结构 (在席 / 已退席 / 开关 / 立即挖掘)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FormulaMinerRow, FormulaMinerView } from '@/lib/api'

const mocks = vi.hoisted(() => ({ formulaMiner: vi.fn(), setFormulaMiner: vi.fn(), runFormulaMiner: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FormulaMinerPanel, forwardText, rowTitle, runLine } from './FormulaMiner'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

const row = (id: string, extra: Partial<FormulaMinerRow> = {}): FormulaMinerRow => ({
  id, name: `公式 ${id}`, formula: '(close/ts_min(low,10)-1)', direction: 'low', status: 'bench', admitted_day: '2026-11-18', sat_days: 3,
  replay: { mining: { n: 732, excess: 0.0071 }, holdout: { n: 400, excess: -0.02 } },
  forward: { hits: 12, filled: 6, days: 3, excess: null, ci: null, verdict: 'thin' }, ...extra,
})
const view = (extra: Partial<FormulaMinerView> = {}): FormulaMinerView => ({
  enabled: true, running: false, pool_ready: true,
  bench: [row('a'), row('b', { forward: { hits: 80, filled: 44, days: 26, excess: 0.31, ci: [0.05, 0.6], verdict: 'above' } })],
  selected: [row('s', { status: 'selected', forward: { hits: 90, filled: 50, days: 30, excess: 0.2, ci: [0.01, 0.4], verdict: 'above' } })],
  retired: [row('r', { status: 'retired', reason: 'below', retired_day: '2026-12-20', forward: { hits: 70, filled: 40, days: 22, excess: -0.3, ci: [-0.5, -0.1], verdict: 'below' } })],
  totals: { tried: 120, admitted: 4, retired: 1, selected: 1, above: 2, outcomes: { eligible: 81, admitted: 4 } },
  caps: { bench: 6, per_run: 2, tenure_days: 60, need_days: 20, need_filled: 15 },
  last_run: { run_id: 'fm-2026-11-18-1', day: '2026-11-18', funnel: { generated: 120, new: 119, evaluated: 119, errors: 1, thin: 0, same_as_alive: 36, same_as_retired: 0, eligible: 82, admitted: 2 } },
  runs: [], ...extra,
})

const flush = async () => { for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
async function mount(v: FormulaMinerView) {
  mocks.formulaMiner.mockResolvedValue(v)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><FormulaMinerPanel /></QueryClientProvider>) })
  await flush()
}
const q = (sel: string) => host.querySelector(sel)

it('forward column shows progress until there are enough signal days', () => {
  expect(forwardText(row('a'), 20)).toBe('3/20')
  expect(forwardText(row('b', { forward: { days: 26, filled: 44, excess: 0.31, verdict: 'above' } }), 20)).toBe('+0.31')
  expect(forwardText(row('c', { forward: { days: 22, filled: 40, excess: -0.3, verdict: 'below' } }), 20)).toBe('−0.30')
})

it('row title carries sample sizes and keeps holdout replay out of the main layer', () => {
  const t = rowTitle(row('a'), 20)
  expect(t).toContain('已坐 3 个交易日')
  expect(t).toContain('满 20 个信号日后评定')
  expect(t).toContain('回放留出段 −0.02 R / 400 笔')
})

it('run line is plain numbers or a plain reason', () => {
  expect(runLine(null)).toBe('尚未运行')
  expect(runLine(view().last_run)).toBe('11-18 · 生成 120 · 新公式 119 · 可用 82 · 入席 2')
  expect(runLine({ run_id: 'x', day: '2026-11-19', skipped: 'bench_full' })).toBe('11-19 · 挖掘席位已满')
  expect(runLine({ run_id: 'x', day: '2026-11-19', error: 'Boom' })).toBe('11-19 · 运行失败')
})

it('renders live rows first, retired below, with Chinese states only', async () => {
  await mount(view())
  expect(q('[data-miner-state]')!.textContent).toBe('运行中')
  const facts = q('[data-miner-facts]')!.textContent!
  expect(facts).toContain('2 / 6')
  expect(facts).toContain('120 条')
  expect(q('[data-miner-rules]')!.textContent).toContain('回放为负不入席')
  const rows = [...host.querySelectorAll('[data-miner-row]')]
  expect(rows.map((r) => r.getAttribute('data-miner-row'))).toEqual(['s', 'a', 'b', 'r'])
  expect(rows.map((r) => r.lastElementChild!.textContent)).toEqual(['入选', '攒样本', '领先', '前向落后'])
  expect(rows[1].querySelector('[data-miner-forward]')!.textContent).toBe('3/20')
  expect(rows[1].querySelector('[data-miner-replay]')!.textContent).toBe('+0.01')
  expect(forwardText(row('z', { forward: { days: 25, filled: 30, excess: -0.001, verdict: 'unclear' } }), 20)).toBe('0.00')
  expect(host.textContent).not.toMatch(/bench|thin|below|selected|retired|timeout/)
})

it('switch and run call their own endpoints; run is blocked without a pool', async () => {
  await mount(view())
  mocks.setFormulaMiner.mockResolvedValue(view({ enabled: false }))
  await act(async () => { (q('[data-miner-switch]') as HTMLButtonElement).click() })
  await flush()
  expect(mocks.setFormulaMiner).toHaveBeenCalledWith(false)
  expect(q('[data-miner-state]')!.textContent).toBe('已关闭')
  mocks.runFormulaMiner.mockResolvedValue({ started: true, running: true })
  await act(async () => { (q('[data-miner-run]') as HTMLButtonElement).click() })
  await flush()
  expect(mocks.runFormulaMiner).toHaveBeenCalledTimes(1)
})

it('empty state and missing pool', async () => {
  await mount(view({ bench: [], selected: [], retired: [], pool_ready: false, last_run: null, totals: { tried: 0, admitted: 0, retired: 0, selected: 0, above: 0, outcomes: {} } }))
  expect(host.textContent).toContain('无在席公式')
  expect(host.textContent).toContain('尚未运行')
  expect((q('[data-miner-run]') as HTMLButtonElement).disabled).toBe(true)
})
