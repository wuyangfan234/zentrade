// @vitest-environment jsdom
/** 2026-11-15 B 档: 口径标签 / 主题压力 / 历史日回看 / 因子库状态 / 周度挖掘文字 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AsOfPayload, FactorLibraryItem, ReplayRuler, ThemeExposure } from '@/lib/api'

const mocks = vi.hoisted(() => ({ oppAsof: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { RulerTag, rulerLabel, rulerTitle } from './RulerTag'
import { ThemeBar } from './ThemeBar'
import { AsOfView } from './AsOfView'
import { displayStatus, maturityTitle } from '@/pages/factors/FactorLibrary'
import { weeklyText } from '@/pages/factors/weeklySchedule'

let root: Root
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  mocks.oppAsof.mockReset()
})
afterEach(() => { act(() => root.unmount()); host.remove() })

const R: ReplayRuler = { contract: 'replay-ruler-v1', kind: 'post_hoc', n: 46, fixed: '2026-11-09', window: ['2023-01-03', '2026-10-30'], watch_now: 47, changed: true }

it('口径标签: 事后名单 46 + 变动点; 说明只在悬停', async () => {
  expect(rulerLabel(R)).toBe('事后名单 46')
  expect(rulerTitle(R)).toBe('名单 2026-11-09 · 46 只 · 回看 2023-01-03 → 2026-10-30 · 现名单 47 只 · 与现名单不同 · 名单晚于回看起点 · 事后挑选')
  expect(rulerLabel({ ...R, kind: 'unrecorded', n: null, fixed: null })).toBe('名单未记录')
  await act(async () => { root.render(<RulerTag r={R} />) })
  const el = host.querySelector('[data-ruler]')!
  expect(el.textContent).toBe('事后名单 46')
  expect(el.hasAttribute('data-ruler-changed')).toBe(true)
  expect(el.getAttribute('title')).toContain('回看 2023-01-03')
})

const THEMES: ThemeExposure = {
  contract: 'theme-exposure-v1', stress_pct: 20, levels: [10, 15, 20, 25, 30], nav: 105517.63, positions: 6, hidden: 0,
  tiers: [{ name: '核心', n: 31 }],
  rows: [{ theme: '科技巨头', symbols: ['AMZN', 'TSLA'], count: 2, notional: 23799, notional_pct: 22.6, stop_risk: 1040, stress_loss: 4760, stress_pct_nav: 4.51,
    pending: [], after_fill_loss: 4760, after_fill_pct_nav: 4.51 }],
  total: { notional: 57913, notional_pct: 54.9, stop_risk: 2546, stop_risk_pct: 2.41, stress_loss: 11583, stress_pct_nav: 10.98 },
}

it('主题压力: 一行一个主题; 合计写计划止损与整组跌幅两种口径', async () => {
  await act(async () => { root.render(<ThemeBar t={THEMES} />) })
  const row = host.querySelector('[data-theme-row="科技巨头"]')!
  expect(row.textContent).toContain('22.6%')
  expect(row.textContent).toContain('−$4,760 · −4.51%')
  const meta = host.querySelector('[data-theme-meta]')!
  expect(meta.textContent).toBe('持仓 6 · 名义 54.9% · 计划止损 $2,546（2.41%） · 全部跌 20% −$11,583（−10.98%）')
  expect(meta.getAttribute('title')).toBe('核心 31 只 · 覆盖过半 · 不算主题')
  // 只有挂单、没有持仓的主题: 持仓三列写「—」, 只给挂单全成交后
  const pendOnly = { ...THEMES.rows[0], theme: '医药', symbols: [], count: 0, notional: 0, notional_pct: 0, stop_risk: 0, stress_loss: 0, stress_pct_nav: 0,
    pending: ['LLY'], after_fill_loss: 3098, after_fill_pct_nav: 2.94 }
  await act(async () => { root.render(<ThemeBar t={{ ...THEMES, rows: [pendOnly] }} />) })
  expect([...host.querySelectorAll('[data-theme-row="医药"] td')].map((td) => td.textContent)).toEqual(['医药', '0', '—', '—', '—', '−$3,098 · −2.94%'])
  await act(async () => { root.render(<ThemeBar t={{ ...THEMES, positions: 0, rows: [] }} />) })
  expect(host.querySelector('[data-theme-meta]')!.textContent).toBe('暂无持仓')
  expect(host.querySelector('table')).toBeNull()
})

const ASOF: AsOfPayload = {
  contract: 'opp-asof-v1', asof: '2026-11-13', today: '2026-11-16', days: ['2026-11-13'],
  board: { signal_days: ['2026-11-11', '2026-11-12'], built_at: null, rules: null, skipped: [],
    cards: [{ card_id: 'c1', signal_day: '2026-11-12', symbol: 'LLY', side: 'long', entry_zone: [1400, 1410], stop: 1380, target: 1450, reward_risk: 2,
      score: 1, rules: [{ id: 'ibs_mean_reversion', name: 'IBS 均值回归' }], outcome: { status: 'closed', r: -1.02, why: 'stop', fill_date: null, exit_date: null, settled_on: null } }] },
  ls_rank: null, intraday: null, radar: null, resonance: null, mood: null,
  errors: { ls_rank: 'not_archived', intraday: 'not_archived', radar: 'not_archived', resonance: 'not_archived', mood: 'not_archived' },
}

it('历史日回看: 没存档的块写「未存档」; 结局列与当天列分开', async () => {
  mocks.oppAsof.mockResolvedValue(ASOF)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><AsOfView day="2026-11-13" onOpen={() => {}} /></QueryClientProvider>) })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(mocks.oppAsof).toHaveBeenCalledWith('2026-11-13')
  expect(host.querySelectorAll('[data-asof-missing]').length).toBe(5)
  const card = host.querySelector('[data-asof-card="c1"]')!
  expect(card.textContent).toContain('IBS 均值回归')
  expect(card.lastElementChild!.textContent).toBe('止损 −1.02R')
  expect(card.lastElementChild!.className).toContain('border-l')
})

const item = (over: Partial<FactorLibraryItem> = {}): FactorLibraryItem => ({
  id: 'annual_vol_20d', label: '20日波动率', group: '波动', kind: 'base', version: 1, formula: '', direction: 'none', unit: '', warmup_bars: 20,
  pit: false, asset_types: ['stock'], stability: 'stable', scale_free: true, dependencies: [], origin: 'builtin', status: 'active', ...over,
})

it('因子库状态: 内置因子没有评估 = 未评估 (不再默认在用); 上线后按派生成熟度; 自定义的手动状态优先', () => {
  expect(displayStatus(item(), undefined).label).toBe('未评估')
  expect(displayStatus(item({ maturity: { state: 'retired', icir: 0.0791, n: 251, streak: 4 } }), { launched: false }).label).toBe('未评估')
  const meta = { launched: true, month: '2026-10-30', pool: 200 }
  const st = displayStatus(item({ maturity: { state: 'retired', icir: 0.0791, n: 251, streak: 4 } }), meta)
  expect(st.label).toBe('退役')
  expect(st.title).toBe('退役 · 12 月 ICIR 0.079 · 连续 4 个月末低于 0.2 · 样本 251 天 · 主题池 200 · 2026-10 月末')
  const custom = displayStatus(item({ origin: 'intake', status: 'watch', maturity: { state: 'experiment' } }), meta)
  expect(custom.label).toBe('观察')
  expect(custom.manual).toBe(true)
  expect(maturityTitle({ state: 'experiment' }, meta)).toBe('实验 · 并入不足 180 天 · 不显示读数')
})

it('周度挖掘: 开关关着不写「下次」, 写最近一轮', () => {
  expect(weeklyText({ next_weekly: null, enabled: false, last_weekly: { run_id: 'weekly-2026-W44', week: '2026-W44', day: '2026-10-30' } }))
    .toBe('已停用 · 最近 W44（10-30）')
  expect(weeklyText({ next_weekly: '2026-11-20', enabled: true }, '周五 收盘后')).toBe('下次 11-20 周五 收盘后')
})
