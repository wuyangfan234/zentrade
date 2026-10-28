import { expect, it } from 'vitest'
import { benchmarkView } from './benchmark'

it('missing SPY bars stay missing; no substitute curve or return is made up', () => {
  const view = benchmarkView({ config: { benchmark_symbol: 'SPY' }, equity_curve: [{ date: '2026-01-05', value: 1000 }, { date: '2026-01-06', value: 1100 }] })
  expect(view.symbol).toBe('SPY')
  expect(view.values).toEqual([null, null])
  expect(view.totalReturn).toBeNull()
  expect(view.hasData).toBe(false)
})

it('sparse real bars retain gaps and require both interval endpoints for a return', () => {
  const result = { config: { benchmark_symbol: 'SPY' }, equity_curve: [5, 6, 7].map(d => ({ date: `2026-01-0${d}`, value: 1000 })), benchmark_curve: [{ date: '2026-01-06', value: 100, close: 500 }, { date: '2026-01-07', value: 102, close: 510 }] }
  expect(benchmarkView(result).values).toEqual([null, 500, 510])
  expect(benchmarkView(result).totalReturn).toBeNull()
  result.benchmark_curve.unshift({ date: '2026-01-05', value: 100, close: 500 })
  expect(benchmarkView(result).totalReturn).toBeCloseTo(0.02)
})

it('historical benchmark identifiers are never silently relabelled SPY', () => {
  expect(benchmarkView({ config: { cost_model: 'legacy_pct' }, equity_curve: [], benchmark_curve: [{ date: '2024-01-02', value: 1, symbol: 'SPX' }] }).symbol).toBe('SPX')
})
