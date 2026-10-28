// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { buildMiningCostPayload, formatMiningCost, loadMiningDraft } from './MiningWorkbench'

const source = readFileSync(resolve('src/pages/backtest/MiningWorkbench.tsx'), 'utf8')

it('mining form has no stamp-tax input and sends US per-share costs', () => {
  expect(source).not.toMatch(/印花税 bp|draft\.stampTaxBps|stamp_tax_pct:/)
  expect(source).toContain('每股佣金 (USD/股)')
  expect(source).toContain('每笔最低 (USD)')
  const payload = buildMiningCostPayload({ commissionPerShare: '0.005', commissionMin: '1', slippageBps: '5' })
  expect(payload).toEqual({ cost_model: 'us_per_share_v1', commission_per_share: 0.005, commission_min: 1, slippage_bps: 5 })
  expect(payload).not.toHaveProperty('stamp_tax_pct')
  expect(payload).not.toHaveProperty('commission_pct')
})

it('invalid cost input is rejected instead of silently sent', () => {
  expect(buildMiningCostPayload({ commissionPerShare: '-1', commissionMin: '1', slippageBps: '5' })).toBeNull()
  expect(buildMiningCostPayload({ commissionPerShare: '0.005', commissionMin: 'abc', slippageBps: '5' })).toBeNull()
})

it('run summary shows US costs, and labels old runs as legacy stamp tax', () => {
  expect(formatMiningCost({ cost_model: 'us_per_share_v1', commission_per_share: 0.005, commission_min: 1, slippage_bps: 5 }))
    .toBe('每股 $0.005（最低 $1）/ 滑点 5.0bp')
  expect(formatMiningCost({ cost_model: 'legacy_pct', commission_pct: 0.0002, stamp_tax_pct: 0.0005, slippage_bps: 5 }))
    .toBe('旧算法：佣金 2.0bp / 印花 5.0bp / 滑点 5.0bp')
})

it('an old saved draft with stamp-tax fields loads without them', () => {
  localStorage.setItem('mining_workbench_draft_v1', JSON.stringify({ commissionBps: '2', stampTaxBps: '5', slippageBps: '7' }))
  const draft = loadMiningDraft() as unknown as Record<string, unknown>
  expect(draft).not.toHaveProperty('stampTaxBps')
  expect(draft).not.toHaveProperty('commissionBps')
  expect(draft.commissionPerShare).toBe('0.005')
  expect(draft.slippageBps).toBe('7')
  localStorage.clear()
})

it('start button is not sticky, so it cannot cover the cost inputs above it', () => {
  expect(source).not.toMatch(/sticky bottom-0[^"]*"[^>]*>\s*<button[^>]*onClick=\{runMining\}/)
  expect(source).not.toContain('<div className="sticky bottom-0 flex gap-2 bg-base/95 py-2">')
})

it('weekly auto-mining states its bounded pool instead of implying the full market', () => {
  expect(source).toContain('股票池：自选 + 科技/AI/存储/服务器算力/商业航天/生物医药主题，上限 200 只')
})

it('sidebar realtime source label drops the parenthesised market so it fits', () => {
  const layout = readFileSync(resolve('src/components/Layout.tsx'), 'utf8')
  expect(layout).toMatch(/title=\{realtimeProviderName \|\| realtimeModeLabel\}/)
  expect('Alpaca (美股)'.replace(/\s*[（(].*$/, '')).toBe('Alpaca')
})
