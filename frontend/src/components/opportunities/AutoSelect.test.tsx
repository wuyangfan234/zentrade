// @vitest-environment jsdom
// 2026-11-18 自动选策略: 机会页卡片 + 齿轮面板 (开关 / 候补 / 撤回); 机会卡网格等高守门。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type AutoSelectRow, type AutoSelectView } from '@/lib/api'
import { AutoCard, AutoPanel, lastLine, rowTitle, shortName, VERDICT_ZH } from './AutoSelect'
import { CARD, CARD_FOOT, GRID } from './Cards'
import { BANNED } from './terms.test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

let root: Root
let host: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks() })

const row = (id: string, side: 'long' | 'short', verdict: AutoSelectRow['verdict'], excess: number | null, x: Partial<AutoSelectRow> = {}): AutoSelectRow =>
  ({ id, side, name: id, verdict, excess, hits: 50, filled: 30, days: 22, ci: excess === null ? null : [excess - 0.1, excess + 0.1], ...x })

const view = (x: Partial<AutoSelectView> = {}): AutoSelectView => ({
  enabled: true, scored: 9, days: 31, cells: 2900, window: ['2026-10-15', '2026-12-03'], as_of: '2026-12-04',
  need_days: 20, need_filled: 15, cooldown_days: 20, next_eval: '2026-12-11', last_eval: null, swaps: [],
  picks: { long: [row('IBS 均值回归', 'long', 'above', 0.21), row('收盘上穿 ZLSMA（零滞后均线）', 'long', 'unclear', 0.02), row('新策略', 'long', 'thin', null, { days: 4, filled: 3 })],
    short: [row('UT Bot 翻空', 'short', 'early', 0.4, { days: 9 }), row('HalfTrend 翻空', 'short', 'below', -0.2), row('SMC 结构翻空', 'short', 'unclear', 0)] },
  bench: [row('低波动龙头', 'long', 'above', 0.3), row('QS-MDD', 'long', 'below', -0.17), row('ZLSMA 下穿', 'short', 'above', 0.41)], ...x,
})

const swap = { id: '2026-12-04-long', day: '2026-12-04', side: 'long' as const, out: 'c', in: 'd', out_name: 'QS-MDD', in_name: '低波动龙头', out_excess: -0.17, in_excess: 0.3, undone_at: null }

it('card lists the six picks with excess and plain-language status, and opens the console', async () => {
  const onOpen = vi.fn()
  await act(async () => { root.render(<AutoCard v={view()} onOpen={onOpen} />) })
  const rows = [...host.querySelectorAll('[data-auto-row]')]
  expect(rows.map((r) => r.getAttribute('data-verdict'))).toEqual(['above', 'unclear', 'thin', 'early', 'below', 'unclear'])
  expect(rows[0].textContent).toContain('+0.21')
  expect(rows[0].textContent).toContain('领先')
  expect(rows[1].textContent).toContain('收盘上穿 ZLSMA')
  expect(rows[1].textContent).not.toContain('零滞后')                                  // 行内短名, 全名在悬停
  expect(rows[1].getAttribute('title')).toContain('零滞后均线')
  expect(rows[2].querySelector('[data-auto-excess]')?.textContent).toBe('4/20')       // 样本不足写进度, 不写数字
  expect(rows[4].textContent).toContain('−0.20')
  expect(host.querySelector('[data-auto-state]')?.textContent).toBe('自动')
  expect(host.querySelector('[data-auto-next]')?.textContent).toBe('下次评定 12-11 周五 收盘后')
  expect(host.querySelector('[data-auto-last]')?.textContent).toBe('已记分 31 个信号日')
  expect(host.textContent).toContain('候补 3')
  expect(host.textContent).not.toMatch(/thin|early|above|below|unclear/)             // 英文状态码不上屏
  await act(async () => { (host.querySelector('[data-auto-open]') as HTMLButtonElement).click() })
  expect(onOpen).toHaveBeenCalledTimes(1)
})

it('card says 手动 when switched off and reports the last swap or evaluation', async () => {
  await act(async () => { root.render(<AutoCard v={view({ enabled: false, swaps: [swap] })} onOpen={() => {}} />) })
  expect(host.querySelector('[data-auto-state]')?.textContent).toBe('手动')
  expect(host.querySelector('[data-auto-next]')?.textContent).toContain('下次记录')
  expect(lastLine(view({ swaps: [swap] }))).toBe('上次调整 12-04 · QS-MDD → 低波动龙头')
  expect(lastLine(view({ swaps: [{ ...swap, undone_at: 'x' }], last_eval: { day: '2026-12-04', enabled: true, sides: {} } }))).toBe('上次评定 12-04 · 未调整')
})

it('helpers: every backend verdict has a label; titles carry sample and interval', () => {
  expect(Object.keys(VERDICT_ZH).sort()).toEqual(['above', 'below', 'early', 'thin', 'unclear'])
  expect(shortName('UT Bot（ATR 追踪止损）翻空')).toBe('UT Bot 翻空')
  expect(rowTitle(row('a', 'long', 'thin', null, { days: 4, filled: 3, hits: 6 }), 20)).toBe('a · 4 个信号日 · 成交 3 笔 · 命中 6 次 · 满 20 个信号日后评定')
  expect(rowTitle(row('a', 'long', 'above', 0.3), 20)).toContain('95% 区间 +0.20 ~ +0.40')
})

const mount = async (v: AutoSelectView) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><AutoPanel fallback={v} /></QueryClientProvider>) })
}

it('panel: switch is a named switch that posts the opposite state; candidates sit in capped scroll lists', async () => {
  const set = vi.spyOn(api, 'setAutoSelect').mockResolvedValue(view({ enabled: false }))
  vi.spyOn(api, 'autoSelect').mockResolvedValue(view({ enabled: false }))
  await mount(view())
  const sw = host.querySelector('[data-auto-switch]') as HTMLButtonElement
  expect([sw.getAttribute('role'), sw.getAttribute('aria-checked'), sw.getAttribute('aria-label')]).toEqual(['switch', 'true', '自动选策略'])
  for (const side of ['long', 'short']) {
    const b = host.querySelector(`[data-auto-bench="${side}"]`)!
    expect(b.className).toMatch(/max-h-\[\d+px\]/)
    expect(b.className).toContain('overflow-hidden')
    expect(b.querySelector('.overflow-y-auto')?.className).toContain('min-h-0')
  }
  expect(host.querySelectorAll('[data-auto-bench="long"] [data-auto-row]').length).toBe(2)
  expect(host.querySelector('[data-auto-interval]')).not.toBeNull()
  expect(host.querySelector('[data-auto-swaps]')).toBeNull()                          // 没有换入记录不占位
  await act(async () => { sw.click() })
  expect(set).toHaveBeenCalledWith(false)
})

it('panel: swap log offers undo only for swaps still in force, and shows what the last evaluation did', async () => {
  const undo = vi.spyOn(api, 'undoAutoSelect').mockResolvedValue(view())
  vi.spyOn(api, 'autoSelect').mockResolvedValue(view())
  await mount(view({ swaps: [swap, { ...swap, id: '2026-11-06-short', side: 'short', undone_at: '2026-11-07T10:00:00' }],
    last_eval: { day: '2026-12-04', enabled: true, sides: { long: { action: 'swap', out_name: 'QS-MDD', in_name: '低波动龙头', applied: true }, short: { action: 'none', reason: 'cooldown' } } } }))
  expect(host.querySelectorAll('[data-auto-swap]').length).toBe(2)
  expect(host.querySelectorAll('[data-auto-undo]').length).toBe(1)
  expect(host.querySelector('[data-auto-swap="2026-11-06-short"]')?.textContent).toContain('已撤回')
  expect(host.querySelector('[data-auto-eval="long"]')?.textContent).toBe('↑ 做多 · QS-MDD → 低波动龙头')
  expect(host.querySelector('[data-auto-eval="short"]')?.textContent).toBe('↓ 做空 · 调整后 20 个交易日内不动')
  await act(async () => { (host.querySelector('[data-auto-undo]') as HTMLButtonElement).click() })
  expect(undo).toHaveBeenCalledWith('2026-12-04-long')
})

it('机会卡 / 持仓卡网格等高: auto-rows-fr + 统一最小高度, 末行贴底; 不用 items-start', () => {
  expect(GRID).toContain('auto-rows-fr')
  expect(GRID).not.toContain('items-start')
  expect(CARD).toMatch(/min-h-\[\d+px\]/)
  expect(CARD).toContain('flex-col')
  expect(CARD_FOOT).toContain('mt-auto')
})

it('源码里的中文字面量不含机会页禁用词', () => {
  const src = readFileSync(resolve(__dirname, 'AutoSelect.tsx'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  for (const b of BANNED.filter((x) => x !== '。')) expect(src, b).not.toContain(b)
})
