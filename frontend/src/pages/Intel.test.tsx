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
vi.mock('@/lib/api', async (orig) => {
  const m = await orig<typeof import('@/lib/api')>()
  return { ...m, api: { ...m.api, intelToday: () => intelToday(), intelSettings: (x: string) => intelSettings(x), intelRefresh: vi.fn() } }
})

import { Intel, PAIR_BODY, PAIR_CARD, filterNews, fmtPct, fmtUsd, heatCls, intelPollMs, quoteSub, rev30, surpriseLabel } from './Intel'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })

const P = prod as unknown as IntelPayload

async function render(p: IntelPayload) {
  intelToday.mockResolvedValue(p)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter><Intel /></MemoryRouter></QueryClientProvider>)
  })
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

it('renders all sections from the production payload', async () => {
  await render(P)
  for (const k of ['tape', 'calendar', 'news', 'earnings', 'sources']) expect(host.querySelector(`[data-intel="${k}"]`)).not.toBeNull()
  expect(host.querySelectorAll('[data-heat]').length).toBe(18)                 // 12 板块 + 6 跨资产
  expect(host.querySelector('[data-intel-cell="QQQ"]')?.textContent).toContain('非当日')   // 周一盘前: 指数是上周五收盘
  expect(host.querySelector('[data-intel-cell="pcr"]')?.textContent).toContain('0.67')
  expect(host.querySelector('[data-intel-breadth]')?.textContent).toContain('3100 只')
  expect(host.querySelectorAll('[data-earn]').length).toBe(6)
  expect(host.querySelector('[data-intel="sources"]')?.textContent).toContain('数据源正常')
})

it('earnings row compares implied move with historical median', async () => {
  await render(P)
  const msft = host.querySelector('[data-earn="MSFT"]')!
  expect(msft.querySelector('[data-implied]')?.textContent).toBe('±5.3%0.9×历史')     // 5.30 / 6.12
  expect(msft.textContent).toContain('$4.71')
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
  const tr = host.querySelector('[data-earn="ARM"]')!
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
  expect(spy.textContent).toContain('盘前 08:04 · 对官方前收')
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

// 0.45.0: 日历与新闻并排两卡宽屏等高, 只有中间列表在卡内滚动; 头部筛选、底部「全部/收起」不随列表滚走
it('calendar and news share one fixed-height card with an inner scrolling list', async () => {
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
  for (const c of cards) expect(c.className).toBe(PAIR_CARD)
  const calBody = host.querySelector<HTMLElement>('[data-pair-body="calendar"]')!
  const newsBody = host.querySelector<HTMLElement>('[data-pair-body="news"]')!
  expect(calBody.className).toContain(PAIR_BODY)
  expect(newsBody.className).toBe(PAIR_BODY)
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
