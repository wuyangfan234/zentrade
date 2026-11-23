// @vitest-environment jsdom
// 候选方案 → 发布: 未发布的挖掘候选给可点的「去发布」(落到所属那一轮挖掘并选中), 不再只写「待发布」;
// 名字与挖掘页、策略池同名 (保存时的「因子组合 · amplitude」不上屏)。数据形状取自生产 research_candidates.json。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ResearchCandidate } from '@/lib/api'

const mocks = vi.hoisted(() => ({ researchCandidates: vi.fn(), strategyList: vi.fn(), factorColumns: vi.fn() }))
vi.mock('@/lib/api', async (orig) => {
  const actual = await orig<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/Toast', () => ({ toast: () => {} }))
import { ResearchCandidatesDialog } from './ResearchCandidatesDialog'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
const tick = async () => { for (let i = 0; i < 12; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

function mined(id: string, name: string, factor: string, direction: 'high' | 'low', run: string, strategyId: string, status: ResearchCandidate['status'] = 'validated'): ResearchCandidate {
  return {
    id, kind: 'strategy', name, source_id: strategyId, status, data_as_of: '2026-11-20', created_at: '', updated_at: '',
    metrics: { oos_sharpe: 1.2, oos_return: 0.03 },
    config: {
      origin_run_id: run, candidate_signature: `factor:${factor}|w:1|d:${direction === 'low' ? -1 : 1}`,
      strategy_id: strategyId, factor_names: [factor], directions: [direction],
    },
  }
}
const ITEMS: ResearchCandidate[] = [
  mined('rc_amp', '因子组合 · amplitude', 'amplitude', 'low', 'weekly-2026-W47', 'mined_factor_7df8d78a3ca7c60f3b4e'),
  mined('rc_boll', '因子组合 · boll_width', 'boll_width', 'high', 'weekly-2026-W47', 'mined_factor_abe24aef5a06a655bc01'),
  mined('rc_atr', '因子组合 · atr_14', 'atr_14', 'low', 'weekly-2026-W46', 'mined_factor_797ee728df832027ee16'),
  mined('rc_rej', '因子组合 · amplitude', 'amplitude', 'high', 'weekly-2026-W45', 'mined_factor_rejected0000000000', 'rejected'),
]

let location: ReturnType<typeof useLocation> | null = null
function LocationProbe() { location = useLocation(); return null }

async function render(onClose = vi.fn(), onLoadStrategy = vi.fn()) {
  mocks.researchCandidates.mockResolvedValue({ items: ITEMS })
  mocks.strategyList.mockResolvedValue({ strategies: [
    { id: 'mined_factor_797ee728df832027ee16', name: '因子组合 · atr_14', execution_backend: 'matrix_native', asset_types: ['stock'], timeframes: ['1d'] },
  ] })
  mocks.factorColumns.mockResolvedValue({ columns: [
    { id: 'amplitude', label: '日振幅', group: '波动率', desc: '' },
    { id: 'boll_width', label: '布林带宽', group: '波动率', desc: '' },
    { id: 'atr_14', label: 'ATR(14)原值', group: '波动率', desc: '' },
  ] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/factors']}>
          <Routes><Route path="*" element={<><LocationProbe /><ResearchCandidatesDialog onClose={onClose} onLoadStrategy={onLoadStrategy} /></>} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await tick()
  return { onClose, onLoadStrategy }
}
const rows = () => [...document.querySelectorAll('[data-candidate-name]')].map(el => el.closest('.border-b') as HTMLElement)
const rowOf = (name: string) => rows().find(row => row.querySelector('[data-candidate-name]')!.textContent === name)

it('names match the mining page and the strategy pool, never the saved English id', async () => {
  await render()
  expect([...document.querySelectorAll('[data-candidate-name]')].map(el => el.textContent))
    .toEqual(['日振幅 低', '布林带宽 高', '因子组合 · ATR(14)原值', '日振幅 高'])
  expect(document.body.textContent).not.toMatch(/amplitude|boll_width|atr_14/)
})

it('unpublished mined candidates get a clickable 去发布; the dead 待发布 text is gone', async () => {
  await render()
  expect(document.body.textContent).not.toContain('待发布')
  expect(rowOf('日振幅 低')!.querySelector('[data-candidate-publish]')!.textContent).toBe('去发布')
  expect(rowOf('布林带宽 高')!.querySelector('[data-candidate-publish]')).not.toBeNull()
  // 已发布的保留「载入复测」, 不再给去发布; 已排除的不给
  const atr = rowOf('因子组合 · ATR(14)原值')!
  expect(atr.querySelector('[data-candidate-publish]')).toBeNull()
  expect([...atr.querySelectorAll('button')].some(b => b.textContent === '载入复测')).toBe(true)
  expect(rowOf('日振幅 高')!.querySelector('[data-candidate-publish]')).toBeNull()
})

it('去发布 closes the dialog and lands on that candidate\'s own mining run with it selected', async () => {
  const { onClose } = await render()
  await act(async () => { rowOf('日振幅 低')!.querySelector<HTMLButtonElement>('[data-candidate-publish]')!.click() })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(location!.pathname).toBe('/factors')
  const params = new URLSearchParams(location!.search)
  expect(params.get('tab')).toBe('mining')
  expect(params.get('run')).toBe('weekly-2026-W47')
  expect(params.get('candidate')).toBe('factor:amplitude|w:1|d:-1')
  expect(location!.state).toEqual({ miningFocus: 'factor:amplitude|w:1|d:-1' })
})
