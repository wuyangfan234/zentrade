// @vitest-environment jsdom
// 审计回归: 因子库 / 编辑器 / 组合 / 生成策略 (findings-library-fe A1–A22)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const toasts: [string, string][] = []
vi.mock('@/components/Toast', () => ({ toast: (m: string, k = 'error') => { toasts.push([k, m]) } }))
const mocks = vi.hoisted(() => ({
  factorLibrary: vi.fn(), factorColumns: vi.fn(), factorValidate: vi.fn(), factorTrial: vi.fn(),
  factorCustomCreate: vi.fn(), factorCustomUpdate: vi.fn(), factorCompositeCreate: vi.fn(),
  factorSetStatus: vi.fn(), factorSetGroup: vi.fn(), factorDelete: vi.fn(), strategySaveCodeV2: vi.fn(),
}))
vi.mock('@/lib/api', async (orig) => {
  const actual = await orig<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FactorEditor } from './FactorEditor'
import { FactorComposite } from './FactorComposite'
import { FactorLibrary } from './FactorLibrary'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
const tick = async () => { for (let i = 0; i < 12; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
const f = (o: { id: string } & Record<string, unknown>) => ({
  label: o.id as unknown, group: 'g', kind: 'custom', version: 1, formula: 'rank(close)', direction: 'none', unit: '',
  warmup_bars: 1, pit: false, asset_types: ['stock', 'etf'], stability: 'experimental', scale_free: true,
  dependencies: ['close'], origin: 'custom', status: 'draft', available: true, description: '', ...o,
})
let LIB: { factors: ReturnType<typeof f>[] }
const trialOk = (ic: number) => ({ ok: true, n_dates: 3, null_ratio: 0, ic_mean: ic, ic_std: 0.1, ir: 0.1, ic_win_rate: 0.5, t_newey_west: 1, ic_series: [{ date: '2026-10-01', ic, n_symbols: 10 }] })
beforeEach(() => {
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); toasts.length = 0
  LIB = { factors: [
    f({ id: 'uf_a', label: '因子A', group: 'A组', formula: 'rank(-ts_sum(change_pct, 10))', direction: 'low', description: '某站 · 2026-10-30 · 原文标题' }),
    f({ id: 'ma20', label: '均线20', group: '趋势', kind: 'base', origin: 'builtin', status: 'active' }),
    f({ id: 'rsi_14', label: 'RSI14', group: '动量', kind: 'base', origin: 'builtin', status: 'active' }),
    f({ id: 'roe_latest', label: 'ROE', group: '财务', kind: 'base', origin: 'builtin', status: 'active', pit: true, available: false }),
  ] }
  mocks.factorLibrary.mockImplementation(async () => LIB)
  mocks.factorColumns.mockResolvedValue({ columns: [] })
  mocks.factorValidate.mockResolvedValue({ ok: true, errors: [], dependencies: ['change_pct'], referenced_factors: [], warmup_bars: 5, cross_sectional: true })
  mocks.factorTrial.mockResolvedValue(trialOk(0.0123))
  mocks.factorCustomCreate.mockResolvedValue({ ok: true, id: 'uf_new', version: 1 })
  mocks.factorCustomUpdate.mockResolvedValue({ ok: true, id: 'uf_a', version: 2, status: 'draft' })
  mocks.factorCompositeCreate.mockResolvedValue({ ok: true, id: 'cf_x', version: 1 })
  mocks.factorSetStatus.mockResolvedValue({ ok: true, id: 'uf_a', status: 'watch' })
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const qc = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
const btn = (text: string) => Array.from(host.querySelectorAll('button')).find(b => (b.textContent ?? '').includes(text)) as HTMLButtonElement | undefined
const dialogBtn = (text: string) => Array.from(host.querySelectorAll('[role="dialog"] button')).find(b => (b.textContent ?? '').trim() === text) as HTMLButtonElement | undefined
const setVal = async (el: HTMLInputElement | HTMLTextAreaElement, value: string) => {
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')!.set!.call(el, value)
  await act(async () => { el.dispatchEvent(new Event('input', { bubbles: true })) }); await tick()
}
const setSelect = async (el: HTMLSelectElement, value: string) => {
  await act(async () => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })) }); await tick()
}
const click = async (el: Element | undefined) => { await act(async () => { (el as HTMLElement).click() }); await tick() }
const renderEditor = async (editId = '', client = qc()) => {
  await act(async () => { root.render(<QueryClientProvider client={client}><MemoryRouter><FactorEditor editId={editId} /></MemoryRouter></QueryClientProvider>) })
  await tick()
}
const input = (label: string) => host.querySelector(`input[aria-label="${label}"]`) as HTMLInputElement

it('A1 editor: switching back to new clears id / group / direction left from the edited factor', async () => {
  await renderEditor()
  const pick = host.querySelector('select[aria-label="编辑已有自定义因子"]') as HTMLSelectElement
  await setSelect(pick, 'uf_a')
  await setSelect(pick, '')
  await click(btn('校验')); await click(btn('试算 40 日'))
  expect(input('id').value).toBe('')
  expect(input('分组').value).toBe('自定义')
  await setVal(input('名称'), '新的')
  await click(btn('保存为草稿'))
  const payload = mocks.factorCustomCreate.mock.calls[0][0]
  expect(payload.id).toBeUndefined()
  expect(payload.group).toBe('自定义')
  expect(payload.direction).toBe('none')
})

it('A2/A3 editor: metadata-only save needs no trial and keeps the stored description', async () => {
  await renderEditor('uf_a')
  expect(btn('保存')).toBeTruthy() // no validate / trial needed for a name change
  await setVal(input('名称'), '因子A2')
  await click(btn('保存'))
  const [id, payload] = mocks.factorCustomUpdate.mock.calls[0]
  expect(id).toBe('uf_a')
  expect(payload.description).toBe('某站 · 2026-10-30 · 原文标题')
  expect(payload.direction).toBe('low')
  expect(mocks.factorTrial).not.toHaveBeenCalled()
})

it('A9 editor: a trial result that returns after the formula changed is discarded', async () => {
  await renderEditor()
  const ta = host.querySelector('textarea') as HTMLTextAreaElement
  await click(btn('校验'))
  let resolveTrial!: (v: unknown) => void
  mocks.factorTrial.mockImplementationOnce(() => new Promise(r => { resolveTrial = r }))
  await click(btn('试算 40 日'))
  await setVal(ta, 'rank(ts_mean(volume, 20))')
  await act(async () => { resolveTrial(trialOk(0.0777)) }); await tick()
  expect(host.textContent).not.toContain('0.0777')
  expect(btn('保存为草稿')).toBeUndefined()
})

it('A10 editor: after ?edit= the form does not jump back to that factor once a new one is saved', async () => {
  await renderEditor('uf_a')
  await setSelect(host.querySelector('select[aria-label="编辑已有自定义因子"]') as HTMLSelectElement, '')
  await setVal(host.querySelector('textarea') as HTMLTextAreaElement, 'rank(ts_mean(volume, 20))')
  await click(btn('校验')); await click(btn('试算 40 日'))
  await setVal(input('名称'), '新因子')
  mocks.factorCustomCreate.mockImplementation(async () => {
    LIB = { factors: [...LIB.factors, f({ id: 'uf_new', label: '新因子', formula: 'rank(ts_mean(volume, 20))' })] }
    return { ok: true, id: 'uf_new', version: 1 }
  })
  await click(btn('保存为草稿'))
  expect((host.querySelector('textarea') as HTMLTextAreaElement).value).toBe('rank(ts_mean(volume, 20))')
  expect(host.textContent).not.toContain('编辑 uf_a')
  expect(btn('检验 uf_new')).toBeTruthy()
})

it('A11 editor: after saving a changed formula the button no longer offers another version bump', async () => {
  await renderEditor('uf_a')
  await setVal(host.querySelector('textarea') as HTMLTextAreaElement, 'rank(ts_mean(volume, 20))')
  await click(btn('校验')); await click(btn('试算 40 日'))
  await click(btn('保存 · 回草稿'))
  expect(btn('保存 · 回草稿')).toBeUndefined()
  expect(host.textContent).toContain('更新 uf_a')
  expect(host.textContent).not.toContain('→ v3')
})

it('A15 editor: the operator panel stays collapsed after the user closes it', async () => {
  await renderEditor()
  const d = host.querySelector('details') as HTMLDetailsElement
  expect(d.open).toBe(true)
  await act(async () => { d.open = false }); await tick()
  await setVal(host.querySelector('textarea') as HTMLTextAreaElement, 'rank(close)')
  expect(d.open).toBe(false)
})

it('A19 editor: a structured trial error renders as a DSL error line, not raw JSON', async () => {
  await renderEditor()
  await click(btn('校验'))
  mocks.factorTrial.mockRejectedValueOnce(new Error(JSON.stringify({ errors: [{ code: 'E013', message: '依赖列不可用: 面板缺少公式所需列', position: { offset: 0, line: 1 }, detail: { missing: ['roe_latest'] } }] })))
  await click(btn('试算 40 日'))
  expect(host.textContent).toContain('[E013] 依赖列不可用: 面板缺少公式所需列 · roe_latest')
  expect(host.textContent).not.toContain('{"errors"')
})

it('A4/A14/A16 composite: equal mode submits equal weights, unavailable factors are hidden, weight input can be retyped', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc()}><FactorComposite /></QueryClientProvider>) })
  await tick()
  expect(btn('ROE')).toBeUndefined()
  await click(btn('均线20')); await click(btn('RSI14'))
  await click(btn('手动'))
  const w = host.querySelector('input[type="number"]') as HTMLInputElement
  await setVal(w, '')
  expect(w.value).toBe('')
  await setVal(w, '3')
  expect(w.value).toBe('3')
  await click(btn('均等'))
  await setVal(input('名称'), 'combo')
  await click(btn('注册复合因子'))
  // 提交 = 界面显示的归一权重 (11-04 审计: 显示 0.5 / 0.5 而提交 1 / 1)
  expect(mocks.factorCompositeCreate.mock.calls[0][0].members).toEqual({ ma20: 0.5, rsi_14: 0.5 })
})

it('A5/A12/A13/A21 library: stale group filter falls back, status shows Chinese, unavailable factors have no 检验', async () => {
  const client = qc()
  await act(async () => { root.render(<QueryClientProvider client={client}><FactorLibrary onInspect={() => {}} /></QueryClientProvider>) })
  await tick()
  const roeRow = Array.from(host.querySelectorAll('tbody tr')).find(r => r.textContent?.includes('roe_latest'))!
  expect(roeRow.textContent).toContain('缺数据')
  expect(roeRow.querySelector('button')).toBeNull()
  const sel = host.querySelector('select[aria-label="因子分组"]') as HTMLSelectElement
  await setSelect(sel, 'A组')
  LIB = { factors: LIB.factors.map(x => x.id === 'uf_a' ? { ...x, group: '新组' } : x) }
  await act(async () => { await client.invalidateQueries({ queryKey: ['factors-library'] }) }); await tick()
  expect(sel.value).toBe('all')
  expect(host.textContent).not.toContain('无匹配因子')
  await click(Array.from(host.querySelectorAll('tbody tr')).find(r => r.textContent?.includes('uf_a')))
  const rows = Array.from(host.querySelectorAll('[role="dialog"] dl > div')).map(d => `${d.querySelector('dt')?.textContent}=${d.querySelector('dd')?.textContent}`)
  expect(rows).toContain('状态=草稿')
  await click(dialogBtn('观察'))
  expect(toasts).toContainEqual(['success', '因子A · 观察'])
})

it('A8 nested dialogs: one Escape closes only the generate dialog', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc()}><FactorLibrary onInspect={() => {}} onEdit={() => {}} /></QueryClientProvider>) })
  await tick()
  await click(host.querySelector('tbody tr') as HTMLElement)
  await click(dialogBtn('生成策略'))
  expect(host.querySelectorAll('[role="dialog"]').length).toBe(2)
  await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) }); await tick()
  expect(host.querySelectorAll('[role="dialog"]').length).toBe(1)
})

it('A6/A18 generate: no preselected direction for unknown factors; id conflicts retry quietly past 3', async () => {
  LIB = { factors: [f({ id: 'ma20', label: '均线20', group: '趋势', kind: 'base', origin: 'builtin', status: 'active' })] }
  let n = 0
  mocks.strategySaveCodeV2.mockImplementation(async (payload: { strategy_id: string }, opts?: { quiet?: boolean }) => {
    n += 1
    expect(opts?.quiet).toBe(true)
    if (n <= 4) throw new Error(`策略 ${payload.strategy_id} 已存在，请改用修改模式或换一个策略 ID`)
    return { strategy_id: payload.strategy_id }
  })
  await act(async () => { root.render(<QueryClientProvider client={qc()}><FactorLibrary onInspect={() => {}} /></QueryClientProvider>) })
  await tick()
  await click(host.querySelector('tbody tr') as HTMLElement)
  await click(dialogBtn('生成策略'))
  const submit = () => Array.from(host.querySelectorAll('[role="dialog"] button')).filter(b => b.textContent?.includes('生成策略')).pop() as HTMLButtonElement
  expect(submit().disabled).toBe(true)
  await click(btn('值小看多'))
  await click(submit())
  expect(mocks.strategySaveCodeV2.mock.calls[4][0].strategy_id).toBe('custom_factor_ma20_5')
  expect(mocks.strategySaveCodeV2.mock.calls[4][0].code).toContain('DIRECTIONS = {"ma20": "low"}')
  expect(toasts).toEqual([['success', '已生成 custom_factor_ma20_5']])
})
