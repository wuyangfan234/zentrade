// @vitest-environment jsdom
// 因子发现页 C 方案: 对比清单 + 单列采集 + 合并投递区。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({ factorLabPage: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FactorDiscover } from './FactorDiscover'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
Element.prototype.scrollIntoView ??= function scrollIntoView() {}
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const page = fixture as unknown as FactorLabPage

async function render(p: FactorLabPage = page) {
  mocks.factorLabPage.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><FactorDiscover onGo={() => {}} /></QueryClientProvider>) })
  for (let i = 0; i < 20 && !host.querySelector('[data-discover]'); i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('candidate and engine use the same comparison-row regions inside one list', async () => {
  await render()
  const list = host.querySelector('[data-cands]')!
  expect(list.getAttribute('data-layout')).toBe('comparison')
  const rows = [...list.querySelectorAll('[data-comparison-row]')]
  expect(rows).toHaveLength(host.querySelectorAll('[data-cand]').length + host.querySelectorAll('[data-ref]').length)
  for (const row of rows) {
    for (const region of ['identity', 'legs', 'metrics', 'actions']) {
      expect(row.querySelector(`[data-row-${region}]`), `${row.getAttribute('data-cand') ?? 'engine'}:${region}`).not.toBeNull()
    }
  }
  expect(list.querySelector('[data-ref] [data-row-actions]')!.textContent).not.toContain('保存')
})

it('an engine-only list still occupies the comparison row rather than an empty card grid', async () => {
  const p = JSON.parse(JSON.stringify(page)) as FactorLabPage
  p.week!.candidates = []
  await render(p)
  expect(host.querySelectorAll('[data-cand]')).toHaveLength(0)
  expect(host.querySelectorAll('[data-cands] [data-comparison-row]')).toHaveLength(1)
  expect(host.querySelector('[data-ref] [data-row-metrics]')).not.toBeNull()
})

it('intake cards use one list with aligned content and metric regions', async () => {
  await render()
  const list = host.querySelector('[data-intake-cards]')!
  expect(list.getAttribute('data-layout')).toBe('list')
  for (const row of list.querySelectorAll('[data-intake]')) {
    expect(row.querySelector('[data-intake-content]')).not.toBeNull()
    expect(row.querySelector('[data-intake-evidence]')).not.toBeNull()
    expect(row.querySelector('[data-intake-metrics]')).not.toBeNull()
  }
})

it('submissions and automatic intake share one section without losing their anchors', async () => {
  await render()
  const heads = [...host.querySelectorAll('h2')].map(h => h.textContent)
  expect(heads.filter(x => x === '采集与投递')).toHaveLength(1)
  expect(heads).not.toContain('我的投递')
  expect(host.querySelector('#fl-mine [data-mine]')).not.toBeNull()
  expect(host.querySelector('#fl-intake [data-open="sources"]')).not.toBeNull()
})

it('mobile grid tracks and the factor main area can shrink instead of clipping content', () => {
  const layout = readFileSync(resolve('src/components/Layout.tsx'), 'utf8')
  const factors = readFileSync(resolve('src/pages/Factors.tsx'), 'utf8')
  expect(layout).toContain('h-full min-w-0 overflow-auto overflow-x-clip')
  expect(factors).toContain('min-h-0 min-w-0 flex-1')
})
