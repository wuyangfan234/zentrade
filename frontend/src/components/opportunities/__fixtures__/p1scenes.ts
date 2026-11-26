/** UI 检查用的三档数据密度 (满 / 稀疏 / 异常): docs/ui-shots/tools/shoot.py 经 dump-scenes.ts 导成 JSON, 拦截 /api/** 喂给真页面。
 * 字段形状对齐后端真实返回 (OppPagePayload / 规则目录); 全部虚拟数据。 */
import type { OppPagePayload, OppRadarView, RadarBlip, SwingBoardCard, WatchlistRuleCatalogItem } from '@/lib/api'
import { CARDS, LEDGER, REGIME_CALM, swingWith } from './p1'

const view = (): OppRadarView => ({
  contract: 'opp-radar-v1', at: '2026-11-25T11:20:00-05:00',
  clock: { et: '2026-11-25T11:20:00-05:00', trading_day: true, phase: 'session', entry_window: true, entry_close: '15:30', close: '16:00', board_first: '08:00',
    session_day: '2026-11-25', next: { day: '2026-11-25', at: '12:00', code: 'intraday_check', ts: '2026-11-25T12:00:00-05:00' } },
  verdict: { kind: 'go', code: 'orders', orders: 2, blocked: [], holds: 1, exits: [], exit_at: '15:30', nearest: { symbol: 'NVDA', d: 0.25 } },
  caps: { slots: [], sector_cap: 4, risk_pct: 0.005, opens: { used: 1, cap: 10, day: '2026-11-25' }, positions: { used: 1, cap: 12 }, portfolio_risk: { used: 0.014, cap: 0.06, gate: 'green' } },
  lanes: { board: { ticks: ['08:00'], window: ['09:45', '15:30'], fills: [], full: [], orders: 2, refresh_last: '11:05' },
    gap: { at: '09:00', live: false, warming: false, n: 0, threshold: 3 }, open: { at: '09:51', ready_at: '09:51', picks: [], eligible: 40, push: null },
    intraday: { ticks: ['10:00'], schedule: ['10:00', '11:00', '12:00'], fired: 0, active: 0, next: '12:00' } },
  blips: [{ symbol: 'ARM', side: 'short', kind: 'hold', ring: 2, r: 0.67, d: null, bearing: 200, group: '算力',
    hold: { qty: 35, avg: 213.8, price: 210.33, price_source: 'live', price_asof: null, stop_now: 228.27, stop_initial: 228.27, stop_moved: false,
      target: 184.66, entry_date: '2026-11-24', day_n: 2, max_days: 5, exit_day: '2026-11-28', exit_at: '15:30', exit_code: 'time_exit', stop_dist_atr: 1.23,
      r_now: 0.27, pnl_usd: 121, earnings: null } } as RadarBlip],
  price_sources: { live: 4 }, stale_prices: false, frames: 4, last_frame_at: '2026-11-25T11:19:00-05:00', missing: [],
})

const more = (sym: string, px: number): SwingBoardCard => ({ ...CARDS.t0, symbol: sym, card_id: `${sym}-short`, close: px, price: px * 1.004,
  entry_zone: [px * 1.0125, px * 1.025], stop: px * 1.035, target: px * 0.985 })

/** 观察级 / T1 卡的 P1 字段 (价位用各自的票价) */
const pick = (c: SwingBoardCard): Partial<SwingBoardCard> => ({ grade: c.grade, ledger_tier: c.ledger_tier, strategies: c.strategies, score_components: c.score_components,
  timing_tier: c.timing_tier, blocked: c.blocked, verdict: c.verdict, signal_close: c.signal_close, t1: c.t1, card_id: `${c.symbol}-${c.side}-${c.timing_tier}` })

const page = (swing: OppPagePayload['swing'], long: string[], short: string[]): OppPagePayload => ({
  contract: 'opp-page-v1', generated_at: '2026-11-25T11:20:00-05:00', phase: { phase: 'session', label: '盘中', at: '2026-11-25T11:20' },
  swing, intraday: null, radar: null, gaps: null, mood: null, ctx: {}, desk: null, ls_rank: null, themes: null, auto: null,
  settings: { long, short, risk_per_trade_pct: 0.5, ruleset_version: 52, evo: false }, radar_view: view(), errors: {},
})

const SHORT = ['tv_halftrend', 'tv_chandelier_exit', 'tv_smc_structure']
const LONG3 = ['tv_zlsma_cross', 'volume_price_surge', 'macd_golden']

export const OPP = {
  two: page(swingWith([more('NVDA', 178.2), { ...more('AMD', 214.6), hit_count: 2, strategies: CARDS.t0b.strategies, score_components: CARDS.t0b.score_components }]), LONG3, SHORT),
  full: page(swingWith([more('NVDA', 178.2), { ...more('AMD', 214.6), hit_count: 2, strategies: CARDS.t0b.strategies, score_components: CARDS.t0b.score_components }, more('AVGO', 342.1), more('MU', 104.6), more('ORCL', 188.4),
    { ...more('TSLA', 351.3), ...pick(CARDS.observe) }, { ...more('META', 591.37), ...pick(CARDS.t1) }]), LONG3, SHORT),
  sparse: page(swingWith([{ ...CARDS.t0, side: 'long', symbol: 'AAPL', card_id: 'AAPL-long', regime_state: '进攻', entry_zone: [97.5, 98.75], stop: 95.5, target: 102.75,
    strategies: [{ id: 'tv_zlsma_cross', name: 'ZLSMA 金叉' }], score_components: { rules: [{ strategy: 'tv_zlsma_cross', n: 601, excess: 0.127, shrunk: 0.121, tier: 'active' }], k: 30, total: 0.121 } }],
  { regime: REGIME_CALM, ledger: { ...LEDGER, regime: '进攻', cells: { 'tv_zlsma_cross|long': { n: 601, excess: 0.127, tier: '在用' } } } }), ['tv_zlsma_cross'], []),
  empty: page(swingWith([], { regime: REGIME_CALM, ledger: { ...LEDGER, regime: '进攻', cells: { 'tv_zlsma_cross|long': { n: 601, excess: 0.127, tier: '在用' } } } }), ['tv_zlsma_cross'], []),
  /** #11 合并后的预期: 账本格带 ci_low / ci_high (字段形状对齐 strategy_ledger._cell_view, 数值为模拟) */
  ci: page(swingWith([], { regime: REGIME_CALM, ledger: { ...LEDGER, regime: '进攻', cells: {
    'tv_zlsma_cross|long': { n: 601, excess: 0.127, tier: '在用', ci_low: 0.001, ci_high: 0.252 },
    'tv_halftrend|short': { n: 164, excess: 0.249, tier: '在用', ci_low: 0.147, ci_high: 0.352 },
    'tv_chandelier_exit|short': { n: 210, excess: 0.031, tier: '观察', ci_low: -0.05, ci_high: 0.11 },
    'volume_price_surge|long': { n: 410, excess: -0.203, tier: '冷冻', ci_low: -0.31, ci_high: -0.09 },
    'macd_golden|long': { n: 388, excess: -0.112, tier: '待定' } } } }), ['tv_zlsma_cross'], SHORT),
  anomaly: page(swingWith([CARDS.observe, CARDS.t1], { ledger: { status: 'unavailable', month: '2026-12', regime: '震荡', k: 30, cells: {} } }), LONG3, SHORT),
}

const hist = (n: number, mean_r: number, excess: number, p: number) => ({ n, mean_r, excess, p })
const cand = (n: number, excess: number, lo: number, hi: number, passed = false) => ({ passed, n, excess, excess_ci: [lo, hi], mean_r: excess })
export const RULES_FULL: { catalog: WatchlistRuleCatalogItem[] } = { catalog: [
  { id: 'tv_zlsma_cross', name: 'ZLSMA 金叉', kind: 'builtin', enabled: true, forward: null, history: hist(601, 0.21, 0.127, 0.048) },
  { id: 'volume_price_surge', name: '量价齐升', kind: 'builtin', enabled: true, forward: null, history: hist(410, -0.05, -0.203, 0.01) },
  { id: 'macd_golden', name: 'MACD 金叉放量', kind: 'builtin', enabled: true, forward: null, history: hist(388, 0.02, -0.112, 0.08) },
  { id: 'rank_88e88c5b6e', name: '排序因子 A 前 5', kind: 'ranker', enabled: false, forward: null, formula: 'rank(ret_20) * -rank(vol_5)', candidate: cand(158, 0.14, 0.05, 0.23, true) },
  { id: 'cus_shrink_pull', name: '挖掘 · 缩量回踩', kind: 'custom', enabled: false, forward: null, candidate: cand(121, 0.08, -0.03, 0.19) },
  { id: 'ai_strat_a', name: 'AI 策略 A', kind: 'ai', enabled: false, forward: null, candidate: cand(74, 0.05, -0.09, 0.15) },
  { id: 'cus_gap_chase', name: '挖掘 · 跳空追涨', kind: 'custom', enabled: false, forward: null, candidate: cand(96, -0.06, -0.11, -0.01) },
] }
export const RULES_SPARSE: { catalog: WatchlistRuleCatalogItem[] } = { catalog: RULES_FULL.catalog.slice(0, 1) }
