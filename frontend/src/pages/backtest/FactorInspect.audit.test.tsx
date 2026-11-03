// @vitest-environment jsdom
// 审计回归: 检验页 (findings-inspect-2 A4/A5/A9/A10/A11)
import { describe, expect, it } from 'vitest'
import { directionalHitRate, parseSymbols } from '@/lib/format'
import { uniqueSignalId } from './AddFactorSignalDialog'
import { mergeFactorIntoScoring } from './ResearchCandidatesDialog'

describe('parseSymbols (A9)', () => {
  it('uppercases and splits on commas, full-width commas and spaces', () => {
    expect(parseSymbols('aapl,nvda， msft  tsla,AAPL')).toEqual(['AAPL', 'NVDA', 'MSFT', 'TSLA'])
    expect(parseSymbols('')).toEqual([])
  })
})

describe('directionalHitRate (A5)', () => {
  it('reports the share of days agreeing with the factor direction', () => {
    expect(directionalHitRate(-0.0364, 0.36)).toBeCloseTo(0.64)
    expect(directionalHitRate(0.02, 0.58)).toBeCloseTo(0.58)
    expect(directionalHitRate(null, 0.5)).toBeCloseTo(0.5)
  })
})

describe('uniqueSignalId (A4)', () => {
  it('never reuses an existing signal id', () => {
    expect(uniqueSignalId('rsi_14', '<', [])).toBe('f_rsi_14_lt')
    expect(uniqueSignalId('rsi_14', '<', ['f_rsi_14_lt'])).toBe('f_rsi_14_lt_2')
    expect(uniqueSignalId('rsi_14', '>', ['f_rsi_14_lt'])).toBe('f_rsi_14_gt')
    const long = 'uf_' + 'a'.repeat(40)
    const first = uniqueSignalId(long, '>', [])
    expect(first.length).toBeLessThanOrEqual(40)
    expect(uniqueSignalId(long, '>', [first])).not.toBe(first)
    expect(/^[a-z0-9_]{1,40}$/.test(uniqueSignalId(long, '>', [first]))).toBe(true)
  })
})

describe('mergeFactorIntoScoring (A10)', () => {
  it('keeps share-capital weights untouched so the strategy patch is accepted', () => {
    const merged = mergeFactorIntoScoring({ momentum_60d: 0.4, momentum_20d: 0.3, turnover_rate: 0.3 }, 'rsi_14', 0.2)
    expect(merged.turnover_rate).toBe(0.3)
    expect(merged.rsi_14).toBe(0.2)
    expect(merged.momentum_60d).toBeCloseTo(0.4 / 0.7 * 0.5, 5)
    expect(merged.momentum_20d).toBeCloseTo(0.3 / 0.7 * 0.5, 5)
    expect(Object.values(merged).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 5)
  })
  it('falls back to the factor alone when nothing else is adjustable', () => {
    expect(mergeFactorIntoScoring({ turnover_rate: 0.5 }, 'rsi_14', 0.2)).toEqual({ turnover_rate: 0.5, rsi_14: 1 })
    expect(mergeFactorIntoScoring({}, 'rsi_14', 0.2)).toEqual({ rsi_14: 1 })
  })
})
