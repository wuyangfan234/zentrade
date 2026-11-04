// @vitest-environment jsdom
// 11-04 因子页审计: 信号 / 单因子检验 / 编辑器 / 自动挖掘 回归 (由审计探针改为断言正确行为)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
const mocks = vi.hoisted(() => ({
  customSignalsList: vi.fn(), customSignalSave: vi.fn(), customSignalCreate: vi.fn(),
  factorLibrary: vi.fn(), factorColumns: vi.fn(), factorRun: vi.fn(),
  factorValidate: vi.fn(), factorTrial: vi.fn(), factorCustomCreate: vi.fn(), factorCustomUpdate: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { ApiError, type FactorBatchItem } from '@/lib/api'
import { AddFactorSignalDialog } from './AddFactorSignalDialog'
import { FactorBacktest } from './FactorBacktest'
import { FactorEditor } from '../factors/FactorEditor'

let root: Root
let host: HTMLDivElement
let qc: QueryClient
const flush = async () => { for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
const setValue = async (element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('change', { bubbles: true }))
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await flush()
}
const click = async (element: Element | undefined) => { await act(async () => { (element as HTMLElement).click() }); await flush() }
const btn = (text: string) => [...document.body.querySelectorAll('button')].find(el => (el.textContent ?? '').includes(text)) as HTMLButtonElement | undefined

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  mocks.customSignalsList.mockResolvedValue({ signals: [] })
  mocks.customSignalCreate.mockResolvedValue({ ok: true })
  mocks.factorColumns.mockResolvedValue({ columns: [{ id: 'momentum_20d', label: '20日动量', group: '动量', desc: '' }] })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  document.body.querySelectorAll('[data-modal-root], [role="dialog"]').forEach(node => node.remove())
  qc.clear()
})

it('negative-IC non-negative factor gets no impossible < 0 default', async () => {
  const item = { factor_name: 'annual_vol_20d', label: '20日波动率', group: '波动率', ic_mean: -0.03 } as FactorBatchItem
  await act(async () => root.render(<QueryClientProvider client={qc}><AddFactorSignalDialog item={item} onClose={() => {}} /></QueryClientProvider>))
  await flush()
  const threshold = document.body.querySelector('input[type="number"]') as HTMLInputElement
  expect(threshold.value).toBe('')
  expect(btn('保存信号')!.disabled).toBe(true)
})

it('callback reports the kind that was actually saved', async () => {
  let resolveSave!: (value: { ok: boolean }) => void
  mocks.customSignalCreate.mockImplementation(() => new Promise(resolve => { resolveSave = resolve }))
  mocks.factorLibrary.mockResolvedValue({ factors: [{ id: 'rsi_14', label: 'RSI14', group: '震荡', available: true }] })
  const created = vi.fn()
  await act(async () => root.render(<QueryClientProvider client={qc}><AddFactorSignalDialog defaultKind="entry" onCreated={created} onClose={() => {}} /></QueryClientProvider>))
  await flush()
  await setValue(document.body.querySelector('select') as HTMLSelectElement, 'rsi_14')
  const kindSelect = () => [...document.body.querySelectorAll('select')].at(-1)! as HTMLSelectElement
  await setValue(kindSelect(), 'exit')
  await click(btn('保存信号'))
  // pending 期间类型框被禁用; 就算绕过去改了, 回调也只认提交快照
  expect(kindSelect().disabled).toBe(true)
  await act(async () => resolveSave({ ok: true }))
  await flush()
  expect(mocks.customSignalCreate.mock.calls[0][0].kind).toBe('exit')
  expect(created.mock.calls[0][0].kind).toBe('exit')
})

it('create-only conflict retries with the next id instead of overwriting', async () => {
  mocks.factorLibrary.mockResolvedValue({ factors: [{ id: 'rsi_14', label: 'RSI14', group: '震荡', available: true }] })
  mocks.customSignalsList
    .mockResolvedValueOnce({ signals: [] })
    .mockResolvedValueOnce({ signals: [{ id: 'f_rsi_14_gt' }] })
  mocks.customSignalCreate
    .mockRejectedValueOnce(new ApiError('信号 id 已存在', 409))
    .mockResolvedValueOnce({ ok: true })
  const created = vi.fn()
  await act(async () => root.render(<QueryClientProvider client={qc}><AddFactorSignalDialog onCreated={created} onClose={() => {}} /></QueryClientProvider>))
  await flush()
  await setValue(document.body.querySelector('select') as HTMLSelectElement, 'rsi_14')
  await click(btn('保存信号'))
  expect(mocks.customSignalCreate.mock.calls.map(c => c[0].id)).toEqual(['f_rsi_14_gt', 'f_rsi_14_gt_2'])
  expect(mocks.customSignalSave).not.toHaveBeenCalled()
  expect(created.mock.calls[0][0].id).toBe('f_rsi_14_gt_2')
})

it('result frequency label follows the result, not the form', async () => {
  mocks.factorRun.mockResolvedValue({
    run_id: 'old-monthly', config: { factor_name: 'momentum_20d', rebalance: 'monthly' },
    ic_mean: 0.02, ic_std: 0.01, ir: 2, ic_win_rate: 0.6, ic_series: [],
    group_stats: [], group_nav: [], long_short_stats: {},
    long_short_nav: [], elapsed_ms: 1, n_symbols: 2, n_dates: 2, error: null,
  })
  await act(async () => root.render(<QueryClientProvider client={qc}><FactorBacktest /></QueryClientProvider>))
  await flush()
  await click(btn('开始因子分析'))
  expect(host.textContent).toContain('Rank IC · 月度调仓')
  expect(host.textContent).not.toContain('Rank IC · 日度调仓')
})

it('long/short sharpe cell reads the returned sharpe instead of a fixed dash', async () => {
  // 分组净值图依赖 canvas, jsdom 渲染不了整张表: 直接核源码里那一格的取值
  const { readFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const src = readFileSync(resolve('src/pages/backtest/FactorBacktest.tsx'), 'utf8')
  expect(src).toContain('result.long_short_stats.sharpe.toFixed(2)')
})

// ── 编辑器 ──
const item = { id: 'uf_a', label: '因子A', group: 'A组', kind: 'custom' as const, version: 1,
  formula: 'rank(-ts_sum(change_pct, 10))', direction: 'low' as const, unit: 'none' as const,
  warmup_bars: 11, pit: false, asset_types: ['stock', 'etf'], stability: 'experimental' as const,
  scale_free: true, dependencies: ['change_pct'], origin: 'custom' as const, status: 'draft' as const,
  available: true, description: '' }
const trialOk = { ok: true, n_dates: 3, null_ratio: 0, ic_mean: 0.0777, ic_std: 0.1, ir: 0.777,
  ic_win_rate: 0.5, t_newey_west: 1, ic_series: [{ date: '2026-10-01', ic: 0.0777, n_symbols: 10 }] }
async function renderEditor(editId = '') {
  mocks.factorLibrary.mockResolvedValue({ factors: [item] })
  mocks.factorColumns.mockResolvedValue({ columns: [] })
  mocks.factorValidate.mockResolvedValue({ ok: true, errors: [], dependencies: ['close'], referenced_factors: [], warmup_bars: 1, cross_sectional: true })
  mocks.factorTrial.mockResolvedValue(trialOk)
  mocks.factorCustomUpdate.mockResolvedValue({ ok: true, id: 'uf_a', version: 2, status: 'draft' })
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter><FactorEditor editId={editId} /></MemoryRouter></QueryClientProvider>) })
  await flush()
}

it('a stock trial that lands after switching to ETF is discarded', async () => {
  await renderEditor()
  await click(btn('校验'))
  let resolveTrial!: (v: unknown) => void
  mocks.factorTrial.mockImplementationOnce(() => new Promise(r => { resolveTrial = r }))
  await click(btn('试算 40 日'))
  const asset = host.querySelector('select[aria-label="试算资产"]') as HTMLSelectElement
  await setValue(asset, 'etf')
  await act(async () => { resolveTrial(trialOk) })
  await flush()
  expect(asset.value).toBe('etf')
  expect(host.textContent).not.toContain('0.0777')
})

it('a late save response keeps the newer unsaved formula dirty', async () => {
  await renderEditor('uf_a')
  const ta = host.querySelector('textarea') as HTMLTextAreaElement
  await setValue(ta, 'rank(ts_mean(volume, 20))')
  await click(btn('校验')); await click(btn('试算 40 日'))
  let resolveSave!: (v: unknown) => void
  mocks.factorCustomUpdate.mockImplementationOnce(() => new Promise(r => { resolveSave = r }))
  await click(btn('保存 · 回草稿'))
  expect(mocks.factorCustomUpdate.mock.calls[0][1].formula).toBe('rank(ts_mean(volume, 20))')
  await setValue(ta, 'rank(close)')
  await act(async () => { resolveSave({ ok: true, id: 'uf_a', version: 2, status: 'draft' }) })
  await flush()
  // 当前公式没存过: 仍要求校验 + 试算才能保存
  expect(btn('保存 · 回草稿')).toBeUndefined()
  expect(btn('保存')).toBeUndefined()
})
