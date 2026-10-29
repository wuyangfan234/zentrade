// @vitest-environment jsdom
// 0.41.0 机会页 = 状态栏 (保留) + 大盘状态条 + 共振 + 四栏同屏 + 持仓 (10-28 产品决策 「非显然档」)。
// 不变量: 一个请求 (/page) 取齐; 四栏顺序随大盘状态; 某块失败只空一块; 整页失败 = 错误不是“无机会”; 齿轮一个保存口;
// 系统文字只写结果 (无说明句、无口语)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { IntradayCard, OppDesk, OppPagePayload, OppRadarView, RadarBlip, SwingBoardCard } from '@/lib/api'

const mocks = vi.hoisted(() => ({
  oppPage: vi.fn(), saveOppSettings: vi.fn(), watchlistRules: vi.fn(), newsSummary: vi.fn(), qverisOptions: vi.fn(), runSwingCandidates: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: (p: { symbol: string; navList: { symbol: string }[] }) => <div data-preview={p.symbol} data-nav={p.navList.map((x) => x.symbol).join(',')} />,
}))
import { Opportunities } from './Opportunities'

const card = (symbol: string, over: Partial<SwingBoardCard> = {}): SwingBoardCard => ({
  contract: 'swing-desk-tight-v1', symbol, side: 'long', score: 1, hit_count: 1, strategies: [{ id: 'macd_golden', name: 'MACD 金叉放量' }],
  gate: 'event', close: 100, entry_zone: [97, 98], stop: 95, target: 104, risk: 2, rr: 2.43, stop_pct: -3, target_pct: 6,
  entry_days: 2, hold_days: 5, as_of: '2026-10-19', signal_day: '2026-10-19', entry_day_n: 1, state: 'pending', price: 99,
  price_source: 'live', dist_pct: -1, verdict: 'order', ...over,
})

const order = (symbol: string, over: Partial<RadarBlip> = {}): RadarBlip => ({
  symbol, side: 'short', kind: 'order', ring: 1, r: 0.48, d: 0.25, bearing: 90, group: '芯片', codes: [],
  order: { card_id: `c-${symbol}`, zone: [101, 102], stop: 107, target: 90, qty: 49, risk_usd: 488, price: 100, price_source: 'live', dist_pct: 1,
    atr: 4, signal_day: '2026-10-22', day_n: 1, entry_days: 2, valid_until: '2026-10-26T15:30', rules: ['吊灯翻空'], gate: 'event', odds: { fill: 0.6, ev_r: 0.1, ci_r: [-0.1, 0.3], n: 120 },
    sector: 'Technology', earnings: null },
  changed_at: null, dir: null, ...over,
})
const hold = (symbol: string, over: Partial<RadarBlip> = {}): RadarBlip => ({
  symbol, side: 'short', kind: 'hold', ring: 2, r: 0.67, d: null, bearing: 200, group: '算力',
  hold: { qty: 35, avg: 213.8, price: 210.33, price_source: 'close', price_asof: '2026-10-23', stop_now: 228.27, stop_initial: 228.27, stop_moved: false,
    target: 184.66, entry_date: '2026-10-23', day_n: 1, max_days: 5, exit_day: '2026-10-29', exit_at: '15:30', exit_code: 'time_exit', stop_dist_atr: 1.23,
    r_now: 0.27, pnl_usd: 120, earnings: null },
  ...over,
})
const quiet = (symbol: string, bearing: number): RadarBlip => ({ symbol, side: null, kind: 'quiet', ring: 4, r: 0.94, d: null, bearing, group: '芯片' })

const view = (over: Partial<OppRadarView> = {}): OppRadarView => ({
  contract: 'opp-radar-v1', at: '2026-10-23T10:39:00-04:00',
  clock: { et: '2026-10-23T10:39:00-04:00', trading_day: true, phase: 'session', entry_window: true, entry_close: '15:30', close: '16:00', board_first: '08:00',
    session_day: '2026-10-23', next: { day: '2026-10-23', at: '11:00', code: 'intraday_check', ts: '2026-10-23T11:00:00-04:00' } },
  verdict: { kind: 'go', code: 'orders', orders: 2, blocked: [], holds: 1, exits: [], exit_at: '15:30', nearest: { symbol: 'MSFT', d: 0.25 } },
  caps: { slots: [{ key: 'Technology:short', sector: 'Technology', side: 'short', used: 2, cap: 4, free: 2, holders: ['ARM', 'COHR'], waiting: ['MSFT', 'META'], near: [], state: 'ok', release_by: [], release_at: null }],
    sector_cap: 4, risk_pct: 0.005, opens: { used: 1, cap: 10, day: '2026-10-23' }, positions: { used: 1, cap: 12 }, portfolio_risk: { used: 0.014, cap: 0.06, gate: 'green' } },
  lanes: { board: { ticks: ['08:00', '08:30'], window: ['09:45', '15:30'], fills: [{ at: '10:04', symbol: 'COHR', side: 'short' }], full: [], orders: 2, refresh_last: '10:30' },
    gap: { at: '09:00', live: false, warming: false, n: 2, threshold: 3 }, open: { at: '09:51', ready_at: '09:51', picks: ['AAPL'], eligible: 40, push: { enabled: true, min_rvol: 3, today: { symbols: [], at: null } } },
    intraday: { ticks: ['10:00', '10:30'], schedule: ['10:00', '10:30', '11:00'], fired: 0, active: 1, next: '11:00' } },
  blips: [order('MSFT'), order('META', { d: 0.5, r: 0.58, bearing: 120 }), hold('ARM'),
    { symbol: 'NFLX', side: null, kind: 'watch', ring: 3, r: 0.84, d: null, bearing: 300, group: '消费娱乐', also: [{ src: 'intraday', r_now: 0.4 }] },
    quiet('AAA', 30), quiet('BBB', 60)],
  price_sources: { live: 3 }, stale_prices: false, frames: 4, last_frame_at: '2026-10-23T10:38:00-04:00', missing: [],
  ...over,
})

const icard = (symbol: string, over: Partial<IntradayCard> = {}): IntradayCard => ({
  contract: 'intraday-strength-v1', card_id: `ids-${symbol}`, symbol, side: 'long', day: '2026-10-23', check: '10:00', fired_at: '2026-10-23T10:00:04-04:00', slot: 6,
  entry_ref: 100, entry: 100.05, stop: 98, target: 104.1, risk: 2.05, stop_pct: -2, target_pct: 4, hold_days: 5, status: 'open', price: 101, r_now: 0.46, day_n: 1, ...over,
})

const desk = (over: Partial<OppDesk['regime']> = {}): OppDesk => ({
  contract: 'opp-desk-v1',
  regime: { key: 'up', spy: 0.54, qqq: 1.25, n: 44, up: 34, down: 2, up3: 22, chg: { BABA: 3.9, NBIS: 11.32, TSLA: 3.88 }, at: '2026-10-23T10:39:00-04:00', ...over },
  resonance: [{ symbol: 'TSLA', side: 'long', n: 3, chg: 3.88, price: 406.87, shadow_at: '2026-10-23T10:05:00-04:00',
    engines: { open: { rank: 3, rvol: 1.24 }, intraday: { cards: [{ day: '2026-10-23', r: 0.55 }, { day: '2026-10-22', r: 1.45 }] }, board: { state: 'pending' } } }],
  shadow: { rule: 'resonance-shadow-v1', n: 1, settled: 0, mean5: null, win5: null, since: '2026-10-23' },
})

const page = (over: Partial<OppPagePayload> = {}): OppPagePayload => ({
  contract: 'opp-page-v1', generated_at: '2026-10-23T10:39:00-04:00',
  phase: { phase: 'session', label: '盘中', at: '2026-10-23T10:39' },
  swing: {
    contract: 'swing-desk-tight-v1', available: true, as_of: '2026-10-22', long_rules: ['macd_golden'], short_rules: ['tv_halftrend'],
    long: [card('BABA'), card('TSLA', { entry_zone: [384.51, 388.07], stop: 373.85, price: 406.87, dist_pct: -4.62, odds: { fill: 0.084, ev_r: 0.02, ci_r: [-0.02, 0.07], sig: 0, n: 452, level: 'x', day: 1, phase: 'pm', bin: 'gt1', dist_atr: 1.3, qty: 41, risk_usd: 510, ev_usd: 12, ci_usd: [-12, 37] } })], short: [card('META', { side: 'short', verdict: 'skip', verdict_reason: 'premarket_beyond_stop' })],
    history: [], refresh: { count: 1, last_at: '2026-10-23T10:30:00-04:00', changes: [] },
  },
  intraday: {
    contract: 'intraday-strength-v1', today: '2026-10-23', now: '2026-10-23T10:39:00-04:00', active: [icard('TSLA')],
    history: [icard('AVGO', { status: 'closed', exit_reason: 'stop', exit_at: '2026-10-23T10:15', r: -1.05, r_now: null })], stats: { n: 4, win: 0, mean_r: -1.03, sum_r: -4.11 },
    replay: { n: 1, win: 0.4, mean_r: 0.1, ci_r: [-0.1, 0.2], train_mean_r: 0.1, period: 'x', sig: 0 }, checks_today: [], next_check: '11:00',
    rule: { min_chg: 0.02, min_rvol: 1.5, min_er: 0.3, min_above: 0.7, max_pull: 0.33, hold_days: 5, target_r: 2 },
  },
  mood: null,
  ctx: { BABA: { news: { count_24h: 1, tone: { bearish: 0, bullish: 1, bearish_heads: [], bullish_heads: ['x'] } }, pcr: 0.46 } },
  settings: { long: ['macd_golden'], short: ['tv_halftrend'], risk_per_trade_pct: 0.5, ruleset_version: 45, evo: false },
  radar_view: view(),
  gaps: { at: '2026-10-23T09:00:00-04:00', live: false, threshold_pct: 3, checked: 44, rows: [{ symbol: 'NBIS', gap_pct: 4.26, price: 159.8 }] },
  radar: { contract: 'open-radar-v1', today: '2026-10-23', ready_at: '09:51', pending: false,
    replay: { period: 'x', hit: 0.32, other: 0.13, ctl_hit: 0.2, ctl_other: 0.05, after_0935_mean_pct: 0.26 }, stats: null, day: '2026-10-23', is_today: true,
    at: '2026-10-23T09:51:00-04:00', eligible: 44, others: [],
    picks: [{ symbol: 'TSLA', rvol: 1.24, dir: 1, open: 400, or_high: 401, or_low: 395.25, or_close: 400.8, prev_close: 391.7, gap_pct: 2.1, or_chg_pct: 2.3, atr: 14, pick: true, rank: 3, chg_now_pct: 3.88 }] },
  desk: desk(),
  errors: {},
  ...over,
})

const rules = {
  ruleset: { version: 45, enabled: ['macd_golden'], params: { risk_per_trade_pct: 0.5 } },
  history_meta: { window: ['2023-01-03', '2026-10-12'], universe: 'watchlist 45 (2026-10-20)', data_end: '2026-10-20', generated: '2026-10-21T11:47:26+08:00' },
  catalog: [
    { id: 'macd_golden', name: 'MACD 金叉放量', kind: 'builtin', enabled: true, forward: null, history: { n: 7, mean_r: 0.1, excess: 0.02 }, short_capable: false },
    { id: 'trend_breakout', name: '趋势突破', kind: 'builtin', enabled: false, forward: null, history: null, short_capable: false },
    { id: 'tv_halftrend', name: 'HalfTrend 翻空', kind: 'builtin', enabled: false, forward: null, history: null, short_capable: true },
  ],
  candidates: { generated_at: '2026-10-21T00:41:34+08:00', window: ['2023-01-03', '2026-10-19'], running: false, pending_at: null },
  short_rules: [], versions: [45], forward: { n: 0, months: 0, excess: null, p: null, threshold: 0.05, promoted: false },
  rule: { start: '2026-10-06', min_n: 100, min_months: 3 },
}

let qc: QueryClient, root: Root, host: HTMLDivElement
async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><Opportunities /></QueryClientProvider>) })
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.oppPage.mockResolvedValue(page())
  mocks.watchlistRules.mockResolvedValue(rules)
  mocks.saveOppSettings.mockResolvedValue({ swing: { long: [], short: [] }, ruleset: { version: 46, risk_per_trade_pct: 0.75 } })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); document.body.innerHTML = '' })
const text = (sel: string) => host.querySelector(sel)?.textContent ?? ''

it('一个请求取齐整页: 不逐票请求期权 / 新闻; 规则目录只在开齿轮时取', async () => {
  await render()
  expect(mocks.oppPage).toHaveBeenCalledTimes(1)
  for (const k of ['newsSummary', 'qverisOptions', 'watchlistRules'] as const) expect(mocks[k], k).not.toHaveBeenCalled()
  expect(text('[data-swing-head]')).toContain('信号日 10-22')
})

it('状态栏保留: 时钟 + 时段 + 下一事件 + 名额 + 泳道; 雷达图 / 页签 / 引擎明细已删', async () => {
  await render()
  expect(text('[data-rail-clock]')).toContain('10:39')
  expect(text('[data-rail-clock]')).toContain('北京 22:39')
  expect(text('[data-rail-phase]')).toContain('盘中 · 入场窗')
  expect(text('[data-rail-next]')).toContain('11:00 盘中检查')
  expect(text('[data-cap="slot"]')).toContain('科技·空')
  expect(host.querySelector('[data-lane="board"]')).not.toBeNull()
  for (const sel of ['[data-sweep]', '[data-readout]', '[data-feed]', '[data-engine-details]', '[data-details-toggle]', '[data-stage-tab]', '[data-mark]']) {
    expect(host.querySelector(sel), sel).toBeNull()
  }
})

it('名额格进度条按占用比例填充: 组合风险 1.4%/6% = 23%, 持仓 1/12 = 8%', async () => {
  await render()
  const fill = (k: string) => (host.querySelector(`[data-cap="${k}"] i`) as HTMLElement | null)?.style.width
  expect(text('[data-cap="risk"]')).toContain('1.4%/6%')
  expect(fill('risk')).toBe('23%')
  expect(fill('positions')).toBe('8%')
})

it('大盘状态条: 普涨 + 自选宽度 + SPY / QQQ', async () => {
  await render()
  expect(host.querySelector('[data-regime]')!.getAttribute('data-regime')).toBe('up')
  expect(text('[data-regime-badge]')).toBe('普涨')
  expect(text('[data-regime-breadth]')).toBe('自选涨≥1% 34/44 · 涨≥3% 22 · 跌≥1% 2')
  expect(text('[data-regime-index]')).toBe('SPY +0.54% · QQQ +1.25%')
})

it.each([
  ['up', ['intraday', 'open', 'gap', 'board']],
  ['normal', ['board', 'intraday', 'open', 'gap']],
  ['down', ['board', 'open', 'gap', 'intraday']],
  [null, ['board', 'intraday', 'open', 'gap']],
] as const)('四栏顺序随大盘状态: %s', async (key, order) => {
  mocks.oppPage.mockResolvedValue(page({ desk: desk({ key }) }))
  await render()
  expect([...host.querySelectorAll('[data-col]')].map((e) => e.getAttribute('data-col'))).toEqual(order)
  expect([...host.querySelectorAll('[data-col]')].every((e) => !e.className.includes('opacity'))).toBe(true)      // 末栏不变淡
})

it('共振: 同票多引擎; 影子标记; 栏内同票带圆点', async () => {
  await render()
  const r = host.querySelector('[data-res="TSLA"]')!
  expect(r.textContent).toContain('3 引擎')
  expect(r.textContent).toContain('开盘放量 #3 1.2×')
  expect(r.textContent).toContain('盘中强势 10-22 +1.45R')
  expect(r.textContent).toContain('机会板 挂单')
  expect(text('[data-res="TSLA"] [data-res-shadow]')).toBe('影子 10:05')
  expect(host.querySelector('[data-intraday-row="TSLA"] [data-res-mark]')).not.toBeNull()
  expect(host.querySelector('[data-board-row="BABA"] [data-res-mark]')).toBeNull()
})

it('四栏内容: 跳空现在涨幅 / 开盘点名 / 盘中在途与今日已止损 / 机会板回区率', async () => {
  await render()
  expect(text('[data-gap-row="NBIS"]')).toContain('跳空 +4.3%')
  expect(text('[data-gap-row="NBIS"]')).toContain('+11.32%')
  expect(text('[data-open-row="TSLA"]')).toContain('#3 · 相对量 1.2×')
  expect(text('[data-intraday-row="TSLA"]')).toContain('+0.46R')
  expect(text('[data-intraday-closed="AVGO"]')).toContain('10:15 止损')
  expect(text('[data-col="intraday"]')).toContain('已结算 4 · −4.11R')
  expect(text('[data-board-row="TSLA"]')).toContain('回区 8%')
  expect(text('[data-board-row="TSLA"]')).toContain('距区 4.6%')
})

it('四栏同一高度, 跳空多时栏内滚动', async () => {
  const rows = Array.from({ length: 18 }, (_, i) => ({ symbol: `G${i}`, gap_pct: -3 - i * 0.4, price: 100 }))
  mocks.oppPage.mockResolvedValue(page({ gaps: { at: '2026-10-29T04:28:00-04:00', live: true, threshold_pct: 3, checked: 41, rows } }))
  await render()
  expect(host.querySelectorAll('[data-gap-row]').length).toBe(18)
  const bodies = [...host.querySelectorAll('[data-col] > [data-col-body]')]
  expect(bodies.length).toBe(4)
  for (const b of bodies) {
    expect(b.className).toContain('max-h-[320px]')
    expect(b.className).toContain('md:h-[320px]')
    expect(b.className).toContain('overflow-y-auto')
  }
  expect(host.querySelector('[data-col="gap"] [data-col-body] [data-gap-row="G17"]')).not.toBeNull()
})

it('持仓表读台账: 当前止损 (原止损) + 浮动', async () => {
  const v = view({ blips: [hold('IREN', { kind: 'exit', ring: 0, hold: { ...hold('IREN').hold!, qty: 198, avg: 27.69, stop_now: 27.68, stop_initial: 30.29, stop_moved: true, day_n: 5 } })] })
  mocks.oppPage.mockResolvedValue(page({ radar_view: v }))
  await render()
  const row = text('[data-hold="IREN"]')
  expect(row).toContain('27.68 (30.29)')
  expect(row).toContain('5/5')
})

it('某块失败只空那一块', async () => {
  mocks.oppPage.mockResolvedValue(page({ desk: null, radar: null, errors: { desk: 'KeyError', radar: 'KeyError' } }))
  await render()
  expect(text('[data-section-error="大盘状态"]')).toBe('大盘状态读取失败（KeyError）')
  expect(text('[data-col="open"] [data-col-error]')).toBe('开盘放量读取失败')
  expect(host.querySelector('[data-intraday-row="TSLA"]')).not.toBeNull()
  expect([...host.querySelectorAll('[data-col]')].map((e) => e.getAttribute('data-col'))).toEqual(['board', 'intraday', 'open', 'gap'])
})

it('整页请求失败 = 错误, 不是“无机会”', async () => {
  mocks.oppPage.mockRejectedValue(new Error('HTTP 503'))
  await render()
  expect(text('[data-error]')).toContain('HTTP 503')
  expect(host.querySelector('[data-col]')).toBeNull()
})

it('点共振卡打开预览; 左右切票顺序 = 共振 → 各栏', async () => {
  await render()
  await act(async () => { (host.querySelector('[data-res="TSLA"]') as HTMLElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const pv = host.querySelector('[data-preview]')!
  expect(pv.getAttribute('data-preview')).toBe('TSLA')
  expect(pv.getAttribute('data-nav')).toBe('TSLA,NBIS,BABA,META')
})

it('齿轮: 读当前 3+3 与风险; 一个保存按钮发 3+3 + 风险; 风险越界不能存', async () => {
  await render()
  await act(async () => { (host.querySelector('[data-gear]') as HTMLElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const c = document.body.querySelector('[data-console]') as HTMLElement
  expect(mocks.watchlistRules).toHaveBeenCalledTimes(1)
  const save = c.querySelector('[data-save]') as HTMLButtonElement
  const risk = c.querySelector('[data-risk]') as HTMLInputElement
  expect(save.disabled).toBe(true)
  await act(async () => { (c.querySelector('[data-swing-list="long"] [data-swing-pick="trend_breakout"] input') as HTMLInputElement).click() })
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => { setter.call(risk, '5'); risk.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(save.disabled).toBe(true)
  await act(async () => { setter.call(risk, '0.75'); risk.dispatchEvent(new Event('input', { bubbles: true })) })
  await act(async () => { save.click() })
  expect(mocks.saveOppSettings).toHaveBeenCalledWith(['macd_golden', 'trend_breakout'], ['tv_halftrend'], 0.75, null)
})

it('系统文字只写结果: 页面无说明句与禁用词', async () => {
  await render()
  const t = host.textContent!
  for (const banned of ['今天不会', '不会再', '不用操作', '照着追', '能不能', '接下来', '建议', '吧', '呢', '。', '只记录不下单', '依据', '说明', '回测', '未证明', '大概率']) {
    expect(t, banned).not.toContain(banned)
  }
})
