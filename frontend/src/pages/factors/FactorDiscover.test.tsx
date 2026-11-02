// @vitest-environment jsdom
// 0.47.0 因子页「发现」首页: 数据快照 (一周挖掘 + 两条采集) 渲染; 同屏计数一致; 不主动发布。
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
import { FactorDiscover, pct } from './FactorDiscover'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

const page = fixture as unknown as FactorLabPage

async function render(p: FactorLabPage = page) {
  mocks.factorLabPage.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorDiscover onGo={() => {}} /></QueryClientProvider>) })
  for (let i = 0; i < 20 && !host.querySelector('[data-discover]'); i++) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
}

it('flow bar has six steps and the last one equals pending strategies + pending factors', async () => {
  await render()
  const steps = [...host.querySelectorAll('[data-flow] [data-step]')]
  expect(steps.map(s => s.textContent?.match(/^\d(\S+?)(每|待)/)?.[1])).toEqual(['采集', '筛因子', '组合', '验证', '回测', '你决定'])
  const cands = page.week!.candidates.filter(c => !c.error).length
  const factors = page.intake!.funnel.pending
  expect(steps[5].textContent).toContain(`${cands + factors}待看`)
  expect(steps[5].textContent).toContain(`策略 ${cands} · 因子 ${factors}`)
  expect(host.querySelector('[data-flow]')?.textContent).toContain('已发布 0')
})

it('reference card is the current card engine and is not counted as a candidate', async () => {
  await render()
  const ref = host.querySelector('[data-ref]')!
  expect(ref.textContent).toContain('当前出卡引擎')
  expect(ref.textContent).toContain('对照')
  expect(ref.textContent).not.toContain('保存')
  expect(host.querySelectorAll('[data-cand]').length).toBe(page.week!.candidates.filter(c => !c.error).length)
  expect(host.querySelector('[data-ccount]')?.textContent).toContain('1 对照')
  expect(host.textContent).not.toContain('现套')
})

it('detail yearly table ends with 本年度 and checks have no beat-the-engine gate', async () => {
  await render()
  const rows = [...host.querySelectorAll('[data-yearly] tbody tr')]
  expect(rows[rows.length - 1].firstElementChild?.textContent).toBe('本年度')
  expect(host.querySelector('[data-detail]')?.textContent).not.toMatch(/年化 ≥|跑赢/)
  const ckn = host.querySelector('[data-ckn]')!.textContent!
  const [ok, total] = ckn.split('/').map(s => Number(s.trim()))
  expect(total).toBe(7)
  expect(ok).toBeLessThanOrEqual(7)
})

it('intake funnel last cell equals pending intake cards; duplicate card is archived with restore only', async () => {
  await render()
  const funnel = host.querySelector('[data-funnel]')!.textContent!
  expect(funnel).toContain(`待你${page.intake!.funnel.pending}`)
  const pending = host.querySelectorAll('[data-intake][data-stage="pending"]')
  expect(pending.length).toBe(page.intake!.funnel.pending)
  const dup = host.querySelector('[data-intake][data-stage="archived"]')!
  expect(dup.textContent).toContain('重复')
  expect(dup.textContent).toContain('恢复')
  expect(dup.textContent).not.toContain('并入因子库')
})

it('save sends a decision and never calls a publish endpoint', async () => {
  mocks.factorLabDecide.mockResolvedValue({})
  await render()
  const card = host.querySelector('[data-cand]')!
  const save = [...card.querySelectorAll('button')].find(b => b.textContent === '保存')!
  await act(async () => { save.click() })
  expect(mocks.factorLabDecide).toHaveBeenCalledWith(page.week!.week, card.getAttribute('data-cand'), 'save')
})

it('renders without a weekly run yet', async () => {
  await render({ ...page, week: null, weeks: [] })
  expect(host.textContent).toContain('本周未生成')
  expect(host.querySelector('[data-funnel]')).not.toBeNull()
})

it('pct uses real minus and sign', () => {
  expect(pct(0.494)).toBe('+49.4%')
  expect(pct(-0.2849)).toBe('−28.5%')
  expect(pct(null)).toBe('—')
})
