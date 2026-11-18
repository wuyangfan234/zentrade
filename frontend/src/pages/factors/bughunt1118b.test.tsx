// @vitest-environment jsdom
// 2026-11-18 清查复现 (第二批): 采集页列表卡限高; 公式挖掘的席位不与机会页「候补」同名。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage, FormulaMinerView } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({ factorLabPage: vi.fn(), formulaMiner: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FactorDiscover } from './FactorDiscover'
import { FormulaMinerPanel, runLine } from './FormulaMiner'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const wrap = (el: React.ReactNode) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{el}</QueryClientProvider>
const settle = async (sel: string) => { for (let i = 0; i < 20 && !host.querySelector(sel); i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

it('intake list is capped and scrolls inside its card', async () => {
  // 线上 11-18: 这张通栏卡 1440 宽下 913px, 手机 1664px, 把下面的「近 4 周」推出两屏外
  mocks.factorLabPage.mockResolvedValue(fixture as unknown as FactorLabPage)
  await act(async () => { root.render(wrap(<FactorDiscover onGo={() => {}} />)) })
  await settle('[data-intake-cards]')
  const list = host.querySelector('[data-intake-cards]')!
  expect(list.className).toMatch(/max-h-\[\d+px\]/)
  expect(list.className).toContain('overflow-y-auto')
  expect(list.className).toContain('scroll-y-visible')
  expect(list.closest('.rounded-card')!.className).toContain('overflow-hidden')
})

it('formula miner seats are not called 候补 (that word means the scored rules on the opportunities page)', async () => {
  const v = { enabled: true, running: false, pool_ready: true, bench: [], selected: [], retired: [],
    totals: { tried: 120, admitted: 2, retired: 0, selected: 0, above: 0, outcomes: {} },
    caps: { bench: 6, per_run: 2, tenure_days: 60, need_days: 20, need_filled: 15 }, last_run: null, runs: [] } as unknown as FormulaMinerView
  mocks.formulaMiner.mockResolvedValue(v)
  await act(async () => { root.render(wrap(<FormulaMinerPanel />)) })
  await settle('[data-miner-facts]')
  expect(host.querySelector('[data-miner-facts]')!.textContent).toContain('挖掘席位0 / 6')
  expect(host.textContent).not.toContain('候补')
  expect(runLine({ run_id: 'r', day: '2026-11-19', skipped: 'bench_full' })).toBe('11-19 · 挖掘席位已满')
})
