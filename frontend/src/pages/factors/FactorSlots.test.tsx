// @vitest-environment jsdom
// 因子页 C 方案 (0.54.0): 槽位视图 + 依赖矩阵。纯函数口径 + 渲染结构 (等高行 / 在用 vs 挑战 / 旧地址)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FactorLabPage, FactorLibraryItem, MiningResult, StrategyDetail, WatchlistRuleCatalogItem } from '@/lib/api'
import fixture from './__fixtures__/factor-lab-page.json'

const mocks = vi.hoisted(() => ({
  factorLabPage: vi.fn(), watchlistRules: vi.fn(), oppPage: vi.fn(), screenerStrategies: vi.fn(),
  factorLibrary: vi.fn(), miningRuns: vi.fn(), miningResult: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import {
  FactorMatrix, FactorSlots, boardRows, humanizeName, roundShares, scoringChallengers, scoringUses, strategyFamily, weeklyIcTable,
} from './FactorSlots'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

const strat = (id: string, scoring: Record<string, number>, tags: string[], extra: Partial<StrategyDetail> = {}) =>
  ({ id, name: id, tags, scoring, scoring_directions: {}, descending: true, ...extra }) as unknown as StrategyDetail
const libItem = (id: string, label: string, extra: Partial<FactorLibraryItem> = {}) =>
  ({ id, label, origin: 'builtin', status: 'active', available: true, ...extra }) as FactorLibraryItem
const result = (rows: [string, number, boolean][]): MiningResult =>
  ({ run_id: 'r', factors: rows.map(([f, ic, sel]) => ({ factor_name: f, label: f, ic_mean: ic, selected: sel, direction: ic >= 0 ? 1 : -1 })), folds: [] }) as unknown as MiningResult

it('strategy family follows META tags with breakout first', () => {
  expect(strategyFamily(['布林', '突破'])).toBe('突破')
  expect(strategyFamily(['回踩', '均线', '反弹'])).toBe('回踩')
  expect(strategyFamily(['超跌', '反弹', 'RSI'])).toBe('反转')
  expect(strategyFamily(['均线', '金叉'])).toBe('趋势')
})

it('scoring IC is folded by scoring direction and descending flag', () => {
  const lib = new Map([['a', libItem('a', 'A')], ['b', libItem('b', 'B')], ['c', libItem('c', 'C')]])
  const ic = weeklyIcTable([{ week: 'W1', result: result([['a', -0.01, true], ['b', 0.01, false], ['c', 0.01, true]]) },
    { week: 'W2', result: result([['a', -0.012, true], ['b', 0.012, false], ['c', 0.01, false]]) }])
  const strategies = [
    strat('s1', { a: 0.5, b: 0.5 }, ['突破']),
    strat('s2', { c: 1 }, ['回踩'], { scoring_directions: { c: 'low' } } as Partial<StrategyDetail>),
  ]
  const { uses, nStrat } = scoringUses(strategies, lib, ic, 2, new Set(['s2']))
  expect(nStrat).toBe(2)
  const a = uses.find(u => u.id === 'a')!, c = uses.find(u => u.id === 'c')!
  // a 打分偏好大值, IC 为负 → 方向相反
  expect(a.dirMean).toBeCloseTo(-0.011)
  expect(a.flag).toBe('方向相反')
  // c 打分偏好小值, 原始 IC 为正 → 折算后为负
  expect(c.dirMean).toBeCloseTo(-0.01)
  expect(c.uses[0].inPlay).toBe(true)
  expect(a.sel).toEqual([true, true])
  // descending=false 再取反
  const flipped = scoringUses([strat('s3', { a: 1 }, [], { descending: false } as Partial<StrategyDetail>)], lib, ic, 2, new Set())
  expect(flipped.uses[0].dirMean).toBeCloseTo(0.011)
  expect(flipped.uses[0].flag).toBeNull()
})

it('raw scoring columns without a data source are flagged instead of hidden', () => {
  const { uses } = scoringUses([strat('s', { turnover_rate: 0.3, amount: 0.7 }, ['突破'])], new Map(), new Map(), 4, new Set())
  expect(uses.find(u => u.id === 'turnover_rate')!.flag).toBe('缺数据')
  expect(uses.find(u => u.id === 'amount')!.flag).toBe('无检验')
  expect(uses.find(u => u.id === 'amount')!.label).toBe('成交额')
})

it('rounded weight shares always sum to 100', () => {
  for (const shares of [[0.214, 0.214, 0.2, 0.145, 0.068, 0.041, 0.023, 0.023, 0.023, 0.018, 0.014, 0.009, 0.009], [1 / 3, 1 / 3, 1 / 3], [0.5, 0.5]]) {
    expect(roundShares(shares).reduce((a, b) => a + b, 0)).toBe(100)
  }
})

it('board rows: champions = 3+3 long with offline history, challengers = discovered with intervals, thin samples carry no excess', () => {
  const catalog = [
    { id: 'x', name: 'X', kind: 'builtin', enabled: true, forward: null, history: { n: 500, mean_r: 0.3, excess: 0.08, p: 0.01 } },
    { id: 'rk', name: '排序 · 六十日动量', kind: 'ranker', enabled: false, forward: null, candidate: { passed: false, n: 1080, excess: 0.07, excess_ci: [-0.01, 0.14] } },
    { id: 'ai1', name: 'AI 一', kind: 'ai', enabled: false, forward: null, candidate: { passed: false, n: 3, excess: 0.1 } },
    { id: 'b2', name: '不相干内置', kind: 'builtin', enabled: false, forward: null, history: { n: 100, mean_r: 0.2, excess: 0.3, p: 0.1 } },
  ] as unknown as WatchlistRuleCatalogItem[]
  const rows = boardRows(['x'], catalog)
  expect(rows.map(r => [r.id, r.role])).toEqual([['x', 'champ'], ['rk', 'ch'], ['ai1', 'ch']])
  expect(rows[0].p).toBe(0.01)
  expect(rows[1].lo).toBe(-0.01)
  expect(rows[2].excess).toBeNull()
  expect(rows[2].note).toBe('样本少 3 笔')
})

it('factor ids inside strategy names are shown with library labels', () => {
  const lib = new Map([['annual_vol_20d', libItem('annual_vol_20d', '20日波动率')]])
  expect(humanizeName('因子组合 · annual_vol_20d', lib)).toBe('因子组合 · 20日波动率')
  expect(humanizeName('QS-IBS', lib)).toBe('QS-IBS')
})

it('scoring challengers: pending intake formulas, watched custom factors, and steady unused builtins', () => {
  const lib = new Map([
    ['a', libItem('a', 'A')], ['z', libItem('z', 'Z')], ['w', libItem('w', 'W')],
    ['uf', libItem('uf', '自定义', { origin: 'intake', status: 'watch' })],
  ])
  const ic = weeklyIcTable([{ week: 'W1', result: result([['a', 0.01, true], ['z', 0.03, true], ['w', 0.02, false]]) },
    { week: 'W2', result: result([['a', 0.01, true], ['z', 0.028, true], ['w', -0.02, false]]) }])
  const { uses } = scoringUses([strat('s', { a: 1 }, [])], lib, ic, 2, new Set())
  const page = { intake: { cards: [{ id: 'c1', stage: 'pending', source_name: '某站', formulas: [{ name: '新公式', direction: 'low', screen: { ic: -0.02 } }] }] } } as unknown as FactorLabPage
  const ch = scoringChallengers(uses, lib, ic, page, 2)
  expect(ch.map(c => [c.label, c.tag])).toEqual([['新公式', 'pending'], ['自定义', 'watch'], ['Z', 'pool']])
  expect(ch[0].dirMean).toBeCloseTo(0.02)    // 声明值小利多, IC −0.02 折算为 +0.02
  // w 两周方向不一致, 不进挑战者
  expect(ch.find(c => c.id === 'w')).toBeUndefined()
})

// ---------------------------------------------------------------- render
const page = fixture as unknown as FactorLabPage
function seed() {
  mocks.factorLabPage.mockResolvedValue(page)
  mocks.oppPage.mockResolvedValue({ settings: { long: ['x'], short: [], risk_per_trade_pct: 0.5, ruleset_version: 1, evo: false } })
  mocks.watchlistRules.mockResolvedValue({ catalog: [
    { id: 'x', name: 'X 策略', kind: 'builtin', enabled: true, forward: null, history: { n: 500, mean_r: 0.3, excess: 0.08, p: 0.01 } },
    { id: 'rk', name: '排序 · 六十日动量', kind: 'ranker', enabled: false, forward: null, candidate: { passed: false, n: 1080, excess: 0.07, excess_ci: [-0.01, 0.14] } },
  ] })
  mocks.screenerStrategies.mockResolvedValue({ presets: [strat('x', { change_pct: 0.6, momentum_60d: 0.4 }, ['突破']), strat('y', { change_pct: 1 }, ['回踩'])] })
  mocks.factorLibrary.mockResolvedValue({ factors: [libItem('change_pct', '日涨跌幅'), libItem('momentum_60d', '60日动量'), libItem('close_position', '收盘位置')] })
  mocks.miningRuns.mockResolvedValue({ items: ['W43', 'W44'].map((wk, i) => ({ run_id: `weekly-2026-${wk}`, source: 'scheduled', status: 'succeeded', created_at: `2026-10-2${i}T00:00:00Z` })) })
  mocks.miningResult.mockResolvedValue(result([['change_pct', -0.01, true], ['momentum_60d', 0.014, true], ['close_position', 0.03, true]]))
}
async function render(node: React.ReactNode, ready: string) {
  seed()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>) })
  for (let i = 0; i < 40 && !host.querySelector(ready); i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('slots view: board and stock share one stretched row, every slot has a ruler and a footer', async () => {
  await render(<FactorSlots onInspect={() => {}} onOpenReview={() => {}} />, '[data-score-row="champ"] [data-sel-dots]')
  const row = host.querySelector('[data-slot-row]')!
  expect(row.className).toContain('items-stretch')
  expect([...row.children].map(c => c.getAttribute('data-slot'))).toEqual(['board', 'stock'])
  for (const slot of ['board', 'stock', 'scoring']) {
    const el = host.querySelector(`[data-slot="${slot}"]`)!
    expect(el.querySelector('[data-ruler]')!.textContent).toMatch(/^口径 · /)
    expect(el.querySelector('[data-slot-footer]')).not.toBeNull()
    // 卡片本身是 flex 列, 页脚 mt-auto 才能在同高时贴底
    expect(el.className).toContain('flex-col')
  }
  expect(host.querySelectorAll('[data-board-row="champ"]')).toHaveLength(1)
  expect(host.querySelectorAll('[data-board-row="ch"]')).toHaveLength(1)
  expect(host.querySelector('[data-stock-row="champ"]')).not.toBeNull()
  const flagged = host.querySelector('[data-score-row="champ"][data-flag="方向相反"]')!
  expect(flagged.textContent).toContain('日涨跌幅')
  // 挑战者: 周度方向一致、未被打分引用的内置因子
  expect([...host.querySelectorAll('[data-score-row="ch"]')].map(r => r.textContent).join('|')).toContain('收盘位置')
})

it('matrix view: one row per scoring factor, family columns, warning ring on reversed factors', async () => {
  await render(<FactorMatrix />, '[data-matrix-row] [data-sel-dots]')
  expect([...host.querySelectorAll('[data-matrix-row]')].map(r => r.getAttribute('data-matrix-row'))).toEqual(['change_pct', 'momentum_60d'])
  const heads = [...host.querySelectorAll('[data-matrix-table] th')].map(t => t.textContent)
  for (const f of ['突破', '趋势', '回踩', '反转']) expect(heads.some(h => h?.startsWith(f))).toBe(true)
  const cells = host.querySelector('[data-matrix-row="change_pct"]')!.querySelectorAll('[data-matrix-cell]')
  // 突破 + 回踩 + 其中 3+3 (x)
  expect(cells).toHaveLength(3)
  for (const c of cells) expect(c.className).toContain('ring-warning')
  expect(host.querySelector('[data-matrix-row="momentum_60d"] [data-matrix-cell]')!.className).not.toContain('ring-warning')
})

it('factor page: slots is the home view, tools group the old tabs, old tab addresses keep working', () => {
  const src = readFileSync(resolve('src/pages/Factors.tsx'), 'utf8')
  expect(src).toMatch(/: \(searchParams\.get\('focus'\) \? 'inspect' : 'slots'\)/)
  for (const t of ['inspect', 'editor', 'composite', 'mining']) expect(src).toContain(`key: '${t}'`)
  for (const t of ['matrix', 'discover', 'library']) expect(src).toContain(`requestedTab === '${t}'`)
})
