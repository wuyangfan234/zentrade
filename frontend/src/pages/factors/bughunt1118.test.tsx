// @vitest-environment jsdom
// 2026-11-18 清查复现: 因子页 / 机会页的显示值与真实数据对不上的几处。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AutoSelectRow, AutoSelectView, FormulaMinerRow, FormulaMinerView, OppRadarView, WatchlistRuleCatalogItem } from '@/lib/api'

const mocks = vi.hoisted(() => ({ formulaMiner: vi.fn(), autoSelect: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { boardRows, fmtR, navTicks } from './FactorSlots'
import { FormulaMinerPanel, opErrorText } from './FormulaMiner'
import { AutoPanel, rowTitle } from '@/components/opportunities/AutoSelect'
import { opensLeft } from '@/components/opportunities/SideRail'
import { gapText } from '@/components/opportunities/terms'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const flush = async () => { for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
const wrap = (el: React.ReactNode) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{el}</QueryClientProvider>

it('an in-use ranker shows its own replay instead of "no replay, 0 trades"', () => {
  // 线上 11-18: 入选做多里的「排序 · 波动扩张」复核 1196 笔 / +0.071R, 槽位 1 却写「离线回放 · 0 笔 · 无回放」
  const catalog = [
    { id: 'ibs', name: 'QS-IBS', kind: 'builtin', enabled: true, forward: null, history: { n: 1323, mean_r: 0.2, excess: 0.06, p: 0.035 } },
    { id: 'rank_59ae3b44ef', name: '排序 · 波动扩张', kind: 'ranker', enabled: false, forward: null, history: null,
      candidate: { passed: false, n: 1196, excess: 0.071, excess_ci: [-0.0096, 0.1535] } },
    { id: 'ai_x', name: 'AI 策略', kind: 'ai', enabled: false, forward: null, history: null, candidate: null },
  ] as unknown as WatchlistRuleCatalogItem[]
  const rows = boardRows(['ibs', 'rank_59ae3b44ef', 'ai_x'], catalog)
  expect(rows.map((r) => r.role)).toEqual(['champ', 'champ', 'champ'])
  expect(rows[0]).toMatchObject({ n: 1323, excess: 0.06, p: 0.035, note: null })           // 内置策略照旧读离线回放
  expect(rows[1]).toMatchObject({ kind: 'ranker', n: 1196, excess: 0.071, lo: -0.0096, hi: 0.1535, note: null })
  expect(rows[2]).toMatchObject({ n: null, excess: null, note: '无回放' })                 // 真没有回放的仍写无回放
})

it('a value that rounds to zero is not printed as minus zero', () => {
  expect(fmtR(-0.0004)).toBe('0.00R')
  expect(fmtR(0.004)).toBe('0.00R')
  expect(fmtR(-0.006)).toBe('−0.01R')
  expect(fmtR(0.071)).toBe('+0.07R')
  const r = { id: 'a', side: 'long', name: 'A', verdict: 'unclear', excess: -0.004, ci: [-0.2, 0.2], hits: 9, filled: 9, days: 22 } as AutoSelectRow
  expect(rowTitle(r, 20)).toContain('超额 0.00 R')
})

it('net value axis keeps a handful of ticks however high the curve goes', () => {
  // 线上 11-18: 候选净值到 51, 采集页纵轴每个整数一条刻度 (约 50 个标签叠在 215px 里); 总览只画 1~6, 上面九成没有刻度
  const hi = navTicks(0.6, 51)
  expect(hi.length).toBeGreaterThanOrEqual(3)
  expect(hi.length).toBeLessThanOrEqual(7)
  expect(Math.max(...hi)).toBeGreaterThan(25)
  expect(hi).toContain(1)                                                                  // 起点 1.00 的参考线保留
  expect(navTicks(0.9, 4.7)).toEqual([1, 2, 3, 4])
  expect(navTicks(0.9, 1.6)).toEqual([1, 1.5])
})

it('opens left today cannot exceed the free position slots', () => {
  // 线上 11-18: 持仓 7 / 12 只, 账户卡写「今日还能开 10 笔」
  const caps = (used: number, cap: number | null, opens = 0) =>
    ({ opens: { used: opens, cap: 10, day: '2026-11-18' }, positions: { used, cap } }) as unknown as OppRadarView['caps']
  expect(opensLeft(caps(7, 12))).toBe(5)
  expect(opensLeft(caps(2, 12, 4))).toBe(6)
  expect(opensLeft(caps(12, 12))).toBe(0)
  expect(opensLeft(caps(7, null))).toBe(10)                                                // 读不到持仓上限时只按当日上限
})

it('a tiny distance to the entry zone is not printed as 0.0%', () => {
  expect(gapText(0.02)).toBe('还差 <0.1%')
  expect(gapText(0.05)).toBe('还差 0.1%')
  expect(gapText(1.44)).toBe('还差 1.4%')
})

const mrow = (id: string, extra: Partial<FormulaMinerRow> = {}): FormulaMinerRow => ({
  id, name: `公式 ${id}`, formula: '(close/ts_min(low,10)-1)', direction: 'low', status: 'retired', reason: 'timeout', admitted_day: '2026-11-18',
  retired_day: '2027-02-20', sat_days: 60, replay: null, forward: { hits: 12, filled: 6, days: 3, excess: null, ci: null, verdict: 'thin' }, ...extra,
})

it('retired table: the count in its title is the number of rows shown, and the list scrolls inside a capped box', async () => {
  // 后端只下发最近 20 条已退席, 总数另给; 标题写总数会出现「已退席 35」下面 20 行
  const v = { enabled: true, running: false, pool_ready: true, bench: [], selected: [],
    retired: Array.from({ length: 20 }, (_, i) => mrow(`r${i}`)),
    totals: { tried: 900, admitted: 41, retired: 35, selected: 0, above: 0, outcomes: {} },
    caps: { bench: 6, per_run: 2, tenure_days: 60, need_days: 20, need_filled: 15 }, last_run: null, runs: [] } as unknown as FormulaMinerView
  mocks.formulaMiner.mockResolvedValue(v)
  await act(async () => { root.render(wrap(<FormulaMinerPanel />)) })
  await flush()
  const box = host.querySelector('[data-miner-retired]')!
  expect(box.querySelectorAll('[data-miner-row]').length).toBe(20)
  expect(box.textContent).toContain('已退席 · 最近 20 / 35')
  const body = box.querySelector('[data-miner-body]')!
  expect(body.className).toMatch(/max-h-\[\d+px\]/)
  expect(body.className).toContain('overflow-y-auto')
})

it('operation errors that already name the failed action are not prefixed a second time', () => {
  expect(opErrorText('立即挖掘失败 · 交易日 09:00-17:00 (美东) 不运行')).toBe('立即挖掘失败 · 交易日 09:00-17:00 (美东) 不运行')
  expect(opErrorText('HTTP 500')).toBe('操作失败 · HTTP 500')
})

it('cooldown reason uses the cooldown the backend reports', async () => {
  const v = { enabled: true, scored: 9, days: 31, cells: 2900, window: null, as_of: '2026-12-04', need_days: 20, need_filled: 15, cooldown_days: 15,
    next_eval: '2026-12-11', swaps: [], picks: { long: [], short: [] }, bench: [],
    last_eval: { day: '2026-12-04', enabled: true, sides: { long: { action: 'none', reason: 'cooldown' }, short: { action: 'none', reason: 'no_candidate' } } },
  } as unknown as AutoSelectView
  mocks.autoSelect.mockResolvedValue(v)
  await act(async () => { root.render(wrap(<AutoPanel fallback={v} />)) })
  await flush()
  expect(host.querySelector('[data-auto-eval="long"]')!.textContent).toContain('调整后 15 个交易日内不动')
})
