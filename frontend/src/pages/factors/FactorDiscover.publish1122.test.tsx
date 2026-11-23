// @vitest-environment jsdom
// 采集页「保存」成功的提示要给下一步: 「去发布」直达该候选所属那一轮挖掘 (只写「已保存」等于没给路)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({ factorLabPage: vi.fn(), factorLabDecide: vi.fn() }))
type ToastCall = [string, string | undefined, { action?: { label: string; onClick: () => void } } | undefined]
const toasts = vi.hoisted(() => ({ calls: [] as ToastCall[] }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/Toast', () => ({ toast: (...args: ToastCall) => { toasts.calls.push(args) } }))
import { FactorDiscover } from './FactorDiscover'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); toasts.calls.length = 0 })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const page = () => JSON.parse(JSON.stringify(fixture)) as FactorLabPage
const tick = async () => { for (let i = 0; i < 12; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
async function render(onOpenCandidate = vi.fn()) {
  mocks.factorLabPage.mockResolvedValue(page())
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorDiscover onGo={() => {}} onOpenCandidate={onOpenCandidate} /></QueryClientProvider>) })
  await tick()
  return onOpenCandidate
}
const saveOnCard = () => [...host.querySelector('[data-cand]')!.querySelectorAll('button')].find(b => b.textContent === '保存')!

it('save success toast offers 去发布 to that candidate\'s own mining run', async () => {
  mocks.factorLabDecide.mockResolvedValue({ candidate_id: 'rc_1' })
  const open = await render()
  await act(async () => { saveOnCard().click() })
  await tick()
  const saved = toasts.calls.find(([msg]) => msg === '已保存 · 候选方案')!
  expect(saved[1]).toBe('success')
  expect(saved[2]?.action?.label).toBe('去发布')
  saved[2]!.action!.onClick()
  const p = page()
  expect(open).toHaveBeenCalledWith(p.week!.run_id, p.week!.candidates[0].signature)
})

it('a save that did not create a candidate gets no next step', async () => {
  mocks.factorLabDecide.mockResolvedValue({})
  await render()
  await act(async () => { saveOnCard().click() })
  await tick()
  expect(toasts.calls).toEqual([['已记录 · 候选方案未建出', 'error']])
})
