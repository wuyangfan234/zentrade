// @vitest-environment jsdom
// 机会页 = 赚钱闭环 (2026-10-05 ⑤): 持仓+下一步动作 / 事件雷达 / 来源体检 (v3.1 三套成绩) / 前向确认进度 / 切换需确认
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  opportunitiesToday: vi.fn(), deskSources: vi.fn(), setDeskSourceStatus: vi.fn(), newsSummary: vi.fn(), deskOverview: vi.fn(),
  deskEvidence: vi.fn(), setDeskEvidenceMode: vi.fn(), runDeskEvidenceV3: vi.fn(), runDeskEvidenceV2: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/PageHeader', () => ({ PageHeader: () => null }))

import { EvidenceDesk as Opportunities } from './EvidenceDesk'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const baseOverview = {
  status: {
    today: '2026-10-05', in_session: false, latest_scan: '2026-10-02', scan_generated_at: null, gate: 'green',
    tradable_sources: [], evaluated_sources: 21, executable: 0, executable_clear: 0, circuit_breaker: null,
    last_quote_at: null, last_run_at: null, entry_blockers: [], crowding: {},
    account: { nav: 100801, cash: 16133, buying_power: 16133, positions: 2, max_positions: 6, open_risk: 1500,
               open_risk_pct: 1.5, max_portfolio_risk_pct: 6, risk_per_trade_pct: 1, marks_origin: ['close'], unmarked: [] },
  },
  executable: [], live_other_count: 0,
  watchlist: { ok: true, count: 0, scan_day: '2026-10-02', removed_since_scan: [], rows: [] },
  positions: [
    { symbol: 'CVE', side: 'long', qty: 677, avg_cost: 31.07, last: 30.6, mark_source: 'close', r_now: -0.73, stop: 29.6,
      stop_dist_pct: -3.3, target: [34.4, 35.1], target_dist_pct: 12.5, held_days: 3, max_days: 7, source: 'pullback',
      source_name: 'S2 回踩', action: '持有中：按止损/目标自动管理', urgency: 'low' },
    { symbol: 'TH', side: 'long', qty: 810, avg_cost: 19.49, last: 19.1, mark_source: 'close', r_now: -0.4, stop: 19.49,
      stop_dist_pct: 2, target: [22, 22.5], target_dist_pct: 15, held_days: 4, max_days: 7, source: 'pullback',
      source_name: 'S2 回踩', action: '待离场：2026-10-02 收盘触发移动止损，下一交易时段按报价卖出', urgency: 'high' },
  ],
  events: {
    premarket: { date: '2026-10-05', alerts: [{ symbol: 'COIN', roles: ['机会卡', '自选'], gap_pct: 3.5, direction: 'up' }] },
    earnings: [{ symbol: 'CVE', date: '2026-10-08', timing: 'bmo', roles: ['持仓'] }],
    earnings_source: 'nasdaq 日历缓存',
  },
  evidence: { mode: 'v3', v3_generated_at: '2026-10-05T23:07', forward_min_n: 300,
              forward: [{ source: 'long_lower_shadow_reversal', n: 0, months: 0, p: null, threshold: 0.006, promoted: false, progress_text: '0/300 笔' }] },
  names: { long_lower_shadow_reversal: '长下影反击' },
}

const v3row = (source: string, tier: string, extra: Record<string, unknown> = {}) => ({
  source, name: null, kind: 'strategy', n: 3042, mean: 0.11, median: -0.22, baseline: 0.032,
  excess: { n: 3042, mean: 0.078, ci_low: -0.005, ci_high: 0.16, p: 0.033 },
  discovery: { n: 1633, mean: 0.057, ci_low: null, ci_high: null, p: 0.1 },
  holdout: { n: 1409, mean: 0.102, ci_low: null, ci_high: null, p: 0.08 },
  by_year: { 2022: { n: 597, mean_r: 0.13, mean_excess: 0.084 } }, concentration: { drop_top_20: 0.042 },
  delisted_trades: 266, holm_pass: false, tier, reason: '3042 笔平均 +0.110R…同时检验多个来源后，优势不足以排除运气：只展示不开仓',
  forward: { candidate: true, n: 0, months: 0, excess: null, p: null, threshold: 0.006, promoted: false, progress_text: '0/300 笔、0/6 个月、p=—' },
  shadow: null, real: null, ...extra,
})

const evidence = {
  mode: 'v1', v1: { generated_at: 'x' }, v2: null, comparison: {}, v2_job: { running: false, last: null },
  v3_job: { running: false, last: null }, permission_changes: [],
  v3: {
    contract: 'v3.1-calibrated-2026-10-05', window: ['2022-01-13', '2026-09-18'], generated_at: '2026-10-05T23:07',
    holdout_start: '2025-01-01', cluster_unit: 'month', calibration: {}, with_delisted: true, hypotheses_tested: 21,
    sources: [v3row('long_lower_shadow_reversal', 'negative'),
              v3row('earnings_beat_drift', 'negative', { kind: 'event', name: '财报超预期后漂移', forward: null })],
    forward_rule: { start: '2026-10-06', min_n: 300, min_months: 6, candidates: ['long_lower_shadow_reversal'] },
    baseline_shadow: null,
  },
}

async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><Opportunities /></QueryClientProvider>) })
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.opportunitiesToday.mockResolvedValue({ as_of: '2026-10-02', gate: 'green', scanned: 2053, cards: [], watch: [] })
  mocks.deskOverview.mockResolvedValue(baseOverview)
  mocks.deskSources.mockResolvedValue({ sources: [], evaluation: {}, rules: {}, audit: [] })
  mocks.deskEvidence.mockResolvedValue(evidence)
  mocks.setDeskEvidenceMode.mockResolvedValue({ ok: true, mode: 'v3', previous: 'v1' })
  mocks.newsSummary.mockResolvedValue({ disclaimer: 'x', summaries: {} })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
})

it('shows held positions with the most urgent next action first', async () => {
  await render()
  const rows = [...host.querySelectorAll('[data-position]')].map((e) => e.getAttribute('data-position'))
  expect(rows).toEqual(['CVE', 'TH'])            // 顺序由后端排好 (这里原样展示)
  const th = host.querySelector('[data-position="TH"]')!
  expect(th.textContent).toContain('待离场')
  expect(th.className).toContain('rose')
  expect(host.querySelector('[data-position="CVE"]')!.textContent).toContain('第 3/7 天')
})

it('shows the event radar: premarket gaps and upcoming earnings with roles', async () => {
  await render()
  const radar = host.querySelector('[data-event-radar]')!
  expect(radar.querySelector('[data-premarket]')!.textContent).toContain('COIN +3.5%（机会卡/自选）')
  expect(radar.querySelector('[data-earnings]')!.textContent).toContain('CVE 10-08 盘前（持仓）')
})

it('status bar states the permission ruler and forward-confirmation progress', async () => {
  await render()
  const brief = host.querySelector('[data-evidence-brief]')!
  expect(brief.textContent).toContain('新尺子 v3.1')
  expect(brief.textContent).toContain('长下影反击 0/300')
})

it('source health board shows three sets of results vs random baseline and forward progress', async () => {
  await render()
  await act(async () => { (host.querySelectorAll('[role=tab]')[2] as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const board = host.querySelector('[data-source-health]')!
  expect(board.textContent).toContain('可开仓 0/2')
  expect(board.textContent).toContain('含已退市股票')
  const row = board.querySelector('[data-v3-row="long_lower_shadow_reversal"]')!
  expect(row.textContent).toContain('比随机 +0.078R')
  expect(row.textContent).toContain('影子前向 尚无')
  expect(row.textContent).toContain('真实纸面 0 笔')
  expect(row.querySelector('[data-forward]')!.textContent).toContain('0/300 笔')
  expect(board.querySelector('[data-v3-row="earnings_beat_drift"]')!.textContent).toContain('事件')
})

it('switching to v3 asks for confirmation and names the sources that stay tradable', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  await render()
  await act(async () => { (host.querySelectorAll('[role=tab]')[2] as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  await act(async () => { (host.querySelector('[data-switch-v3]') as HTMLButtonElement).click() })
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('无（系统进入纯观察，前向确认通过后自动恢复）'))
  expect(mocks.setDeskEvidenceMode).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  await act(async () => { (host.querySelector('[data-switch-v3]') as HTMLButtonElement).click() })
  expect(mocks.setDeskEvidenceMode).toHaveBeenCalledWith('v3', '机会页确认切换')
})

it('old backend without positions/events/evidence fields still renders', async () => {
  const { positions: _p, events: _e, evidence: _v, ...old } = baseOverview
  mocks.deskOverview.mockResolvedValue(old)
  await render()
  expect(host.querySelector('[data-status-bar]')).not.toBeNull()
  expect(host.querySelector('[data-positions]')).toBeNull()
  expect(host.querySelector('[data-event-radar]')).toBeNull()
  expect(host.querySelector('[data-evidence-brief]')).toBeNull()
})
