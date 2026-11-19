// @vitest-environment jsdom
// 2026-11-15 B 方案 = 状态行 → 大盘方向 → 今日机会 / 我的持仓 (价格尺) · 右栏 账户 / 集中风险 / 模型打分 → 盘中异动。
// 不变量: 一个请求 (/page) 取齐整页 (盘中另有轻量报价); 价格尺现价跟报价刷新; 只有今日机会进入下单, 模型与盘中异动只作参考;
// 盘中异动顺序随大盘状态; 某块失败只空一块; 整页失败 = 错误不是“无机会”; 齿轮一个保存口; 系统文字只写结果 (无说明句、无口语)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { IntradayCard, LsRankPayload, LsRankRow, MarketMoodPayload, OppDesk, OppPagePayload, OppRadarView, RadarBlip, SwingBoardCard } from '@/lib/api'

const mocks = vi.hoisted(() => ({
  oppPage: vi.fn(), saveOppSettings: vi.fn(), watchlistRules: vi.fn(), newsSummary: vi.fn(), qverisOptions: vi.fn(), runSwingCandidates: vi.fn(),
  previewOppSettings: vi.fn(), oppAsofDays: vi.fn(), oppAsof: vi.fn(), oppQuotes: vi.fn(),
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

const lsRow = (symbol: string, rank: number, side: 'long' | 'short' | null, over: Partial<LsRankRow> = {}): LsRankRow => ({
  symbol, rank, pct: 100 - rank * 2, side, young: false, close: 100, atr: 5, spark: [95, 97, 100], pre_pct: -1.2, ref: 98.8,
  stop: side === 'long' ? 88.8 : side === 'short' ? 108.8 : null, qty: side ? 51 : null, earnings: null, tags: [], prev_rank: rank,
  is_new: false, book: side, ...over,
})
const ls = (over: Partial<LsRankPayload> = {}): LsRankPayload => {
  const longs = ['AAOI', 'IREN', 'LITE', 'CRWV', 'MRVL'].map((s, i) => lsRow(s, i + 1, 'long', i === 0 ? { earnings: { date: '2026-10-23', timing: 'amc' }, tags: ['趋势突破'] } : i === 1 ? { is_new: true } : {}))
  const shorts = ['MRNA', 'ORCL', 'PLTR', 'WDC', 'HIMS'].map((s, i) => lsRow(s, 47 - i, 'short'))
  const mid = [lsRow('NVDA', 20, null), lsRow('CBRS', 3, null, { young: true, book: null })]
  return {
    contract: 'ls-rank-v1', day: '2026-10-23', hold_until: '2026-10-29', as_of: '2026-10-22', made_at: '2026-10-22T16:52:00-04:00',
    model: 'GBM 2026Q4', trained_to: '2026-09-22', hold_days: 5, stop_atr: 2, risk_usd: 510, equity: 102000,
    pre: { at: '2026-10-23T09:00:00-04:00', live: false }, is_today: true,
    backtest: { period: '2024-01~2026-10', long: { mean_atr: 0.233, win: 0.532, rand: 0.186 }, short: { mean_atr: -0.154, win: 0.482, rand: -0.207 }, long_minus_all: 0.047 },
    long: longs, short: shorts, all: [...longs, ...mid, ...shorts], n: 12, missing: [], stale: [],
    settle: { days: 0, check_days: 65, first: null, last_list: null, total: null, weeks: [], last: null },
    ...over,
  }
}
const mood = (over: Partial<MarketMoodPayload> = {}): MarketMoodPayload => ({
  now: '2026-10-23T10:39-04:00', expected_day: '2026-10-23', next_at: '2026-10-26T08:35-04:00', is_today: true, is_latest: true,
  stale_items: [], missing_items: [], last_score: null, stats: { n: 17, hit: 10, hit_rate: 59 },
  judgment: { day: '2026-10-23', generated_at: '2026-10-23T08:35:00-04:00', mode: 'llm', probs: { up: 25, down: 45, range: 30 }, main: 'down', stance: 'down',
    stars: 3, panic: 'none', summary: 'x', items: [
      { key: 'trend', label: '趋势', text: 'x', status: 'ok', lean: 'down', note: 'QQQ 距 MA20 -4.9%' },
      { key: 'overnight', label: '隔夜', text: 'x', status: 'ok', lean: 'up' },
      { key: 'calendar', label: '日历', text: 'x', status: 'ok', lean: 'range' },
      { key: 'rates', label: '利率', text: 'x', status: 'stale', lean: 'down' },
      { key: 'sectors', label: '板块与宽度', text: 'x', status: 'ok', lean: 'down' },
      { key: 'panic', label: '恐慌', text: 'x', status: 'missing', lean: null }] },
  ...over,
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
  ls_rank: ls(),
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
const PREVIEW = {
  contract: 'opp-settings-preview-v1',
  changes: { long: { add: ['trend_breakout'], remove: [], add_names: ['trend_breakout'], remove_names: [] }, short: { add: [], remove: [], add_names: [], remove_names: [] } },
  odds_match: { long: false, short: true }, book: { day: '2026-10-19', frozen: true },
  risk: { from: 0.5, to: 0.75, caps_from: [10, 4], caps_to: [6, 2] }, radar_push: null, theme_stress: { from: 20, to: 20 }, hash: 'h1',
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.previewOppSettings.mockResolvedValue(PREVIEW)
  mocks.oppAsofDays.mockResolvedValue({ today: '2026-10-20', days: ['2026-10-16', '2026-10-19'] })
  mocks.oppPage.mockResolvedValue(page())
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:12-04:00', quotes: {} })
  mocks.watchlistRules.mockResolvedValue(rules)
  mocks.saveOppSettings.mockResolvedValue({ swing: { long: [], short: [] }, ruleset: { version: 46, risk_per_trade_pct: 0.75 } })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); document.body.innerHTML = '' })
const text = (sel: string) => host.querySelector(sel)?.textContent ?? ''

const flush = async (n = 6) => { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
const seqOf = (sels: Record<string, string>) => [...host.querySelectorAll(Object.values(sels).join(', '))]
  .map((e) => Object.entries(sels).find(([, sel]) => e.matches(sel))![0])

it('一个请求取齐整页; 盘中另取一次价格尺报价 (票去重排序); 不逐票请求期权 / 新闻; 规则目录只在开齿轮时取', async () => {
  await render()
  expect(mocks.oppPage).toHaveBeenCalledTimes(1)
  for (const k of ['newsSummary', 'qverisOptions', 'watchlistRules'] as const) expect(mocks[k], k).not.toHaveBeenCalled()
  expect(text('[data-swing-head]')).toContain('信号日 10-22')
  expect(mocks.oppQuotes).toHaveBeenCalledTimes(1)
  expect(mocks.oppQuotes).toHaveBeenCalledWith('ARM,BABA,META,NBIS,TSLA')
})

it('休市 / 盘后不取报价, 价格尺用页面价', async () => {
  mocks.oppPage.mockResolvedValue(page({ phase: { phase: 'closed', label: '休市', at: '2026-10-25T10:00' } }))
  await render()
  expect(mocks.oppQuotes).not.toHaveBeenCalled()
  expect(host.querySelector('[data-board-card="BABA"] [data-ruler-price]')).not.toBeNull()
})

it('B 版面: 状态行 → 大盘方向 → 今日机会 → 持仓 → 账户 → 模型打分 → 盘中异动; 泳道 / 名额条 / 主题压力表 / 旧大盘条已退役', async () => {
  await render()
  expect(seqOf({ rail: '[data-status-rail]', hero: '[data-market-hero]', board: '[data-board]', hold: '[data-holdings]', acct: '[data-account]',
    model: '[data-ls-rank]', radar: '[data-radar-section]' })).toEqual(['rail', 'hero', 'board', 'hold', 'acct', 'model', 'radar'])
  expect(host.querySelector('[data-opp-rail] [data-account]')).not.toBeNull()       // 账户名额在右栏 (次要位置)
  expect(host.querySelector('[data-opp-main] [data-account]')).toBeNull()
  for (const sel of ['[data-lane]', '[data-rail-lanes]', '[data-rail-caps]', '[data-cap]', '[data-theme-bar]', '[data-strategy-board]', '[data-mood-bar-old]']) {
    expect(host.querySelector(sel), sel).toBeNull()
  }
  expect(text('[data-rail-clock]')).toContain('10:39')
  expect(text('[data-rail-clock]')).toContain('北京 22:39')
  expect(text('[data-rail-phase]')).toContain('盘中 · 入场窗')
  expect(text('[data-rail-next]')).toContain('11:00 ET 盘中检查')
})

it('大盘方向: 盘前结论 + 三档概率 + 星级; 盘中实况并排 (普涨 · 标普 / 纳指 · 自选宽度); 六项依据只在悬停', async () => {
  mocks.oppPage.mockResolvedValue(page({ mood: mood() }))
  await render()
  expect(text('[data-mood-stance]')).toBe('偏空')
  expect(host.querySelector('[data-mood-stance]')!.getAttribute('title')).toBe('趋势 跌 · 隔夜 涨 · 日历 震荡 · 利率 旧 · 板块 跌 · 恐慌 缺')
  expect(text('[data-mood-probs]')).toBe('上涨 25%震荡 30%下跌 45%')
  expect(text('[data-mood-hit]')).toBe('近 17 日判对 10 次')
  expect(host.querySelector('[data-mood-old]')).toBeNull()
  expect(text('[data-market-hero] [data-regime]')).toBe('普涨')
  expect(text('[data-regime-index]')).toContain('标普 +0.54%')
  expect(text('[data-regime-index]')).toContain('纳指 +1.25%')
  expect(text('[data-regime-breadth]')).toBe('自选涨 ≥1% 34/44')
  expect(text('[data-market-hero]')).not.toContain('趋势 跌')
})

it('大盘方向: 非当日标出来; 没有判断写下次生成时刻, 盘中实况照常', async () => {
  mocks.oppPage.mockResolvedValue(page({ mood: mood({ is_today: false }) }))
  await render()
  expect(text('[data-mood-old]')).toBe('非当日')
  act(() => root.unmount()); host.innerHTML = ''; root = createRoot(host)
  mocks.oppPage.mockResolvedValue(page({ mood: mood({ judgment: null }) }))
  await render()
  expect(host.querySelector('[data-market-hero]')!.getAttribute('data-mood-state')).toBe('none')
  expect(text('[data-market-hero]')).toContain('盘前判断 10-26 08:35 ET 生成')
  expect(text('[data-market-hero] [data-regime]')).toBe('普涨')
})

it('大盘方向: 整点更新后大字换成最新一次并标时刻, 原判缩成一行; 悬停含现价与各时点判对; 过点未更新标出来', async () => {
  mocks.oppPage.mockResolvedValue(page({ mood: mood() }))
  await render()
  expect(host.querySelector('[data-mood-stance]')!.getAttribute('data-mood-src')).toBe('origin')     // 没有快照: 与原来一致
  expect(host.querySelector('[data-mood-origin]')).toBeNull()
  expect(host.querySelector('[data-mood-latest]')).toBeNull()
  act(() => root.unmount()); host.innerHTML = ''; root = createRoot(host)

  const j = mood().judgment!
  const latest = { ...j, slot: '10:00', generated_at: '2026-10-23T10:00:04-04:00', probs: { up: 50, down: 20, range: 30 },
    main: 'up' as const, stance: 'up' as const, stars: 4,
    items: [...j.items, { key: 'live' as const, label: 'QQQ 现价', text: 'x', status: 'ok' as const, lean: 'up' as const }] }
  const updates = [{ slot: '09:25', generated_at: '2026-10-23T09:25:03-04:00', probs: { up: 30, down: 40, range: 30 }, main: 'down' as const, stance: 'down' as const, stars: 3 },
    { slot: '10:00', generated_at: latest.generated_at, probs: latest.probs, main: 'up' as const, stance: 'up' as const, stars: 4 }]
  const stats = { n: 17, hit: 10, hit_rate: 59, brier: 0.555, brier_base: 0.627,
    slots: [{ slot: '09:25', n: 5, hit: 3, brier: 0.48, brier_base: 0.61 }, { slot: '10:00', n: 3, hit: 2, brier: 0.41, brier_base: 0.6 }] }
  mocks.oppPage.mockResolvedValue(page({ mood: mood({ latest, updates, stats, next_update_at: '2026-10-23T11:00-04:00', update_missing: null }) }))
  await render()
  const st = host.querySelector('[data-mood-stance]')!
  expect(st.textContent).toBe('偏多')
  expect(st.getAttribute('data-mood-src')).toBe('latest')
  expect(st.getAttribute('title')).toBe('趋势 跌 · 隔夜 涨 · 日历 震荡 · 利率 旧 · 板块 跌 · 恐慌 缺 · 现价 涨')
  expect(text('[data-mood-probs]')).toBe('上涨 50%震荡 30%下跌 20%')
  expect(text('[data-mood-stars]')).toContain('4/5')
  expect(text('[data-mood-latest]')).toBe('最新 10-23 10:00 ET')
  expect(host.querySelector('[data-mood-latest]')!.getAttribute('title')).toBe('08:35 原判 偏空\n09:25 偏空\n10:00 偏多\n下次 11:00 ET')
  expect(text('[data-mood-origin]')).toBe('08:35 原判 偏空 · 涨 25% · 震荡 30% · 跌 45%')
  expect(host.querySelector('[data-mood-missed]')).toBeNull()
  expect(text('[data-market-hero]')).not.toContain('盘前判断 10-23')
  const hit = host.querySelector('[data-mood-hit]')!
  expect(hit.textContent).toBe('近 17 日判对 10 次')
  expect(hit.getAttribute('title')).toBe(['打分越低越准', '08:35 原判 判对 10/17 · 打分 0.555 · 固定分布 0.627',
    '09:25 判对 3/5 · 打分 0.480 · 固定分布 0.610', '10:00 判对 2/3 · 打分 0.410 · 固定分布 0.600'].join('\n'))
  expect(text('[data-market-hero] [data-regime]')).toBe('普涨')                                    // 盘中实况照常并排
  act(() => root.unmount()); host.innerHTML = ''; root = createRoot(host)

  mocks.oppPage.mockResolvedValue(page({ mood: mood({ latest, updates, update_missing: '11:00' }) }))
  await render()
  expect(text('[data-mood-missed]')).toBe('11:00 未更新')
  expect(text('[data-mood-stance]')).toBe('偏多')
})

const BOARD = () => {
  const sw = page().swing!
  return { ...sw, long: [card('BABA'), card('TSLA', { entry_zone: [384.51, 388.07], stop: 373.85, target: 412.3, price: 406.87, dist_pct: -4.62,
    odds: { fill: 0.084, ev_r: 0.02, ci_r: [-0.02, 0.07], sig: 0, n: 452, level: 'x', day: 1, phase: 'pm', bin: 'gt1', dist_atr: 1.3, qty: 41, risk_usd: 510, ev_usd: 12, ci_usd: [-12, 37] } }),
  card('PLTR', { verdict: 'skip', verdict_reason: 'cooldown', blocked: 'cooldown' }), card('PLTR', { verdict: 'skip', verdict_reason: 'cooldown', blocked: 'cooldown', card_id: 'p2' }),
  card('AMD', { verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch', paper_reason: '准入阻断：同板块同向已满；组合风险超限' })],
  short: [card('META', { side: 'short', verdict: 'skip', verdict_reason: 'premarket_beyond_stop' }), card('ORCL', { side: 'short', entry_zone: [101, 102], stop: 107, target: 90, price: 100 })] }
}

it('今日机会: 可进场的卡一张一把尺, 计数 = 卡数; 不做的卡折成一行写白话原因 (同票同原因只写一次)', async () => {
  mocks.oppPage.mockResolvedValue(page({ swing: BOARD() }))
  await render()
  expect([...host.querySelectorAll('[data-board-card]')].map((e) => e.getAttribute('data-board-card'))).toEqual(['BABA', 'TSLA', 'ORCL'])
  expect(text('[data-board] h2 + span')).toBe('3')
  const tsla = text('[data-board-card="TSLA"]')
  expect(tsla).toContain('↑ 做多')
  expect(tsla).toContain('等进场还差 4.6%')
  expect(tsla).toContain('止损亏 −$510')
  expect(tsla).toContain('目标赚 +$1,066')                 // 41 × (412.30 − 386.29)
  expect(tsla).toContain('进场 384.51 ~ 388.07')
  expect(tsla).toContain('41 股')
  expect(tsla).toContain('止损 373.85')
  expect(tsla).toContain('目标 412.30')
  expect(text('[data-board-card="ORCL"]')).toContain('↓ 做空')
  expect(host.querySelector('[data-board-card="ORCL"]')!.getAttribute('data-zone')).toBe('wait')
  expect([...host.querySelectorAll('[data-skip]')].map((e) => e.textContent)).toEqual(['PLTR↑ 做多刚卖出 暂不买回', 'AMD↑ 做多同板块同向已满', 'META↓ 做空盘前已破止损'])
  expect(host.querySelector('[data-skip="AMD"]')!.getAttribute('title')).toContain('组合风险超限')
  expect(text('[data-board-skips]')).toContain('不做 3')
  // 白话: 不再出现内部口径
  for (const x of ['4h', '成交率', '回放', '下单 ', '名义']) expect(text('[data-board]'), x).not.toContain(x)
  expect(text('[data-board]')).not.toMatch(/[+−]?\d+\.\d+R(?![a-z])/)
})

it('价格尺: 左亏右赚 (做空也一样); 报价推送后现价标记移动, 进入进场区改成「已到进场价」', async () => {
  mocks.oppPage.mockResolvedValue(page({ swing: BOARD() }))
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:12-04:00', quotes: { BABA: { price: 99.2, src: 'live', chg: 1.1 } } })
  await render()
  const pos = (s: string) => Number(host.querySelector(`[data-board-card="${s}"] [data-ruler-price]`)!.getAttribute('data-ruler-price'))
  expect(text('[data-board-card="BABA"] [data-ruler-loss]')).toMatch(/^止损亏 /)
  expect(text('[data-board-card="ORCL"] [data-ruler-stop]')).toBe('止损 107.00')            // 做空: 止损仍在左端
  expect(text('[data-board-card="ORCL"] [data-ruler-target]')).toBe('目标 90.00')
  expect(text('[data-board-card="BABA"] [data-card-price]')).toContain('99.20')
  expect(text('[data-board-card="BABA"] [data-card-price]')).toContain('+1.10%')
  expect(text('[data-board-card="BABA"] [data-card-state]')).toContain('等进场')
  expect(text('[data-board] [data-section-meta]')).toBe('现价 10:39:12 ET · 实时')
  const before = pos('BABA')
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:17-04:00', quotes: { BABA: { price: 97.5, src: 'live', chg: -0.6 } } })
  await act(async () => { await qc.invalidateQueries({ queryKey: ['opp-quotes'] }) })
  await flush()
  expect(pos('BABA')).toBeLessThan(before)                     // 做多: 价格跌 → 标记往左 (往止损方向)
  expect(text('[data-board-card="BABA"] [data-card-state]')).toContain('已到进场价')
  expect(host.querySelector('[data-board-card="BABA"]')!.getAttribute('data-zone')).toBe('in')
  expect(text('[data-board-card="BABA"] [data-card-price]')).toContain('97.50')
})

it('价格尺越界: 现价越过止损贴在左端并标出', async () => {
  const v = view({ blips: [hold('ARM', { hold: { ...hold('ARM').hold!, price: 231 } })] })
  mocks.oppPage.mockResolvedValue(page({ radar_view: v }))
  await render()
  expect(host.querySelector('[data-hold="ARM"] [data-ruler]')!.getAttribute('data-ruler-off')).toBe('stop')
  expect(host.querySelector('[data-hold="ARM"] [data-ruler-price]')!.getAttribute('data-ruler-price')).toBe('0.0')
  expect(text('[data-hold="ARM"] [data-hold-tags]')).toContain('已涨破止损')
})

it('持仓: 台账为准 (股数 / 成本 / 当前止损 + 原止损 / 第几天); 浮动按报价重算; 要处理的排前', async () => {
  const v = view({ blips: [hold('ARM'),
    hold('IREN', { side: 'long', kind: 'exit', ring: 0, hold: { ...hold('IREN').hold!, qty: 198, avg: 27.69, price: 28.5, stop_now: 27.69, stop_initial: 26.1, stop_moved: true, target: 31,
      day_n: 5, exit_day: '2026-10-23', exit_at: '15:30', exit_code: 'time_exit', stop_dist_atr: 1.4 } })] })
  mocks.oppPage.mockResolvedValue(page({ radar_view: v }))
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:12-04:00', quotes: { ARM: { price: 200, src: 'live', chg: -2 } } })
  await render()
  expect([...host.querySelectorAll('[data-hold]')].map((e) => e.getAttribute('data-hold'))).toEqual(['IREN', 'ARM'])
  const iren = text('[data-hold="IREN"]')
  expect(text('[data-hold="IREN"] [data-hold-tags]')).toContain('今天 15:30 到期平仓')
  expect(text('[data-hold="IREN"] [data-hold-tags]')).toContain('保本')
  expect(iren).toContain('止损 27.69 · 原 26.10')
  expect(iren).toContain('止损亏 $0')
  expect(iren).toContain('198 股')
  expect(iren).toContain('第 5 天 / 最多 5')
  expect(text('[data-hold="ARM"] [data-hold-pnl]')).toBe('+$483')            // 做空 35 股: (213.80 − 200) × 35, 报价价不是页面价 210.33
  expect(text('[data-hold="ARM"]')).toContain('↓ 做空')
  expect(text('[data-hold="ARM"]')).toContain('止损亏 −$506')               // (213.80 − 228.27) × 35
  expect(text('[data-hold-total]')).toBe('+$643')                             // 483 + 198 × (28.50 − 27.69)
})

it('近止损按现价换算 ATR 距离; 止损已在成本或更好 (保本 / 锁盈) 不提示近止损', async () => {
  const base = hold('ARM').hold!
  const v = view({ blips: [
    hold('NEAR', { side: 'long', hold: { ...base, avg: 100, price: 104, stop_now: 98, stop_initial: 98, target: 110, stop_dist_atr: 1.2 } }),
    hold('EVEN', { side: 'long', hold: { ...base, avg: 100, price: 100.4, stop_now: 100, stop_initial: 95, stop_moved: true, target: 110, stop_dist_atr: 0.1 } })] })
  mocks.oppPage.mockResolvedValue(page({ radar_view: v }))
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:12-04:00', quotes: { NEAR: { price: 99.5, src: 'live', chg: -4 } } })
  await render()
  expect(text('[data-hold="NEAR"] [data-hold-tags]')).toContain('近止损')        // 1.2 ATR × (99.5 − 98) / (104 − 98) = 0.3 ATR
  expect(text('[data-hold="EVEN"] [data-hold-tags]')).not.toContain('近止损')
  expect(text('[data-hold="EVEN"] [data-hold-tags]')).toContain('保本')
})

it('账户名额在右栏: 今日还能开 / 持仓 / 止损全触发 (美元) / 最紧的板块名额', async () => {
  await render()
  expect(text('[data-acct="opens"]')).toContain('今日还能开9 笔')
  expect(text('[data-acct="positions"]')).toContain('1 / 12 只')
  expect(text('[data-acct="risk"]')).toContain('止损全触发−$506')             // 台账: 35 × (228.27 − 213.80), 与持仓卡同一份数
  expect(text('[data-acct="risk"]')).toContain('上限 −$6,120')
  expect(text('[data-acct="slot"]')).toContain('科技股做空还剩 2 只')
  expect(text('[data-acct="slot"]')).toContain('已有 ARM COHR')
})

it('模型打分 (右栏, 仅参考): 看涨 5 / 看跌 5 只列代码与排名; 全部名单与结算在抽屉; 与策略方向相反时卡上标「模型反向」', async () => {
  const sw = BOARD()
  mocks.oppPage.mockResolvedValue(page({ swing: { ...sw, long: [...sw.long, card('MRNA')] } }))
  await render()
  const m = host.querySelector('[data-ls-rank]')!
  expect(m.getAttribute('data-source')).toBe('model')
  expect(host.querySelectorAll('[data-ls-side="long"] [data-ls-row]').length).toBe(5)
  expect(host.querySelectorAll('[data-ls-side="short"] [data-ls-row]').length).toBe(5)
  expect(text('[data-ls-row="AAOI"]')).toBe('AAOI财前 2%')
  expect(host.querySelector('[data-ls-row="IREN"] [data-ls-new]')).not.toBeNull()
  expect(m.querySelector('[title="模型独立打分 · 不据此生成策略机会 · 不下单"]')).not.toBeNull()
  for (const x of ['股数', '止损', 'ATR', '每笔 $510', '88.80']) expect(m.textContent, x).not.toContain(x)
  expect(text('[data-ls-open="all"]')).toBe('全部 12')
  expect(text('[data-board-card="MRNA"] [data-model-conflict]')).toBe('模型反向')     // 模型看跌 MRNA, 策略做多
  expect(host.querySelector('[data-board-card="BABA"] [data-model-conflict]')).toBeNull()
  act(() => { (host.querySelector('[data-ls-open="settle"]') as HTMLButtonElement).click() })
  expect(document.querySelector('[data-ls-settle]')?.textContent).toContain('GBM 2026Q4')
})

it('模型打分: 没有名单 = 一行空态; 段读取失败 = 只空这一块', async () => {
  mocks.oppPage.mockResolvedValue(page({ ls_rank: null }))
  await render()
  expect(host.querySelector('[data-ls-rank-empty]')).not.toBeNull()
  expect(host.querySelector('[data-ls-rank]')).toBeNull()
  act(() => root.unmount()); host.innerHTML = ''; root = createRoot(host)
  mocks.oppPage.mockResolvedValue(page({ ls_rank: null, errors: { ls_rank: 'ValueError' } }))
  await render()
  expect(text('[data-section-error="模型打分"]')).toBe('模型打分读取失败 · ValueError')
  expect(host.querySelector('[data-col]')).not.toBeNull()
})

it.each([
  ['up', ['intraday', 'open', 'gap', 'resonance']],
  ['normal', ['intraday', 'open', 'gap', 'resonance']],
  ['down', ['open', 'gap', 'intraday', 'resonance']],
  [null, ['intraday', 'open', 'gap', 'resonance']],
] as const)('盘中异动四组顺序随大盘状态, 共振固定最后: %s', async (key, order) => {
  mocks.oppPage.mockResolvedValue(page({ desk: desk({ key }) }))
  await render()
  expect([...host.querySelectorAll('[data-col]')].map((e) => e.getAttribute('data-col'))).toEqual(order)
})

it('盘中异动: 四组完整列出, 读数写全; 右列现在涨跌跟报价; 共振票在各组带圆点; 不用固定窄标签列', async () => {
  mocks.oppQuotes.mockResolvedValue({ contract: 'opp-quotes-v1', at: '2026-10-23T10:39:12-04:00', quotes: { NBIS: { price: 160, src: 'live', chg: 12.04 } } })
  mocks.oppPage.mockResolvedValue(page({ radar: { ...page().radar!, push: { enabled: true, min_rvol: 3, today: null } } }))
  await render()
  expect(text('[data-radar-section]')).toContain('仅参考 · 不下单')
  expect(host.querySelector('[data-radar-section] [title="只记录 · 不下单 · 不计入模型评分"]')).not.toBeNull()
  expect(text('[data-gap-row="NBIS"]')).toBe('NBIS高开 +4.3%+12.04%')
  expect(text('[data-intraday-row="TSLA"] [data-res-mark]')).toBe('共振')
  expect(text('[data-open-row="TSLA"] [data-row-main]')).toBe('第 3 · 平时的 1.2 倍 · 开盘 5 分钟 +2.3%')
  expect(text('[data-open-row="TSLA"] [data-row-sub]')).toBe('开盘区间 395.25 ~ 401.00')
  expect(text('[data-intraday-row="TSLA"]')).toContain('10-23 10:00 走强 · 第 1/5 天')
  expect(text('[data-intraday-row="TSLA"]')).toContain('+0.95%')               // 101 / 100.05 − 1
  expect(text('[data-intraday-closed="AVGO"]')).toContain('10:15 止损')
  expect(text('[data-res="TSLA"]')).toContain('3 个信号同向 · ↑ 做多')
  expect(text('[data-res="TSLA"]')).toContain('开盘量第 3')
  expect(text('[data-res="TSLA"]')).toContain('机会 等进场')
  expect(text('[data-res="TSLA"] [data-row-sub]')).toContain('观察 10:05')
  expect(host.querySelector('[data-intraday-row="TSLA"] [data-res-mark]')).not.toBeNull()
  expect(text('[data-col="open"]')).toContain('推送 ≥3 倍')
  // 一行 = 代码 | 读数 | 现在涨跌, 读数格可换行不截断
  for (const r of host.querySelectorAll('[data-col-body] > [role="button"]')) {
    expect(r.className).toContain('grid-cols-[60px_minmax(0,1fr)_auto]')
    expect(r.querySelector('[data-row-main]')!.className).not.toContain('truncate')
  }
  // 四组卡片统一限高, 超出在卡内滚动 (头部不动); 同一网格等高, 不用 items-start 让卡各自长高
  const grid = host.querySelector('[data-cols]')!
  expect(grid.className).toContain('md:auto-rows-fr')
  expect(grid.className).not.toContain('items-start')
  for (const c of host.querySelectorAll('[data-col]')) {
    expect(c.className).toMatch(/max-h-\[\d+px\]/)
    expect(c.className).toContain('overflow-hidden')
    expect(c.querySelector('[data-col-body]')!.className).toContain('overflow-y-auto')
    expect(c.querySelector('[data-col-body]')!.className).toContain('min-h-0')
  }
})

it('盘中异动: 跳空多时全部列出 (卡内滚动); 没跑到点写下次时刻', async () => {
  const rows = Array.from({ length: 18 }, (_, i) => ({ symbol: `G${i}`, gap_pct: -3 - i * 0.4, price: 100 }))
  mocks.oppPage.mockResolvedValue(page({ gaps: { at: '2026-10-29T04:28:00-04:00', live: true, threshold_pct: 3, checked: 41, rows },
    radar: { ...page().radar!, is_today: false, picks: [], others: [] } }))
  await render()
  expect(host.querySelectorAll('[data-gap-row]').length).toBe(18)
  expect(text('[data-gap-row="G17"]')).toContain('低开 −9.8%')
  expect(text('[data-col="gap"]')).toContain('04:28 ET 实时 · 门槛 3%')
  expect(text('[data-col="open"] [data-col-empty]')).toBe('09:51 ET 发布')
})

it('某块失败只空那一块', async () => {
  mocks.oppPage.mockResolvedValue(page({ desk: null, radar: null, errors: { desk: 'KeyError', radar: 'KeyError' } }))
  await render()
  expect(text('[data-market-hero] [data-regime]')).toBe('读取失败')
  expect(text('[data-col="open"] [data-col-error]')).toBe('开盘量读取失败')
  expect(host.querySelector('[data-intraday-row="TSLA"]')).not.toBeNull()
  expect(host.querySelector('[data-board-card="BABA"]')).not.toBeNull()
  expect([...host.querySelectorAll('[data-col]')].map((e) => e.getAttribute('data-col'))).toEqual(['board', 'intraday', 'open', 'gap', 'resonance'].filter((k) => k !== 'board'))
  expect(text('[data-col="resonance"] [data-col-error]')).toBe('多信号共振读取失败')
})

it('整页请求失败 = 错误, 不是“无机会”', async () => {
  mocks.oppPage.mockRejectedValue(new Error('HTTP 503'))
  await render()
  expect(text('[data-error]')).toContain('HTTP 503')
  expect(host.querySelector('[data-col]')).toBeNull()
  expect(host.querySelector('[data-board]')).toBeNull()
})

it('点卡打开预览; 左右切票顺序 = 今日机会 → 持仓 → 模型打分 → 盘中异动', async () => {
  await render()
  await act(async () => { (host.querySelector('[data-res="TSLA"]') as HTMLElement).click() })
  await flush(4)
  const pv = host.querySelector('[data-preview]')!
  expect(pv.getAttribute('data-preview')).toBe('TSLA')
  expect(pv.getAttribute('data-nav')).toBe('BABA,TSLA,META,ARM,AAOI,IREN,LITE,CRWV,MRVL,MRNA,ORCL,PLTR,WDC,HIMS,NBIS')
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
  // 2026-11-15 两步保存: 第一下只出预览 (不写), 确认后才带哈希保存
  await act(async () => { save.click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(mocks.previewOppSettings).toHaveBeenCalledWith(['macd_golden', 'trend_breakout'], ['tv_halftrend'], 0.75, null, null)
  expect(mocks.saveOppSettings).not.toHaveBeenCalled()
  const pv = c.querySelector('[data-settings-preview]') as HTMLElement
  expect(pv.textContent).toContain('+趋势突破')
  expect(pv.textContent).not.toContain('trend_breakout')
  expect(pv.textContent).toContain('0.5% → 0.75%')
  expect(pv.textContent).toContain('单日开仓 10 → 6 笔')
  await act(async () => { (c.querySelector('[data-save-confirm]') as HTMLButtonElement).click() })
  expect(mocks.saveOppSettings).toHaveBeenCalledWith(['macd_golden', 'trend_breakout'], ['tv_halftrend'], 0.75, null, { preview_hash: 'h1', theme_stress_pct: null })
})

it('齿轮: 确认时另一处已改过 (409) → 换成新预览, 不写', async () => {
  const { ApiError } = await import('@/lib/api')
  mocks.saveOppSettings.mockRejectedValueOnce(new ApiError(JSON.stringify({ reason: 'settings_changed', preview: { ...PREVIEW, hash: 'h2' } }), 409))
  await render()
  await act(async () => { (host.querySelector('[data-gear]') as HTMLElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const c = document.body.querySelector('[data-console]') as HTMLElement
  await act(async () => { (c.querySelector('[data-swing-list="long"] [data-swing-pick="trend_breakout"] input') as HTMLInputElement).click() })
  await act(async () => { (c.querySelector('[data-save]') as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  await act(async () => { (c.querySelector('[data-save-confirm]') as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(c.querySelector('[data-preview-conflict]')).not.toBeNull()
  await act(async () => { (c.querySelector('[data-save-confirm]') as HTMLButtonElement).click() })
  expect(mocks.saveOppSettings).toHaveBeenLastCalledWith(['macd_golden', 'trend_breakout'], ['tv_halftrend'], null, null, { preview_hash: 'h2', theme_stress_pct: null })
})

it('历史日回看: 选过去的日子 → 只读那天冻结的数据, 当天各块收起', async () => {
  mocks.oppAsof.mockResolvedValue({ contract: 'opp-asof-v1', asof: '2026-10-19', today: '2026-10-20', days: ['2026-10-19'], board: null, ls_rank: null,
    intraday: null, radar: null, resonance: null, mood: null, errors: { board: 'not_archived' } })
  await render()
  const sel = host.querySelector('[data-asof-picker]') as HTMLSelectElement
  expect([...sel.options].map((o) => o.value)).toEqual(['', '2026-10-19', '2026-10-16'])
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!
  await act(async () => { setter.call(sel, '2026-10-19'); sel.dispatchEvent(new Event('change', { bubbles: true })) })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(mocks.oppAsof).toHaveBeenCalledWith('2026-10-19')
  expect(host.querySelector('[data-asof="2026-10-19"]')).not.toBeNull()
  expect(host.querySelector('[data-strategy-board]')).toBeNull()
  expect(text('[data-asof-head]')).toBe('回看 2026-10-19')
})


it('模型打分: 开盘后才补出的名单标「盘中补出」', async () => {
  mocks.oppPage.mockResolvedValue(page({ ls_rank: ls({ late: true }) }))
  await render()
  expect(text('[data-ls-late]')).toBe('盘中补出')
})

it('模型打分: 结算明细里停牌缺 K 线的票标「缺」', async () => {
  const last = { made: '2026-10-16', settled: '2026-10-22', long: [{ symbol: 'AAOI', r: 0.4, exit: 'time' as const }], short: [],
    sum_long: 0.4, sum_short: 0, missing: { long: ['LITE'], short: [] } }
  mocks.oppPage.mockResolvedValue(page({ ls_rank: ls({ settle: { ...ls().settle, last } }) }))
  await render()
  act(() => { (host.querySelector('[data-ls-open="settle"]') as HTMLButtonElement).click() })
  expect(document.querySelector('[data-ls-missing="long"]')?.textContent).toBe('缺 LITE')
  expect(document.querySelector('[data-ls-missing="short"]')).toBeNull()
})

it('盘中走强「今日已结束」按出场日: 前几天出的卡今天止损也列出, 今天出卡但前一天已出场的不列', async () => {
  const it0 = page().intraday!
  const history = [icard('GOOGL', { day: '2026-10-21', status: 'closed', exit_reason: 'stop', exit_at: '2026-10-23T10:05', exit: 98, r: -1, r_now: null }),
    icard('LLY', { status: 'closed', exit_reason: 'target', exit_at: '2026-10-23T11:00', exit: 104.1, r: 2, r_now: null }),
    icard('OLD', { day: '2026-10-20', status: 'closed', exit_reason: 'stop', exit_at: '2026-10-22T10:05', r: -1, r_now: null })]
  mocks.oppPage.mockResolvedValue(page({ intraday: { ...it0, history } }))
  await render()
  expect(host.querySelector('[data-intraday-closed="GOOGL"]')).not.toBeNull()
  expect(text('[data-intraday-closed="LLY"]')).toContain('11:00 到目标')
  expect(text('[data-intraday-closed="LLY"]')).toContain('+4.05%')
  expect(host.querySelector('[data-intraday-closed="OLD"]')).toBeNull()
})

it('模型打分: 风险读不到时结算明细不显示美元换算, 只显示 ATR', async () => {
  const d = ls({ risk_usd: null, long: ls().long.map((r) => ({ ...r, qty: null })), short: ls().short.map((r) => ({ ...r, qty: null })) })
  mocks.oppPage.mockResolvedValue(page({ ls_rank: d }))
  await render()
  act(() => { (host.querySelector('[data-ls-open="settle"]') as HTMLButtonElement).click() })
  const rules = document.querySelector('[data-ls-rules]')?.textContent ?? ''
  expect(rules).toContain('止损 2 ATR')
  expect(rules).not.toContain('1 ATR = $')
})

it('系统文字只写结果: 页面无说明句与禁用词; 内部口径 (R / ATR / 名义 / 主题压力 / 4h / 回放) 不在主页面', async () => {
  mocks.oppPage.mockResolvedValue(page({ mood: mood() }))
  await render()
  const t = host.textContent!
  for (const banned of ['今天不会', '不会再', '不用操作', '照着追', '能不能', '接下来', '建议', '吧', '呢', '。', '只记录不下单', '依据', '说明', '回测', '未证明', '大概率',
    'ATR', '名义', '主题压力', '4h', '回放', '随机基准', '入场区', '距入场']) {
    expect(t, banned).not.toContain(banned)
  }
  expect(t).not.toMatch(/[+−]\d+\.\d+R(?![a-z])/)
})
