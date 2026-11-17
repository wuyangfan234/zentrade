// @vitest-environment jsdom
// 0.40.0 盘面情报页: 用 10-26 盘前生产干跑的真实载荷 (截短) 渲染; 守门 = 非今日标注、实际值/待同步、财报隐含 vs 历史、新闻筛选、区块隔离
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { IntelEvent, IntelNewsItem, IntelPayload } from '@/lib/api'
import prod from './__fixtures__/intel-snapshot-20261026.json'

vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: ({ symbol }: { symbol: string | null }) => (symbol ? <div data-preview={symbol} /> : null) }))
const intelToday = vi.fn()
const intelSettings = vi.fn()
const intelAsof = vi.fn()
const intelAsofDays = vi.fn()
vi.mock('@/lib/api', async (orig) => {
  const m = await orig<typeof import('@/lib/api')>()
  return { ...m, api: { ...m.api, intelToday: () => intelToday(), intelSettings: (x: string) => intelSettings(x), intelRefresh: vi.fn(),
    intelAsof: (day: string) => intelAsof(day), intelAsofDays: () => intelAsofDays() } }
})

import { EMBED_BODY, EMBED_CARD, Intel, NOT_ARCHIVED, PAIR_BODY, PAIR_CARD, asofLabel, calendarLead, filterNews, fmtPct, fmtUsd, heatCls, hotEarnings, impliedRatio, intelPollMs, quoteSub, rev30, surpriseLabel } from './Intel'
import { CARD_HEAD, CARD_LEAD, GRID_CARD, challengerBadge, driftCount, failText, fmtAtr } from '@/components/intel/IntelGrid'
import type { IntelModel } from '@/lib/api'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

const P = prod as unknown as IntelPayload

async function render(p: IntelPayload) {
  intelToday.mockResolvedValue(p)
  if (!intelAsofDays.getMockImplementation()) intelAsofDays.mockResolvedValue({ today: '2026-10-26', days: [] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter><Intel /></MemoryRouter></QueryClientProvider>)
  })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

it('renders all sections from the production payload', async () => {
  await render(P)
  for (const k of ['tape', 'breadth', 'calendar', 'news', 'earnings', 'model', 'sources']) expect(host.querySelector(`[data-intel="${k}"]`)).not.toBeNull()
  expect(host.querySelectorAll('[data-heat]').length).toBe(18)                 // 12 板块 + 6 跨资产
  expect(host.querySelector('[data-intel-cell="QQQ"]')?.textContent).toContain('非当日')   // 周一盘前: 指数是上周五收盘
  expect(host.querySelector('[data-intel-cell="pcr"]')?.textContent).toContain('0.67')
  expect(host.querySelector('[data-intel="breadth"]')?.textContent).toContain('3100 只')
  expect(host.querySelectorAll('[data-earn]').length).toBe(6)
  expect(host.querySelector('[data-intel="sources"]')?.textContent).toContain('数据源正常')
})

it('earnings row compares implied move with historical median', async () => {
  await render(P)
  const msft = host.querySelector('[data-earn="MSFT"]')!
  expect(msft.querySelector('[data-implied]')?.textContent).toBe('±5.3%0.9×历史')     // 5.30 / 6.12
  expect(msft.textContent).not.toContain('$4.71')                                    // EPS 预期收进「展开」
  await act(async () => { (host.querySelector('[data-expand="earnings"]') as HTMLButtonElement).click() })
  const full = host.querySelector('[data-expand-body="earnings"] [data-earn="MSFT"]')!
  expect(full.textContent).toContain('$4.71')
  expect(full.querySelector('[data-implied]')?.textContent).toBe('±5.3%0.9×历史')     // 两处同一个数
  expect(host.querySelector('[data-earn="GOOGL"] [data-implied]')?.textContent).toBe('—')
})

it('calendar shows actual, pending and unmapped states', async () => {
  const ev = (over: Partial<IntelEvent>): IntelEvent => ({ kind: 'macro', at: '2026-10-29T08:30-04:00', day: '2026-10-29', time: '08:30', title: 'x', past: true, ...over })
  expect(surpriseLabel(ev({ actual: '3.5%', surprise: 'above' }))).toBe('高于预期')
  expect(surpriseLabel(ev({ actual: '0.3%', surprise: 'inline' }))).toBe('符合预期')
  expect(surpriseLabel(ev({ actual_status: 'pending' }))).toBe('实际待同步')
  expect(surpriseLabel(ev({ actual_status: 'unmapped' }))).toBe('已公布')
  expect(surpriseLabel(ev({ actual_status: 'mismatch' }))).toBe('已公布 · 推算口径未对齐，不显示')
  expect(surpriseLabel(ev({ actual: '0.4%', surprise: 'near' }))).toBe('接近预期')
  const day = P.calendar!.today
  const withActual: IntelPayload = {
    ...P, calendar: { ...P.calendar!, events: [
      ev({ day, at: `${day}T08:30-04:00`, title: 'Advance GDP q/q', title_zh: 'GDP 季环比年化 (初值)', impact: 'high', forecast: '3.2%', previous: '1.5%', actual: '3.5%', surprise: 'above', actual_status: 'ok' }),
      ev({ day, at: `${day}T10:00-04:00`, time: '10:00', title: 'CB Consumer Confidence', impact: 'medium', forecast: '80.5', actual_status: 'unmapped' }),
    ] },
  }
  await render(withActual)
  const rows = host.querySelectorAll('[data-cal-event="macro"]')
  expect(rows[0].querySelector('[data-actual]')?.textContent).toBe('实际 3.5%')
  expect(rows[0].textContent).toContain('GDP 季环比年化 (初值)')
  expect(rows[1].querySelector('[data-surprise]')?.textContent).toBe('已公布')
})

it('impact level radio posts the setting', async () => {
  intelSettings.mockResolvedValue({ macro_impact: 'high' })
  await render(P)
  const btn = [...host.querySelectorAll<HTMLButtonElement>('[data-intel-levels] button')].find((b) => b.textContent === '仅高')!
  await act(async () => { btn.click() })
  expect(intelSettings).toHaveBeenCalledWith('high')
})

it('news filters by role, direction and macro', () => {
  const n = (over: Partial<IntelNewsItem>) => ({ id: Math.random().toString(), symbol: 'X', role: '自选', direction: null, event_type: null, ...over }) as IntelNewsItem
  const items = [n({ role: '持仓', direction: 'bullish' }), n({ direction: 'bearish' }), n({ macro: true, role: null })]
  expect(filterNews(items, 'hold').length).toBe(1)
  expect(filterNews(items, 'bearish').length).toBe(1)
  expect(filterNews(items, 'macro').length).toBe(1)
  expect(filterNews(items, 'all').length).toBe(3)
})

it('failed section only blanks that section', async () => {
  await render({ ...P, earnings: null, errors: { earnings: 'RuntimeError' } })
  expect(host.querySelector('[data-intel="earnings"]')?.textContent).toContain('读取失败')
  expect(host.querySelectorAll('[data-heat]').length).toBe(18)
  expect(host.querySelector('[data-intel="sources"]')?.textContent).toContain('区块读取失败: earnings')
})

it('heat classes are literal and poll interval follows phase', () => {
  expect(heatCls(2.5)).toBe('bg-bull/35 text-foreground')
  expect(heatCls(-1.2)).toBe('bg-bear/20 text-foreground')
  expect(heatCls(0.3)).toBe('bg-bull/[0.08] text-foreground')
  expect(heatCls(null)).toBe('bg-elevated text-muted')
  expect(intelPollMs({ phase: { phase: 'session' } } as IntelPayload)).toBe(60_000)
  expect(intelPollMs({ phase: { phase: 'closed' } } as IntelPayload)).toBe(300_000)
})

it('number formatting: no negative zero, negative eps, revision on negative base', () => {
  expect(fmtPct(-0.001, 1)).toBe('0.0%')
  expect(fmtPct(-1.234)).toBe('-1.23%')
  expect(fmtUsd(-0.18)).toBe('-$0.18')
  expect(rev30({ qveris: { eps_avg: -0.46, eps_change_30d_pct: 1.558 } } as never)).toBeNull()
  expect(rev30({ qveris: { eps_avg: 2.0, eps_change_30d_pct: 0.05 } } as never)).toBe(0.05)
})

it('earnings row shows adjusted consensus with source and flags date conflict', async () => {
  const row = { ...P.earnings!.rows[0], symbol: 'ARM', eps_consensus: 0.4734, eps_source: 'QVeris' as const, eps_nasdaq: 0.22, date_conflict: '2026-11-05' }
  await render({ ...P, earnings: { ...P.earnings!, rows: [row] } })
  expect(host.querySelector('[data-earn="ARM"] [data-date-conflict]')?.textContent).toBe('待确认')   // 卡上短写, 全称在悬停
  expect(host.querySelector('[data-earn="ARM"] [data-date-conflict]')?.getAttribute('title')).toContain('日期待确认')
  await act(async () => { (host.querySelector('[data-expand="earnings"]') as HTMLButtonElement).click() })
  const tr = host.querySelector('[data-expand-body="earnings"] [data-earn="ARM"]')!
  expect(tr.textContent).toContain('$0.47')
  expect(tr.querySelector('[data-eps-src]')?.textContent).toBe('QVeris')
  expect(tr.querySelector('[data-date-conflict]')?.textContent).toBe('日期待确认')
})


// 0.42.4: 指数格必须显示现价 (原来只有涨跌幅), 盘前 / 盘中 / 盘后标时段与时刻
it('index cell shows price and live session label', async () => {
  const p = structuredClone(P)
  p.tape!.indexes = [
    { symbol: 'SPY', name: '标普500', price: 767.47, chg_pct: 0.27, live: true, session: 'pre', time_et: '08:04', day: '2026-10-26', base: '官方前收' },
    { symbol: 'QQQ', name: '纳指100', price: 721.25, chg_pct: 1.27, live: false, day: '2026-10-23', base: '官方收盘' },
  ]
  await render(p)
  const spy = host.querySelector('[data-intel-cell="SPY"]')!
  expect(spy.querySelector('[data-price]')?.textContent).toBe('767.47')
  expect(spy.textContent).toContain('+0.27%')
  expect(spy.querySelector('[data-quote-sub]')?.textContent).toBe('盘前 08:04')              // 卡上只留时段与时刻
  expect(spy.querySelector('[data-quote-sub]')?.getAttribute('title')).toBe('盘前 08:04 · 对官方前收')   // 口径在悬停
  expect(spy.textContent).not.toContain('非当日')
  expect(host.querySelector('[data-intel-cell="QQQ"] [data-price]')?.textContent).toBe('721.25')
  expect(quoteSub({ symbol: 'X', name: 'X', live: true, session: 'post', time_et: '16:22' }, '2026-10-27')).toBe('盘后 16:22 · 对官方前收')
  expect(quoteSub({ symbol: 'X', name: 'X', live: true }, '2026-10-27')).toBe('对官方前收')
})

it('news filter caps each view at limit and shows per-filter counts', async () => {
  const p = structuredClone(P)
  const base = p.news!.items[0]
  const mk = (i: number, role: string) => ({ ...base, id: `n${i}`, symbol: role === '持仓' ? 'TSLA' : 'NVDA', role, direction: 'neutral', published_at: `2026-10-26T${String(10 + Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}:00Z` })
  p.news!.items = [...Array.from({ length: 45 }, (_, i) => mk(100 - i, '自选')), mk(1, '持仓')] as NonNullable<typeof p.news>['items']
  p.news!.counts = { all: 46, hold: 1, bullish: 0, bearish: 0, macro: 0 }
  p.news!.limit = 40
  await render(p)
  expect(host.querySelectorAll('[data-news-row]').length).toBe(40)
  expect(host.querySelector('[data-news-filter="hold"]')?.textContent).toBe('持仓 1')
  await act(async () => { (host.querySelector('[data-news-filter="hold"]') as HTMLButtonElement).click() })
  expect(host.querySelectorAll('[data-news-row]').length).toBe(1)
})

// 0.45.0: 本周之后的高重要度事件 (TradingView 前瞻) 收起时也要看得到, 全天事件写「全天」
it('calendar shows next-week high-impact lookahead even when collapsed', async () => {
  const day = P.calendar!.today
  const ev = (over: Partial<IntelEvent>): IntelEvent => ({ kind: 'macro', at: `${day}T08:30-05:00`, day, time: '08:30', title: 'x', past: false, impact: 'medium', ...over })
  const cal = { ...P.calendar!, source: '本周 ForexFactory · 之后 3 周 TradingView · 实际值 FRED', lookahead_from: '2099-01-05', events: [
    ev({ title: 'ISM Manufacturing PMI', title_zh: 'ISM 制造业 PMI', forecast: '54.7' }),
    ev({ day: '2099-01-06', at: '2099-01-06T08:30-05:00', title: 'Inflation Rate YoY', title_zh: 'CPI 同比', impact: 'high', previous: '3.7%', source: 'tradingview' }),
    ev({ day: '2099-01-07', at: '2099-01-07T00:00', time: '', title: 'Midterm Elections', title_zh: '美国中期选举', impact: 'high', source: 'tradingview' }),
    ev({ day: '2099-01-08', at: '2099-01-08T10:00-05:00', title: 'Pending Home Sales MoM', impact: 'medium', source: 'tradingview' }),
  ] }
  await render({ ...P, calendar: cal })
  const ahead = [...host.querySelectorAll('[data-cal-event="ahead"]')].map((li) => li.textContent ?? '')
  expect(ahead.length).toBe(2)                                     // 只列高重要度; 中的点「全部」再看
  expect(ahead[0]).toContain('CPI 同比')
  expect(ahead[0]).toContain('前值 3.7%')
  expect(ahead[1]).toContain('全天')
  const dateCell = host.querySelector('[data-cal-event="ahead"] > span')!
  expect(dateCell.className).toContain('whitespace-nowrap')           // 「11-10 周二」不折成两行
  expect(host.querySelector('[data-cal-day="2099-01-06"]')).toBeNull()      // 前瞻日不挤占本周 3 天
  expect(host.querySelector('[data-intel="calendar"]')?.textContent).toContain('之后 3 周 TradingView')
  await act(async () => { (host.querySelector('[data-cal-more]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-cal-day="2099-01-08"]')?.textContent).toContain('前瞻')
  expect(host.querySelector('[data-cal-day="2099-01-07"] [data-source="tradingview"]')?.textContent).toContain('全天')
  expect(host.querySelectorAll('[data-cal-event="ahead"]').length).toBe(0)  // 展开后不重复列
})

// 0.45.0 起: 日历与新闻只有中间列表在卡内滚动; 头部筛选、底部「全部/收起」不随列表滚走。
// 0.57.0: 两卡进六宫格, 高度由网格行给 (EMBED_*); 「展开」里仍是原来的固定高卡 (PAIR_*)
it('calendar and news keep an inner scrolling list inside the grid cards', async () => {
  const p = structuredClone(P)
  const day = p.calendar!.today
  const ev = (d: string, i: number): IntelEvent => ({ kind: 'macro', at: `${d}T08:30-05:00`, day: d, time: '08:30', title: `e${i}`, past: d < day, impact: 'high' })
  const days = ['2026-10-19', '2026-10-20', '2026-10-21', day, '2099-01-05', '2099-01-06']
  p.calendar!.events = days.flatMap((d, k) => Array.from({ length: 6 }, (_, i) => ev(d, k * 10 + i)))
  await render(p)
  expect(PAIR_CARD).toMatch(/\bflex\b.*\bflex-col\b/)
  expect(PAIR_CARD).toMatch(/\blg:h-\[\d+px\]/)
  expect(PAIR_BODY).toMatch(/\bmin-h-0\b/)
  expect(PAIR_BODY).toMatch(/\boverflow-y-auto\b/)
  expect(PAIR_BODY).toMatch(/\blg:flex-1\b/)
  expect(PAIR_BODY).toMatch(/\bmax-h-\[\d+px\]/)                    // 窄屏列表也封顶, 不再整页拉长
  expect(PAIR_BODY).toMatch(/\bpr-/)                                // 手机浮层滚动条不压右侧股票代码
  const cards = [...host.querySelectorAll<HTMLElement>('[data-pair-card]')]
  expect(cards.map((c) => c.dataset.pairCard)).toEqual(['calendar', 'news'])
  for (const c of cards) expect(c.className).toBe(EMBED_CARD)
  expect(EMBED_CARD).toMatch(/\bflex\b.*\bmin-h-0\b.*\bflex-1\b.*\bflex-col\b/)   // 靠弹性收缩拿高度 (手机上卡只有最大高, 百分比高度不生效)
  expect(EMBED_BODY).toMatch(/\bmin-h-0\b/)
  expect(EMBED_BODY).toMatch(/\bflex-1\b/)
  expect(EMBED_BODY).toMatch(/\boverflow-y-auto\b/)
  expect(EMBED_BODY).toMatch(/\bpr-/)
  for (const c of cards) expect(c.closest('[data-card-body]')!.className).not.toContain('overflow-y-auto')   // 外层不滚, 否则筛选条跟着滚走
  const calBody = host.querySelector<HTMLElement>('[data-pair-body="calendar"]')!
  const newsBody = host.querySelector<HTMLElement>('[data-pair-body="news"]')!
  expect(calBody.className).toContain(EMBED_BODY)
  expect(newsBody.className).toBe(EMBED_BODY)
  expect(newsBody.hasAttribute('data-news-list')).toBe(true)
  // 列表在滚动区里, 控件在外面
  expect(calBody.querySelector('[data-cal-day]')).not.toBeNull()
  expect(newsBody.querySelector('[data-news-row]')).not.toBeNull()
  for (const sel of ['[data-intel-levels]', '[data-cal-more]']) {
    const el = host.querySelector(sel)!
    expect(calBody.contains(el)).toBe(false)
    expect(el.parentElement === cards[0] || cards[0].contains(el)).toBe(true)
  }
  expect(newsBody.contains(host.querySelector('[data-news-filter="all"]'))).toBe(false)
  for (const el of [cards[0].firstElementChild!, cards[1].firstElementChild!, host.querySelector('[data-cal-more]')!]) {
    expect(el.className).toContain('shrink-0')
  }
  // 展开全部: 卡内滚到今天, 不是从本周一顶部开始
  const rect = (top: number) => ({ top, bottom: top + 20, left: 0, right: 0, width: 0, height: 20, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
  calBody.getBoundingClientRect = () => rect(100)
  const proto = HTMLElement.prototype.getBoundingClientRect
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    const d = this.dataset?.calDay
    if (!d) return proto.call(this)
    return rect(100 + days.indexOf(d) * 150)
  }
  try {
    await act(async () => { (host.querySelector('[data-cal-more]') as HTMLButtonElement).click() })
    expect(host.querySelectorAll('[data-cal-day]').length).toBe(days.length)
    expect(calBody.scrollTop).toBe(3 * 150)                          // 今天是第 4 天
    await act(async () => { (host.querySelector('[data-cal-more]') as HTMLButtonElement).click() })
    expect(calBody.scrollTop).toBe(0)
  } finally {
    HTMLElement.prototype.getBoundingClientRect = proto
  }
})

// ForexFactory 缺失 (ff_until 为空) 时 TradingView 顶替本周: 这些日子正常按天列出, 不标「前瞻」, 也不进「下周起」
it('calendar treats TradingView days as this week when ForexFactory is missing', async () => {
  const day = P.calendar!.today
  const nxt = new Date(`${day}T12:00:00Z`); nxt.setUTCDate(nxt.getUTCDate() + 1)
  const d2 = nxt.toISOString().slice(0, 10)
  const ev = (over: Partial<IntelEvent>): IntelEvent => ({ kind: 'macro', at: `${day}T08:30-05:00`, day, time: '08:30', title: 'x', past: false, impact: 'high', source: 'tradingview', ...over })
  const cal = { ...P.calendar!, ff_until: null, source: 'TradingView 经济日历 (ForexFactory 暂缺) · 实际值 FRED', events: [
    ev({ title: 'Non Farm Payrolls', title_zh: '非农就业' }),
    ev({ day: d2, at: `${d2}T08:30-05:00`, title: 'Inflation Rate YoY', title_zh: 'CPI 同比' }),
  ] }
  await render({ ...P, calendar: cal })
  expect(host.querySelector(`[data-cal-day="${day}"]`)?.textContent).toContain('非农就业')
  expect(host.querySelector(`[data-cal-day="${d2}"]`)?.textContent).toContain('CPI 同比')
  expect(host.querySelector(`[data-cal-day="${d2}"]`)?.textContent).not.toContain('前瞻')
  expect(host.querySelectorAll('[data-cal-event="ahead"]').length).toBe(0)
})

// ── 0.57.0 六宫格 / 模型状态 / 历史日 ────────────────────────────────

const MODEL: IntelModel = {
  contract: 'ls-model-v1', asof: null, list_day: '2026-10-27', as_of: '2026-10-26',
  champion: { quarter: '2026Q4', name: 'GBM 2026Q4', trained_to: '2026-09-22', rows: 426912, since: '2026-10-06' },
  forward: { days: 12, check_days: 65, first: '2026-10-06', last_list: '2026-10-21', failed: null,
    total: { long_minus_all: 0.046, long: 0.12, short: -0.03, pair: 0.09, n_lists: 12, win_long: 0.55, win_short: 0.45, all: 0.075 } },
  challenger: null, gate: { n: 25, key: 'long_minus_all' },
  coverage: { ranked: 47, young: 3, missing: 0, stale: 0, total: 47 },
  next_retrain: { quarter: '2027Q1', train_end: '2026-12-24' },
  drift: { as_of: '2026-10-26', quarter: '2026Q4', warn: 0.25, ref: { from: '2025-09-23', to: '2026-09-22', n: 11000 }, cur: { from: '2026-09-28', to: '2026-10-26', n: 940 },
    // mom 0.40 高于 0.25 但没超过自己的线 0.94: 不算注意; on20 超过自己的线; ladv 没有 line (旧文件) 按底线 0.25, 等于也算
    rows: [{ key: 'rev1', psi: 0.03, base: 0.1, line: 0.25, warn: false }, { key: 'ibs', psi: 0.02, base: 0.09, line: 0.25, warn: false },
      { key: 'rev5', psi: 0.04, base: 0.22, line: 0.25, warn: false }, { key: 'mom', psi: 0.4, base: 0.94, line: 0.94, warn: false },
      { key: 'hksoc', psi: 0.06, base: 0.4, line: 0.4, warn: false }, { key: 'on20', psi: 1.75, base: 0.73, line: 0.73, warn: true },
      { key: 'atrp', psi: 0.11, base: 0.41, line: 0.41, warn: false }, { key: 'ladv', psi: 0.25 }] },
  registry: [{ quarter: '2026Q4', name: 'GBM 2026Q4', trained_to: '2026-09-22', rows: 426912, role: 'champion', first: '2026-10-06', last: '2026-10-27', days: 12, long_minus_all: 0.046 }],
  backtest: { period: '2024-01~2026-10', long_minus_all: 0.047 }, post: null,
}
const CHAL: NonNullable<IntelModel['challenger']> = {
  quarter: '2027Q1', name: 'GBM 2027Q1', trained_to: '2026-12-23', since: '2027-01-05', state: 'running', decided: null, n: 9, gate_n: 25, key: 'long_minus_all', lists: 14,
  champ: { long_minus_all: 0.06, long: 0.27, short: -0.13, pair: 0.14, n_lists: 9, win_long: 0.5, win_short: 0.5, all: 0.21 },
  chal: { long_minus_all: 0.03, long: 0.24, short: -0.12, pair: 0.12, n_lists: 9, win_long: 0.5, win_short: 0.5, all: 0.21 },
}

it('grid has six cards in a fixed order with one shared structure', async () => {
  await render({ ...P, model: MODEL })
  const grid = host.querySelector('[data-intel-grid]')!
  expect(grid.className).toBe('intel-grid')
  const cards = [...grid.querySelectorAll<HTMLElement>(':scope > [data-grid-card]')]
  expect(cards.map((c) => c.dataset.intel)).toEqual(['tape', 'breadth', 'calendar', 'news', 'earnings', 'model'])
  for (const c of cards) {
    expect(c.className).toBe(GRID_CARD)
    expect([...c.children].slice(0, 3).map((x) => Object.keys((x as HTMLElement).dataset)[0])).toEqual(['cardHead', 'cardLead', 'cardBody'])
    expect(c.querySelector('[data-card-head]')!.className).toBe(CARD_HEAD)
    expect(c.querySelector('[data-card-lead]')!.className).toBe(CARD_LEAD)
    expect(c.querySelector('[data-card-post]')).toBeNull()                       // 今天不出事后条
  }
  // 同排等高靠三样: 卡撑满网格行 + 标题行、结论行定高 + 明细吃掉剩余高度
  expect(GRID_CARD).toMatch(/\bflex\b.*\bmin-h-0\b.*\bflex-col\b.*\boverflow-hidden\b/)
  expect(GRID_CARD).toMatch(/\bmax-md:max-h-\[\d+px\]/)                         // 手机单列: 卡高封顶
  expect(CARD_HEAD).toMatch(/\bh-9\b.*\bshrink-0\b/)
  expect(CARD_LEAD).toMatch(/\bh-\[\d+px\].*\bshrink-0\b/)
  expect(CARD_LEAD).toMatch(/\boverflow-hidden\b/)
  for (const k of ['tape', 'breadth', 'earnings', 'model']) expect(host.querySelector(`[data-intel="${k}"] [data-card-body]`)!.className).toContain('overflow-y-auto')
})

it('lead lines are computed from the same rows the card lists', async () => {
  const p = structuredClone(P)
  const day = p.calendar!.today
  const ev = (d: string, time: string, title: string, impact: 'high' | 'medium', past: boolean): IntelEvent => ({ kind: 'macro', at: `${d}T${time}-05:00`, day: d, time, title, past, impact })
  p.calendar!.events = [ev(day, '08:30', 'a', 'high', true), ev(day, '10:00', 'b', 'medium', false), ev(day, '14:00', 'c', 'high', false), ev('2099-01-05', '08:30', 'd', 'high', false)]
  expect(calendarLead(p.calendar)).toMatchObject({ today: 2, next: { title: 'c' } })
  await render(p)
  expect(host.querySelector('[data-intel="calendar"] [data-lead-big]')?.textContent).toBe('2')
  expect(host.querySelector('[data-cal-next]')?.textContent).toContain(`下一件 ${day.slice(5)} 14:00`)
  const rows = p.earnings!.rows
  expect(host.querySelector('[data-intel="earnings"] [data-lead-big]')?.textContent).toBe(String(rows.length))
  expect(host.querySelectorAll('[data-intel="earnings"] [data-earn]').length).toBe(rows.length)
  const hot = rows.filter((r) => (impliedRatio(r) ?? 0) >= 1.25).length
  expect(hotEarnings(rows)).toBe(hot)
  expect(host.querySelector('[data-earn-hot]')?.textContent ?? null).toBe(hot ? `隐含 ≥ 1.25× 历史 · ${hot} 只` : null)
  expect(host.querySelector('[data-intel="breadth"] [data-lead-big]')?.textContent).toBe(`${p.breadth!.up_pct!.toFixed(0)}%`)
})

it('sentiment lead counts rows and focus, chips match the board', async () => {
  const row = (symbol: string, grade: 'A' | 'B', direction: 'bullish' | 'bearish') => ({ symbol, grade, heat: 80, direction, event_type: 'lawsuit', hard: true, first_at: null, n_families: 2, families_ok: [] })
  await render({ ...P, sentiment: { as_of: '2026-10-26T08:00:00Z', age_min: 3, rows: [row('NVDA', 'A', 'bearish'), row('MRNA', 'B', 'bullish'), row('INTC', 'B', 'bearish'), row('TSM', 'B', 'bullish')], total: 4, symbols: 47, sources: {} } })
  expect(host.querySelector('[data-intel="news"] [data-lead-big]')?.textContent).toBe('4')
  expect(host.querySelector('[data-sent-focus]')?.textContent).toBe('重点 1')
  expect([...host.querySelectorAll('[data-sent-chip]')].map((x) => (x as HTMLElement).dataset.sentChip)).toEqual(['NVDA', 'MRNA', 'INTC'])
  expect(host.querySelector('[data-sent-chip="NVDA"]')?.textContent).toBe('NVDA 利空')
  expect(host.querySelector('[data-sent-more]')?.textContent).toBe('+1')                 // 4 只列 3 只, 余数写出来
  await act(async () => { (host.querySelector('[data-sent-chip="MRNA"]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-preview="MRNA"]')).not.toBeNull()
})

it('model card shows health only, never the list; drawer opens from the card', async () => {
  await render({ ...P, model: MODEL })
  const card = host.querySelector('[data-intel="model"]')!
  expect(card.querySelector('[data-lead-big]')?.textContent).toBe('GBM 2026Q4')
  expect(card.querySelector('[data-model-badge]')?.getAttribute('data-model-badge')).toBe('none')
  const kv = (k: string) => card.querySelector(`[data-model-kv="${k}"]`)?.textContent
  expect(kv('forward')).toBe('前向12 日 · 看多 − 自选全体 +0.05 ATR')
  expect(kv('fail')).toBe('失败判据未到期 · 12 / 65 日')
  expect(kv('challenger')).toBe('挑战者无')
  expect(kv('coverage')).toBe('覆盖47 / 47 · 新股 3')
  expect(kv('drift')).toBe('特征漂移6 正常 · 2 注意')                          // 等于注意线也算注意
  expect(kv('retrain')).toBe('下次重训2027Q1 · 12-24 后')
  expect(card.textContent).not.toMatch(/看多 5|做多|股数|止损/)                 // 名单留在机会页
  expect(document.querySelector('[data-model-sheet]')).toBeNull()
  await act(async () => { (card.querySelector('[data-card-body]') as HTMLElement).click() })
  const sheet = document.querySelector('[data-model-sheet]')!
  expect(sheet.querySelector('[data-model-compare]')?.textContent).toContain('无挑战者 · 下次重训 2027Q1')
  expect(sheet.querySelectorAll('[data-psi]').length).toBe(8)
  expect([...sheet.querySelectorAll('[data-psi-warn="1"]')].map((x) => (x as HTMLElement).dataset.psi)).toEqual(['on20', 'ladv'])
  expect(sheet.querySelector('[data-psi="mom"]')?.textContent).toBe('动量0.40 / 0.94')        // 每个特征写出自己的线
  expect(sheet.querySelector('[data-psi="on20"]')?.textContent).toBe('隔夜收益1.75 / 0.73')
  expect(sheet.querySelector('[data-psi="ladv"] [data-psi-limit]')?.textContent).toBe(' / 0.25')
  expect(sheet.querySelectorAll('[data-psi-line]').length).toBe(8)               // 每条都画注意线
  expect(sheet.querySelector('[data-registry="2026Q4"]')?.textContent).toBe('GBM 2026Q409-22冠军12 份+0.05')
  await act(async () => { (sheet.querySelector('[aria-label="关闭"]') as HTMLButtonElement).click() })
  expect(document.querySelector('[data-model-sheet]')).toBeNull()
})

it('challenger states: running / rejected / promoted read differently on card and in the drawer', async () => {
  expect(driftCount(MODEL)).toEqual({ n: 8, warn: 2, ok: 6 })
  expect(failText({ ...MODEL, forward: { ...MODEL.forward, days: 65, failed: false } })).toBe('第 65 日 · 未触发')
  expect(failText({ ...MODEL, forward: { ...MODEL.forward, days: 65, failed: true } })).toBe('已触发')
  expect([fmtAtr(0.046), fmtAtr(-0.004), fmtAtr(-0.13), fmtAtr(null)]).toEqual(['+0.05', '0.00', '−0.13', '—'])
  expect(challengerBadge({ ...MODEL, challenger: { ...CHAL, state: 'promoted' } }).props['data-model-badge']).toBe('promoted')
  await render({ ...P, model: { ...MODEL, challenger: CHAL } })
  const kv = (k: string) => host.querySelector(`[data-model-kv="${k}"]`)?.textContent
  expect(host.querySelector('[data-model-badge]')?.textContent).toBe('挑战者对照中')
  expect(kv('challenger')).toBe('挑战者GBM 2027Q1 · 同窗 9 / 25 份')
  expect(kv('verdict')).toBe('裁决未到期')
  await act(async () => { (host.querySelector('[data-expand="model"]') as HTMLButtonElement).click() })
  const sheet = document.querySelector('[data-model-sheet]')!
  expect(sheet.querySelector('[data-compare="long_minus_all"]')?.textContent).toBe('看多 − 自选全体门槛+0.06+0.03低于冠军')
  expect(sheet.querySelector('[data-compare="short"]')?.textContent).toBe('看空每笔 · 越低越好−0.13−0.12低于冠军')   // 做空每笔越负越好
  expect(sheet.querySelector('[data-model-sum-cell="verdict"]')?.textContent).toBe('裁决对照中同窗 9 / 25 份')
  await act(async () => { (sheet.querySelector('[aria-label="关闭"]') as HTMLButtonElement).click() })
  act(() => root.unmount())
  root = createRoot(host)
  await render({ ...P, model: { ...MODEL, challenger: { ...CHAL, state: 'rejected', decided: '2027-02-11', n: 25 } } })
  expect(host.querySelector('[data-model-badge]')?.textContent).toBe('挑战者未晋级')
  expect(kv('challenger')).toBe('挑战者GBM 2027Q1 · +0.03 对 +0.06')
  expect(kv('verdict')).toBe('裁决02-11 · 保留冠军')
})

it('earnings card adds a detail line per symbol only when rows are few', async () => {
  await render(P)                                                                // 6 只: 不加第二行
  expect(host.querySelectorAll('[data-intel="earnings"] [data-earn-detail]').length).toBe(0)
  act(() => root.unmount())
  root = createRoot(host)
  const rows = P.earnings!.rows.slice(0, 3)
  await render({ ...P, earnings: { ...P.earnings!, rows } })
  const det = [...host.querySelectorAll<HTMLElement>('[data-intel="earnings"] [data-earn-detail]')]
  expect(det.map((x) => x.dataset.earnDetail)).toEqual(rows.map((r) => r.symbol))
  expect(host.querySelectorAll('[data-intel="earnings"] [data-earn]').length).toBe(3)   // 行数口径不变
  const r0 = rows[0]
  expect(det[0].textContent).toContain(`EPS 预期 ${fmtUsd(r0.eps_consensus ?? r0.eps_forecast)}`)
  if (r0.hist?.median_abs_move != null) expect(det[0].textContent).toContain(`历史中位 ±${r0.hist.median_abs_move.toFixed(1)}%`)
})

it('no model lists yet: card says so and has no drawer entry', async () => {
  await render({ ...P, model: null })
  const card = host.querySelector('[data-intel="model"]')!
  expect(card.querySelector('[data-missing]')?.textContent).toBe('尚无名单')
  expect(card.querySelector('[data-expand]')).toBeNull()
})

const ASOF: IntelPayload = {
  ...P, model: { ...MODEL, asof: '2026-10-22', drift: null, post: { day: '2026-10-22', end: '2026-10-28', long: 0.21, short: -0.08 } },
  asof: { day: '2026-10-22', archived: true, archived_at: '2026-10-22T18:00:12-04:00' },
  post: { tape: [{ label: 'SPY 次日', pct: -0.38 }, { label: 'SPY 5 日', pct: -2.31 }], news: [], model: [{ label: '当天名单 看多', atr: 0.21 }, { label: '看空', atr: -0.08 }] },
}

async function toHistory(payload: IntelPayload, days = ['2026-10-23', '2026-10-22']) {
  intelAsofDays.mockResolvedValue({ today: '2026-10-26', days })
  intelAsof.mockResolvedValue(payload)
  await render({ ...P, model: MODEL })
  await act(async () => { (host.querySelector('[data-asof-tab="asof"]') as HTMLButtonElement).click() })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

it('history tab reads the frozen day, stops refresh, and keeps hindsight off by default', async () => {
  expect(asofLabel('2026-11-10')).toBe('11-10 周二')
  await toHistory(ASOF)
  expect(intelAsof).toHaveBeenCalledWith('2026-10-23')                           // 默认最近一个历史日
  expect(host.querySelector('[data-page="intel"]')?.getAttribute('data-intel-mode')).toBe('asof')
  expect(host.querySelector('[data-asof-banner]')?.textContent).toContain('10-23 周五 收盘')
  expect(host.querySelector('[data-asof-banner]')?.textContent).toContain('存档于 10-22 18:00 ET')
  expect(host.querySelector('[data-intel-refresh]')).toBeNull()                  // 历史日不能刷新
  expect([...host.querySelectorAll<HTMLOptionElement>('[data-asof-day] option')].map((o) => o.textContent)).toEqual(['10-23 周五', '10-22 周四'])
  expect(host.querySelectorAll('[data-grid-card]').length).toBe(6)
  expect(host.querySelectorAll('[data-card-post]').length).toBe(0)               // 事后默认关: 不用后来的结果解释当时
  expect(host.querySelector('[data-post-toggle]')?.textContent).toBe('事后 · 关')
  for (const b of host.querySelectorAll<HTMLButtonElement>('[data-intel-levels] button')) expect(b.disabled).toBe(true)   // 历史日不改设置
  expect(intelSettings).not.toHaveBeenCalled()
  await act(async () => { (host.querySelector('[data-post-toggle]') as HTMLButtonElement).click() })
  const posts = [...host.querySelectorAll('[data-card-post]')]
  expect(posts.length).toBe(6)                                                    // 六张卡都有一条, 没有的写无结算项
  const text = (k: string) => host.querySelector(`[data-intel="${k}"] [data-card-post]`)?.textContent
  expect(text('tape')).toBe('事后SPY 次日 −0.38%SPY 5 日 −2.31%')
  expect(text('model')).toBe('事后当天名单 看多 +0.21 ATR看空 −0.08 ATR')
  for (const k of ['breadth', 'calendar', 'news', 'earnings']) expect(text(k)).toBe('事后无结算项')
  expect(host.querySelector('[data-model-kv="drift"]')?.textContent).toBe('特征漂移未存档')
  await act(async () => { (host.querySelector('[data-asof-tab="today"]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-asof-banner]')).toBeNull()
  expect(host.querySelector('[data-intel-refresh]')).not.toBeNull()
  expect(host.querySelectorAll('[data-card-post]').length).toBe(0)
})

it('day without an archive marks every block and still shows the model', async () => {
  const keys = ['mood', 'tape', 'vol', 'rates', 'pcr', 'breadth', 'calendar', 'earnings', 'actions', 'news', 'sentiment']
  await toHistory({
    contract: 'intel-v1', generated_at: null, phase: null, settings: { macro_impact: 'high_medium' }, mood: null, tape: null, vol: null, rates: null, pcr: null,
    breadth: null, calendar: null, earnings: null, actions: null, news: null, sentiment: null, watch_count: null, state: {},
    model: { ...MODEL, asof: '2026-10-23', drift: null }, asof: { day: '2026-10-23', archived: false, archived_at: null },
    post: { tape: [], news: [], model: [] }, errors: Object.fromEntries(keys.map((k) => [k, NOT_ARCHIVED])),
  })
  expect(host.querySelector('[data-asof-banner]')?.textContent).toContain('当天情报未存档 · 只有模型状态')
  for (const k of ['tape', 'breadth', 'calendar', 'news', 'earnings']) {
    expect(host.querySelector(`[data-intel="${k}"] [data-missing]`)?.textContent).toBe('当天未存档')
    expect(host.querySelector(`[data-intel="${k}"] [data-expand]`)).toBeNull()   // 没有内容可展开
    expect(host.querySelector(`[data-intel="${k}"] [data-card-lead]`)?.textContent).toBe('—当天未存档')   // 不写 0: 0 会被读成「确认没有」
    expect(host.querySelector(`[data-intel="${k}"]`)?.textContent).not.toContain('读取失败')
  }
  expect(host.querySelector('[data-intel="model"] [data-lead-big]')?.textContent).toBe('GBM 2026Q4')
  expect(host.querySelector('[data-intel="sources"]')?.textContent).not.toContain('区块读取失败')   // 未存档不是故障
  expect(host.querySelectorAll('[data-heat]').length).toBe(0)
})

it('expand shows the full section and closes; tape expand keeps 5-day columns', async () => {
  await render(P)
  expect(host.querySelector('[data-expand-body]')).toBeNull()
  expect(host.querySelectorAll('[data-d5]').length).toBe(0)                      // 卡上只有当日, 5 日在展开里
  await act(async () => { (host.querySelector('[data-expand="breadth"]') as HTMLButtonElement).click() })
  const body = host.querySelector('[data-expand-body="breadth"]')!
  expect(body.querySelectorAll('[data-heat]').length).toBe(18)
  expect(body.querySelectorAll('[data-d5]').length).toBeGreaterThan(0)
  expect(body.querySelector('[data-intel-breadth]')?.textContent).toContain('3100 只')
  await act(async () => { (host.querySelector('[data-expand-close]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-expand-body]')).toBeNull()
  await act(async () => { (host.querySelector('[data-expand="news"]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-expand-body="news"] [data-pair-card="news"]')!.className).toBe(PAIR_CARD)
  expect(host.querySelector('[data-expand-body="news"] [data-pair-body="news"]')!.className).toBe(PAIR_BODY)
})
