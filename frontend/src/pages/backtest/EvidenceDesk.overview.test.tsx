// @vitest-environment jsdom
// 机会页“能不能出手、卡在哪”重排 (2026-10-05): 状态条 / 跨日可执行卡 / 自选总表 / 观察折叠 / 失败不冒充零机会
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ opportunitiesToday: vi.fn(), deskSources: vi.fn(), setDeskSourceStatus: vi.fn(), newsSummary: vi.fn(), deskOverview: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/PageHeader', () => ({ PageHeader: () => null }))

import { EvidenceDesk as Opportunities } from './EvidenceDesk'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const card = (symbol: string, source: string, tier: string, state = 'watch', extra: Record<string, unknown> = {}) => ({
  id: `c-${symbol}`, symbol, setup_type: `strategy:${source}`, created_date: '2026-10-02', entry_zone: [100, 101],
  invalidation: 95, target_zone: [112, 114], max_holding_days: 7, risk_per_share: 5.5, reward_risk: 2.1, narrative: '',
  state, state_reason: tier === 'negative' ? '10238 笔平均 -0.025R，置信下界 -0.056R ≤ 0：只展示不开仓' : '',
  evidence: { decision: { tab: 'pool', sources: [source], tier, score: 0, resonance: 1, primary_source: source } }, ...extra,
})

const exec = {
  ...card('TIGO', 'long_lower_shadow_reversal', 'proven', 'active'), id: 'c-TIGO-1001', created_date: '2026-10-01', reward_risk: 4.9,
  window: { elapsed: 1, remaining: 4, last_day: '2026-10-08', open: true },
  permission: { status: 'enabled', tier: 'proven', weight: 0.0654, risk_pct: 0.01, tradable: true },
  blockers: [], capacity: { planned_qty: 231, need_cash: 19876, risk_pct: 1, downsized_qty: 187, verdict: 'downsized' },
  in_watchlist: false, held: false, rr_suspect: false,
  explain: '长下影反击选中；证据为正；现金不足，入区时将缩量成交（约 187 股，计划 231 股）；入场窗剩 4 个交易日',
}
const blocked = { ...exec, id: 'c-RIOT', symbol: 'RIOT', state: 'watch', capacity: null, in_watchlist: true,
  blockers: ['行业 Finance 已有 2 张进行中（上限 2）'], explain: '长下影反击选中；证据为正；卡在：行业 Finance 已有 2 张进行中（上限 2）' }

const overview = {
  status: {
    today: '2026-10-05', in_session: false, latest_scan: '2026-10-02', scan_generated_at: null, gate: 'green',
    tradable_sources: [{ id: 'long_lower_shadow_reversal', name: '长下影反击', weight: 0.0654, tier: 'proven' }],
    evaluated_sources: 23, executable: 2, executable_clear: 1, circuit_breaker: null, last_quote_at: null, last_run_at: null,
    entry_blockers: [], crowding: { 'Finance: Consumer Services': ['CIFR', 'CLSK', 'CORZ', 'RIOT'] },
    account: { nav: 99676.65, cash: 16133.65, buying_power: 16133.65, positions: 5, max_positions: 6, open_risk: 3705.78,
               open_risk_pct: 3.72, max_portfolio_risk_pct: 6, risk_per_trade_pct: 1, marks_origin: ['close'], unmarked: [] },
  },
  executable: [exec, blocked],
  live_other_count: 229,
  watchlist: {
    ok: true, count: 3, scan_day: '2026-10-02', removed_since_scan: ['OLD'],
    rows: [
      { symbol: 'NVDA', held: false, scan_day: '2026-10-02', added_after_scan: false, card: null, stage: 'no_signal', reason: '当日无来源命中', hits: [] },
      { symbol: 'RIOT', held: false, scan_day: '2026-10-02', added_after_scan: false,
        card: { id: 'c-RIOT', state: 'watch', state_label: '观察', created_date: '2026-10-01', setup_type: 'strategy:long_lower_shadow_reversal', entry_zone: [19.61, 19.81], state_reason: '行业 Finance 已达上限', tradable: true },
        stage: 'card', reason: 'x', hits: [{ source: 'long_lower_shadow_reversal', score: 2.1, rank: 37, total: 80, via: 'watchlist' }] },
      { symbol: 'NEW', held: false, scan_day: '2026-10-02', added_after_scan: true, card: null, stage: 'pending', reason: '最近一次扫描之后才加入自选，下次盘后扫描纳入', hits: [] },
    ],
  },
  names: { long_lower_shadow_reversal: '长下影反击', bullish_alignment: '均线多头' },
}

async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><Opportunities /></QueryClientProvider>) })
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.opportunitiesToday.mockResolvedValue({
    as_of: '2026-10-02', gate: 'green', scanned: 2053, cards: [],
    watch: [card('DHR', 'bullish_alignment', 'negative'), card('ALLE', 'bullish_alignment', 'negative'),
            card('CTVA', 'bullish_alignment', 'negative', 'watch', { reward_risk: 131.7 })],
  })
  mocks.deskOverview.mockResolvedValue(overview)
  mocks.deskSources.mockResolvedValue({ sources: [], evaluation: {}, rules: {}, audit: [] })
  mocks.newsSummary.mockImplementation(async (syms: string[]) => ({ disclaimer: 'x', summaries: Object.fromEntries(syms.map((s) => [s, { count_24h: 0, high_bearish_48h: false, high_bullish_48h: false, top: [] }])) }))
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear(); vi.clearAllMocks() })

it('status bar answers 能不能出手 with tradable sources, account cash and unevaluated breaker', async () => {
  await render()
  const bar = host.querySelector('[data-status-bar]')!.textContent ?? ''
  expect(bar).toContain('可交易来源 1/23（长下影反击）')
  expect(bar).toContain('在窗可执行卡 2（无阻断 1）')
  expect(bar).toContain('熔断 未评估')
  expect(bar).toContain('现金 $16,134')
  expect(bar).toContain('持仓 5/6')
  expect(bar).toContain('组合风险 3.72% / 6%')
  expect(host.querySelector('[data-crowding]')!.textContent).toContain('Finance: Consumer Services 4 只')
})

it('cross-day executable cards come first with plain-language explain, window and cash downsizing', async () => {
  await render()
  const tabs = [...host.querySelectorAll('[role="tab"]')].map((t) => t.textContent)
  expect(tabs).toEqual(['全股票池 (2)', '自选股 (3)', '来源管理', '卡结局'])
  expect(host.querySelector('[data-exec-section]')!.textContent).toContain('① 可执行')
  const t = host.querySelector('[data-card="TIGO"]')!
  expect(t.querySelector('[data-exec]')!.textContent).toContain('入场窗至 2026-10-08（剩 4 个交易日）')
  expect(t.textContent).toContain('现金不足 · 缩量成交（约 187 股）')
  expect(t.textContent).toContain('长下影反击选中；证据为正')
  expect(host.querySelector('[data-card="RIOT"] [data-blockers]')!.textContent).toContain('卡在：行业 Finance 已有 2 张进行中')
  // 可执行卡排在观察组之前
  const order = [...host.querySelectorAll('[data-card], [data-watch-groups]')].map((e) => e.getAttribute('data-card') ?? 'GROUPS')
  expect(order.indexOf('TIGO')).toBeLessThan(order.indexOf('GROUPS'))
})

it('observe-only cards are folded by source and RR outliers are flagged', async () => {
  await render()
  const g = host.querySelector('[data-watch-group="bullish_alignment"]') as HTMLDetailsElement
  expect(g.open).toBe(false)
  expect(g.querySelector('summary')!.textContent).toContain('均线多头 ×3 · 证据为负')
  expect(host.querySelector('[data-card="CTVA"] [data-rr-suspect]')!.textContent).toBe('价位异常待核')
  expect(host.querySelector('[data-card="DHR"] [data-rr-suspect]')).toBeNull()
})

it('watchlist tab lists every current member with the stage it stopped at', async () => {
  await render()
  await act(async () => { (host.querySelectorAll('[role="tab"]')[1] as HTMLButtonElement).click() })
  const rows = [...host.querySelectorAll('[data-watch-row]')].map((r) => r.getAttribute('data-watch-row'))
  expect(rows).toEqual(['NVDA', 'RIOT', 'NEW'])
  expect(host.querySelector('[data-watch-row="NVDA"]')!.textContent).toContain('无来源命中')
  expect(host.querySelector('[data-watch-row="RIOT"]')!.textContent).toContain('长下影反击 第37/80名·自选通道')
  expect(host.querySelector('[data-watch-row="NEW"]')!.textContent).toContain('待下次扫描')
  expect(host.querySelector('[data-watchlist-table]')!.textContent).toContain('扫描后已移出：OLD')
  // 自选页的可执行区只放在自选里的卡
  expect(host.querySelector('[data-card="RIOT"]')).not.toBeNull()
  expect(host.querySelector('[data-card="TIGO"]')).toBeNull()
})

it('a failed opportunities request is an error, not a legal zero-opportunity day', async () => {
  mocks.opportunitiesToday.mockRejectedValue(new Error('HTTP 503'))
  mocks.deskOverview.mockRejectedValue(new Error('HTTP 503'))
  await render()
  expect(host.querySelector('[data-load-error]')!.textContent).toContain('这不是“零机会”')
  expect(host.textContent).not.toContain('零机会是合法产出')
  expect(host.querySelector('[data-status-bar][data-error]')!.textContent).toContain('不代表没有机会')
})

it('news summary is fetched in batches of 100 so no card silently loses its catalyst line', async () => {
  const many = Array.from({ length: 150 }, (_, i) => card(`S${String(i).padStart(3, '0')}`, 'bullish_alignment', 'negative'))
  mocks.opportunitiesToday.mockResolvedValue({ as_of: '2026-10-02', gate: 'green', cards: [], watch: many })
  await render()
  const sizes = mocks.newsSummary.mock.calls.map((c) => (c[0] as string[]).length)
  expect(sizes.reduce((a, b) => a + b, 0)).toBe(152)   // 150 观察 + TIGO + RIOT
  expect(Math.max(...sizes)).toBeLessThanOrEqual(100)
})
