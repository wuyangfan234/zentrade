// @vitest-environment jsdom
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

const card = (symbol: string, tab: string, sources: string[], tier = 'proven', state = 'open') => ({
  id: `c-${symbol}`, symbol, setup_type: `strategy:${sources[0]}`, created_date: '2026-10-01', entry_zone: [100, 101],
  invalidation: 95, target_zone: [112, 114], max_holding_days: 7, risk_per_share: 5.5, reward_risk: 2.1, narrative: '',
  state, state_reason: '', evidence: { decision: { tab, sources, tier, score: 0.31, resonance: sources.length, risk_pct: tier === 'explore' ? 0.0025 : 0.01 } },
})

async function render() {
  await act(async () => { root.render(<QueryClientProvider client={qc}><Opportunities /></QueryClientProvider>) })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.opportunitiesToday.mockResolvedValue({
    as_of: '2026-10-01', gate: 'green', scanned: 2037,
    cards: [card('AAA', 'pool', ['long_lower_shadow_reversal', 'trend_breakout']), card('NVDA', 'watchlist', ['strong_open'], 'explore')],
    watch: [card('SSL', 'pool', ['breakout'], 'negative', 'watch')],
  })
  mocks.deskSources.mockResolvedValue({
    sources: [
      { id: 'long_lower_shadow_reversal', name: '长下影反击', status: 'enabled', tier: 'proven', weight: 0.47, risk_pct: 0.01,
        reason: '1795 笔平均 +0.134R，置信下界 +0.005R > 0', evidence: { n: 1795, mean: 0.134, ci_low: 0.005, ci_high: 0.277, win_rate: 0.45 },
        by_bucket: { top100: { n: 140, mean: 0.22 }, top1000: { n: 1655, mean: 0.13 }, watchlist: { n: 79, mean: 0.07 } }, manual: {} },
      { id: 'breakout', name: 'S1 突破形态', status: 'enabled', tier: 'negative', weight: 0, risk_pct: 0,
        reason: '1395 笔平均 -0.077R，置信下界 -0.152R ≤ 0：只展示不开仓', evidence: { n: 1395, mean: -0.077, ci_low: -0.152, ci_high: 0.004, win_rate: 0.44 },
        by_bucket: {}, manual: {} },
    ],
    evaluation: { generated_at: '2026-10-02T04:31:00', window: ['2022-01-13', '2026-09-22'], universe: 'top 1000' },
    rules: { weight: 'w', negative: 'n', explore: 'e', manual: 'm' }, audit: [],
  })
  mocks.deskOverview.mockRejectedValue(new Error('not mocked'))
  mocks.setDeskSourceStatus.mockResolvedValue({ ok: true })
  mocks.newsSummary.mockResolvedValue({ disclaimer: 'x', summaries: {} })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear() })

it('splits cards into 全股票池 and 自选股 tabs', async () => {
  await render()
  const tabs = [...host.querySelectorAll('[role="tab"]')].map((t) => t.textContent)
  // 2026-10-05: 页签计数 = 跨日可执行卡数 / 当前自选只数 (来自总览); 总览不可用时显示 0 / —
  expect(tabs).toEqual(['全股票池 (0)', '自选股 (—)', '来源管理', '卡结局'])
  expect(host.textContent).toContain('AAA')
  expect(host.textContent).not.toContain('NVDA')
  await act(async () => { (host.querySelectorAll('[role="tab"]')[1] as HTMLButtonElement).click() })
  expect(host.textContent).toContain('NVDA')
  expect(host.textContent).toContain('探索单')
})

it('card shows its sources, resonance and score', async () => {
  await render()
  const t = host.querySelector('[data-card="AAA"]')!.textContent ?? ''
  expect(t).toContain('长下影反击')
  expect(t).toContain('2 个来源共振')
  expect(t).toContain('综合分 0.31')
})

it('source management lists evidence and lets the user change status', async () => {
  await render()
  await act(async () => { (host.querySelectorAll('[role="tab"]')[2] as HTMLButtonElement).click() })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const row = host.querySelector('[data-source="breakout"]')!
  expect(row.textContent).toContain('置信下界 -0.152R')
  expect(row.textContent).toContain('只展示不开仓')
  const select = row.querySelector('select') as HTMLSelectElement
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await act(async () => {
    select.value = 'disabled'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(mocks.setDeskSourceStatus).toHaveBeenCalledWith('breakout', 'disabled', expect.any(String))
})

it('shows a 近期催化 line from the batch news summary without changing card order', async () => {
  const top = [{
    id: 'alpaca:9', symbol: 'AAA', published_at: '2026-10-02T14:00:00Z', age_hours: 5, headline: 'AAA 获得 20 亿美元政府订单',
    summary: '', original_headline: 'AAA wins $2B contract', translated: true, tagged: true, tag_mode: 'llm', relevance: 'primary',
    event_type: 'contract', event_label: '合同订单', direction: 'bullish', importance: 'high', rehash: false, mention: 'headline',
    scope: 'focused', sources: ['Benzinga'], url: '', other_symbols: [],
  }]
  mocks.newsSummary.mockResolvedValue({ disclaimer: 'x', summaries: {
    AAA: { count_24h: 1, high_bearish_48h: false, high_bullish_48h: true, top },
    SSL: { count_24h: 0, high_bearish_48h: false, high_bullish_48h: false, top: [] },
  } })
  await render()
  expect(mocks.newsSummary).toHaveBeenCalledWith(['AAA', 'NVDA', 'SSL'])
  const cat = host.querySelector('[data-card="AAA"] [data-card-catalyst]') as HTMLElement
  expect(cat.textContent).toContain('AAA 获得 20 亿美元政府订单')
  expect(cat.textContent).toContain('合同订单')
  expect(cat.textContent).toContain('不影响入选与排序')
  expect(host.querySelector('[data-card="SSL"] [data-card-catalyst]')).toBeNull()
})

it('renders the news catalyst source as locked observe-only with shadow evidence (no status selector)', async () => {
  const base = await mocks.deskSources()
  mocks.deskSources.mockResolvedValue({ ...base, sources: [...base.sources, {
    id: 'news_catalyst', name: '新闻催化（只观察）', status: 'observe', manual: { status: 'observe', locked: true },
    tier: null, weight: null, risk_pct: null, reason: 'x', evidence: null, by_bucket: null, latest: null,
    news_shadow: { source: 'news_catalyst', name: '新闻催化（只观察）', status: 'observe', snapshots: 12, resolved: 0, pending: 12,
      bearish_veto: { n_with: 0, n_without: 0, mean_with: null, mean_without: null, diff_ci: [null, null], months_compared: 0, months_consistent: false, meets_bar: false },
      bullish_boost: { n_with: 0, n_without: 0, mean_with: null, mean_without: null, diff_ci: [null, null], months_compared: 0, months_consistent: false, meets_bar: false },
      verdict: '只观察：带高重要度利空的候选已结算 0 例，未到 30 例门槛', rule: '每组 ≥30 例', since: '2026-10-02' },
  }] })
  await render()
  const tab = [...host.querySelectorAll('[role="tab"]')].find(b => b.textContent?.includes('来源管理')) as HTMLButtonElement
  await act(async () => { tab.click() })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const row = host.querySelector('[data-source="news_catalyst"]') as HTMLElement
  expect(row.textContent).toContain('只观察 · 锁定')
  expect(row.textContent).toContain('已记录 12 个候选快照')
  expect(row.textContent).toContain('未到 30 例门槛')
  expect(row.querySelector('select')).toBeNull()
})

it('source management shows old/new evidence side by side and switching asks for confirmation', async () => {
  const evidence = {
    mode: 'v1', v1: { generated_at: '2026-10-03T22:12' }, v2: { generated_at: '2026-10-05T16:00', contract: 'v2' },
    comparison: {
      long_lower_shadow_reversal: { v1: { n: 1797, mean: 0.135, ci_low: 0.007 }, v2: { n: 1790, mean: 0.06, ci_low: -0.02, censored: 3 },
        tier_v1: 'proven', tier_v2: 'negative', weight_v1: 0.0654, weight_v2: 0, changes_permission: true, version: 'match' },
      breakout: { v1: { n: 1395, mean: -0.077, ci_low: -0.152 }, v2: null, tier_v1: 'negative', tier_v2: null,
        weight_v1: 0, weight_v2: null, changes_permission: false, version: 'unverified' },
    },
    v2_job: { running: false, last: null }, permission_changes: ['long_lower_shadow_reversal'],
  }
  const deskEvidence = vi.fn().mockResolvedValue(evidence)
  const setMode = vi.fn().mockResolvedValue({ ok: true, mode: 'v2', previous: 'v1' })
  const { api } = await import('@/lib/api')
  Object.assign(api, { deskEvidence, setDeskEvidenceMode: setMode, runDeskEvidenceV2: vi.fn() })
  await render()
  await act(async () => { (host.querySelectorAll('[role="tab"]')[2] as HTMLButtonElement).click() })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const box = host.querySelector('[data-evidence-compare]') as HTMLDetailsElement
  expect(box.open).toBe(true)
  expect(box.textContent).toContain('现行 旧算法 v1')
  expect(box.textContent).toContain('其中 1 个可开仓会变')
  const row = host.querySelector('[data-evidence-row="long_lower_shadow_reversal"]')!
  expect(row.textContent).toContain('1797 笔 +0.135R 下界 +0.007R')
  expect(row.textContent).toContain('证据为正 → 证据为负 · 会变')
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  await act(async () => { (host.querySelector('[data-switch-evidence]') as HTMLButtonElement).click() })
  expect(confirm.mock.calls[0][0]).toContain('长下影反击')
  expect(setMode).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  await act(async () => { (host.querySelector('[data-switch-evidence]') as HTMLButtonElement).click() })
  expect(setMode).toHaveBeenCalledWith('v2', expect.any(String))
})
