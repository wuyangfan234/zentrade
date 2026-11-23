// @vitest-environment jsdom
// 「去发布」深链: 落到该候选所属那一轮挖掘并选中它。
// 修复前: 从别的一轮切过来时, 加载期间页面还显示上一轮结果, 纠正选中项的逻辑拿上一轮的候选去比,
// 把地址里的 candidate 换成上一轮第一条, 新一轮到了再换成新一轮第一条 —— 深链要的那条永远选不中。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useNavigate, type NavigateFunction } from 'react-router-dom'
import { afterAll, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import type { MiningCandidateRow, MiningResult, MiningRun } from '@/lib/api'
import { miningCandidateHref, miningFocusState } from '@/lib/minedCandidate'

vi.mock('@/components/Toast', () => ({ toast: () => {} }))
vi.mock('./charts/MiningOosChart', () => ({ MiningOosChart: () => null }))
vi.mock('./charts/FactorCorrelationHeatmap', () => ({ FactorCorrelationHeatmap: () => null }))
const mocks = vi.hoisted(() => ({
  factorColumns: vi.fn(), strategyList: vi.fn(), miningRuns: vi.fn(), miningAvailability: vi.fn(),
  miningConfig: vi.fn(), preferences: vi.fn(), miningRun: vi.fn(), miningResult: vi.fn(),
}))
vi.mock('@/lib/api', async (orig) => {
  const actual = await orig<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { MiningWorkbench } from './MiningWorkbench'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
const tick = async () => { for (let i = 0; i < 16; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

const W48 = 'weekly-2026-W48', W47 = 'weekly-2026-W47', W46 = 'weekly-2026-W46'
const MOM = 'factor:momentum_5d|w:1|d:1', BOLL = 'factor:boll_width|w:1|d:1', AMP = 'factor:amplitude|w:1|d:-1', ATR = 'factor:atr_14|w:1|d:-1'
const ATR_STRATEGY = 'mined_factor_797ee728df832027ee16'

function cand(signature: string, factor: string, display: string, extra: Partial<MiningCandidateRow> = {}): MiningCandidateRow {
  return {
    signature, name: `因子组合 · ${factor}`, display_name: display, kind: 'factor_combination', factor_names: [factor],
    strategy_id: null, regime_state: 'overall', score: 1, oos_return: 0.02, oos_sharpe: 1.1, oos_max_drawdown: -0.1,
    oos_positive_fold_ratio: 0.75, oos_n_trades: 120, confidence: 'standard', valid_folds: 4, skipped_folds: 0,
    promoted_candidate_id: null, published_strategy_id: null, gate: { qualified: true, reasons: [] }, folds: [], ...extra,
  }
}
function result(runId: string, candidates: MiningCandidateRow[]): MiningResult {
  return {
    run_id: runId, methodology_version: 'factor_v2', algorithm_version: 'mining-v1', data_as_of: '2026-11-20',
    summary: { selected_factor_count: 1, factor_count: 4, candidate_count: candidates.length, valid_fold_count: 4, skipped_fold_count: 0, confidence: 'standard' },
    factors: [], correlation: { labels: [], matrix: [], threshold: 0.75 }, regimes: [], candidates, folds: [], telemetry: {},
  } as unknown as MiningResult
}
const run = (runId: string) => ({ run_id: runId, signature: runId, status: 'succeeded', request: { factor_names: [] }, created_at: '', updated_at: '' }) as unknown as MiningRun
const RESULTS: Record<string, MiningResult> = {
  [W48]: result(W48, [cand(MOM, 'momentum_5d', '5日动量 高')]),
  // 深链要的 AMP 排在第二: 旧逻辑会落到第一条 BOLL
  [W47]: result(W47, [cand(BOLL, 'boll_width', '布林带宽 高'), cand(AMP, 'amplitude', '日振幅 低')]),
  [W46]: result(W46, [cand(ATR, 'atr_14', 'ATR(14)原值 低', { published_strategy_id: ATR_STRATEGY })]),
}

const store = new Map<string, string>()
let root: Root
let host: HTMLDivElement
let navigate: NavigateFunction
let location: ReturnType<typeof useLocation>
let w47Release: (() => void) | null = null
const scrolled: Element[] = []
const realScroll = Element.prototype.scrollIntoView

function Probe() { navigate = useNavigate(); location = useLocation(); return null }
const param = (key: string) => new URLSearchParams(location.search).get(key)
const header = () => host.querySelector('[data-mining-candidate-name]')?.textContent
const listName = (sig: string) => host.querySelector(`[data-mining-candidate="${sig}"] span`)?.textContent

beforeAll(async () => {
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
    removeItem: (k: string) => { store.delete(k) },
  })
  Element.prototype.scrollIntoView = function scrollIntoView() { scrolled.push(this as Element) }
  mocks.factorColumns.mockResolvedValue({ columns: [
    { id: 'momentum_5d', label: '5日动量', group: '动量', desc: '' }, { id: 'boll_width', label: '布林带宽', group: '波动率', desc: '' },
    { id: 'amplitude', label: '日振幅', group: '波动率', desc: '' }, { id: 'atr_14', label: 'ATR(14)原值', group: '波动率', desc: '' },
  ] })
  // 线上已发布的 atr_14 策略文件名是旧版默认名
  mocks.strategyList.mockResolvedValue({ strategies: [
    { id: ATR_STRATEGY, name: '因子组合 · atr_14', execution_backend: 'matrix_native', asset_types: ['stock'], timeframes: ['1d'], description: '' },
  ] })
  mocks.miningRuns.mockResolvedValue({ items: [] })
  mocks.miningAvailability.mockResolvedValue({ eligible: true, trading_bars: 500, required_bars: 252, outer_folds: 4, required_outer_folds: 3 })
  mocks.miningConfig.mockResolvedValue({ mining_schedule_enabled: false, mining_schedule_weekday: 4, mining_budget_profile: 'balanced' })
  mocks.preferences.mockResolvedValue({ realtime_quotes_enabled: false })
  mocks.miningRun.mockImplementation(async (id: string) => run(id))
  // W47 的结果先挂住: 断言「加载期间」地址里的 candidate 没被上一轮冲掉
  mocks.miningResult.mockImplementation((id: string) => id === W47
    ? new Promise(resolve => { w47Release = () => resolve(RESULTS[W47]) })
    : Promise.resolve(RESULTS[id]))
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/factors?tab=mining&run=${W48}`]}>
          <Routes><Route path="*" element={<><Probe /><MiningWorkbench /></>} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await tick()
})
afterAll(() => {
  act(() => root.unmount()); host.remove(); vi.unstubAllGlobals()
  Element.prototype.scrollIntoView = realScroll
})
beforeEach(() => { scrolled.length = 0 })

it('a plain visit selects the first candidate and does not scroll', () => {
  expect(param('run')).toBe(W48)
  expect(param('candidate')).toBe(MOM)
  expect(header()).toBe('5日动量 高')
  expect(scrolled).toHaveLength(0)
})

it('去发布 deep link from another run keeps the requested candidate, selects it and scrolls the candidates into view once', async () => {
  await act(async () => { navigate(miningCandidateHref(W47, AMP), { state: miningFocusState(AMP) }) })
  await tick()
  // 加载期间: 上一轮 (W48) 的候选不能把深链的 candidate 冲掉
  expect(param('run')).toBe(W47)
  expect(param('candidate')).toBe(AMP)
  expect(scrolled).toHaveLength(0)
  await act(async () => { w47Release!() })
  await tick()
  expect(param('candidate')).toBe(AMP)
  expect(header()).toBe('日振幅 低')
  expect(host.querySelector(`[data-mining-candidate="${AMP}"]`)!.getAttribute('aria-current')).toBe('true')
  expect(host.querySelector(`[data-mining-candidate="${BOLL}"]`)!.getAttribute('aria-current')).toBeNull()
  // 发布按钮就在选中的候选上, 且可用 (达标)
  const publish = host.querySelector<HTMLButtonElement>('[data-mining-publish]')!
  expect(publish.textContent).toBe('发布')
  expect(publish.disabled).toBe(false)
  expect(scrolled).toHaveLength(1)
  expect(scrolled[0].hasAttribute('data-mining-candidates')).toBe(true)
  // 之后在页内点选别的候选不再滚动
  await act(async () => { host.querySelector<HTMLButtonElement>(`[data-mining-candidate="${BOLL}"]`)!.click() })
  await tick()
  expect(param('candidate')).toBe(BOLL)
  expect(header()).toBe('布林带宽 高')
  expect(scrolled).toHaveLength(1)
})

it('a published candidate shows its strategy pool name; the rename dialog still defaults to the publish default name', async () => {
  await act(async () => { navigate(`/factors?tab=mining&run=${W46}`) })
  await tick()
  expect(listName(ATR)).toBe('因子组合 · ATR(14)原值')
  expect(header()).toBe('因子组合 · ATR(14)原值')
  expect(scrolled).toHaveLength(0)
  const publish = host.querySelector<HTMLButtonElement>('[data-mining-publish]')!
  expect(publish.textContent).toBe('已发布 · 改名')
  await act(async () => { publish.click() })
  // 默认名不变 (= 后端 default_strategy_name): 显示名换了不等于改名
  expect(document.querySelector<HTMLInputElement>('[data-publish-name]')!.value).toBe('ATR(14)原值 低')
})
