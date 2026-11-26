/** P1 UI 用例与截图共用的虚拟数据 (字段形状 = 后端真实返回, 见 docs/P1-B2-CARD-ATTRIBUTION.md)。 */
import type { SwingBoardCard, SwingBoardsPayload, SwingLedgerMeta, SwingRegimeMeta } from '@/lib/api'

export const REGIME: SwingRegimeMeta = {
  state: '防守', as_of: '2026-11-25T11:00:00-05:00', source: 'intraday', snapshot_state: '震荡', slot: '11:00',
  reasons: ['SPY < EMA10', '跌破前20日低', '波动比5/60 1.62'], caps: { long: 0, short: 5 },
}
export const REGIME_CALM: SwingRegimeMeta = {
  state: '进攻', as_of: '2026-11-24', source: 'snapshot', snapshot_state: '进攻',
  reasons: ['SPY > MA50', 'MA50 20日上行', '自选宽度 61%'],
}

export const LEDGER: SwingLedgerMeta = {
  contract: 'strategy-ledger-v1', status: 'ok', month: '2026-11', regime: '防守', k: 30,
  cells: {
    'tv_halftrend|short': { n: 164, excess: 0.249, tier: '在用' },
    'tv_zlsma_cross|long': { n: 601, excess: 0.127, tier: '观察' },
    'tv_chandelier_exit|short': { n: 210, excess: 0.031, tier: '观察' },
    'tv_smc_structure|short': { n: 29, excess: 0.08, tier: '待定' },
    'volume_price_surge|long': { n: 410, excess: -0.203, tier: '冷冻' },
    'macd_golden|long': { n: 388, excess: -0.112, tier: '待定' },
    'ibs_mean_reversion|long': { n: 140, excess: 0.06, tier: '复活' },
  },
}

const base = (symbol: string, side: 'long' | 'short', extra: Partial<SwingBoardCard> = {}): SwingBoardCard => ({
  contract: 'swing-v2', symbol, side, score: 0.1874, hit_count: 1,
  strategies: [{ id: 'tv_halftrend', name: '半趋势翻空' }], close: 100, entry_zone: side === 'long' ? [97.5, 98.75] : [101.25, 102.5],
  stop: side === 'long' ? 97.5 - 2 : 103.5, target: side === 'long' ? 100.75 + 2 : 98.5, risk: 2, rr: 2, entry_days: 2, hold_days: 5,
  as_of: '2026-11-24', signal_day: '2026-11-24', card_id: `${symbol}-${side}`, state: 'pending', price: 100.4, price_source: 'live',
  verdict: 'order', qty: 40, regime_state: '防守', gate_id: 'multi_scale_v1+4h.event@tight-v1', ledger_tier: 'active', timing_tier: 'T0',
  score_components: { rules: [{ strategy: 'tv_halftrend', n: 164, excess: 0.249, shrunk: 0.2105, tier: 'active' }], k: 30, total: 0.2105 },
  ...extra,
})

export const CARDS = {
  t0: base('NVDA', 'short'),
  t0b: base('AMD', 'short', {
    hit_count: 2, strategies: [{ id: 'tv_halftrend', name: '半趋势翻空' }, { id: 'tv_chandelier_exit', name: '吊灯翻空' }],
    score_components: { rules: [{ strategy: 'tv_halftrend', n: 164, excess: 0.249, shrunk: 0.2105 }, { strategy: 'tv_chandelier_exit', n: 210, excess: 0.031, shrunk: 0.0271, tier: 'observe' }].map((r) => ({ tier: 'active', ...r })), k: 30, total: 0.2376 },
  }),
  observe: base('TSLA', 'short', {
    grade: 'observe', ledger_tier: 'observe', strategies: [{ id: 'tv_chandelier_exit', name: '吊灯翻空' }],
    score_components: { rules: [{ strategy: 'tv_chandelier_exit', n: 210, excess: 0.031, shrunk: 0.0271, tier: 'observe' }], k: 30, total: 0.0271 },
  }),
  t1: base('META', 'short', {
    grade: 'observe', timing_tier: 'T1', blocked: 'observe_only', verdict: undefined, signal_close: 598.2,
    t1: { day: '2026-11-25', valid_until: '2026-11-26', from: '震荡', to: '防守', switched_at: '2026-11-25T11:00:00-05:00', slot: '11:00',
      anchor_price: 591.37, anchor_source: 'live', anchor_at: '2026-11-25T11:02:14-05:00' },
  }),
}

export const swingWith = (cards: SwingBoardCard[], meta: Record<string, unknown> = { regime: REGIME, ledger: LEDGER }): SwingBoardsPayload => ({
  contract: 'swing-v2', available: true, as_of: '2026-11-24', long_rules: [], short_rules: ['tv_halftrend', 'tv_chandelier_exit', 'tv_smc_structure'],
  long: cards.filter((c) => c.side === 'long'), short: cards.filter((c) => c.side === 'short'), meta, generated_at: '2026-11-25T11:05:00-05:00',
})
