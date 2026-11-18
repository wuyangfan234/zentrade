// @vitest-environment jsdom
// 2026-11-18 清查复现: 同一条策略全站一个名字 (IBS 均值回归); 策略页负号用全角减号; 0 不带符号。
import { afterEach, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
import { api } from './api'
import { canonicalStrategyName, displayStrategyName, humanizeFactorIds } from './strategyDisplay'
import { fmtR } from './strategyPanel'

afterEach(() => { vi.unstubAllGlobals() })
const serve = (body: unknown) => vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })))

it('QS-IBS is shown as IBS 均值回归 by every name helper', () => {
  expect(canonicalStrategyName('QS-IBS')).toBe('IBS 均值回归')
  expect(displayStrategyName('QS-IBS')).toBe('IBS 均值回归')
  expect(canonicalStrategyName('IBS 低位反弹')).toBe('IBS 低位反弹')
  expect(canonicalStrategyName('收盘上穿 ZLSMA')).toBe('收盘上穿 ZLSMA')
})

it('strategy lists, strategy detail, rule catalog and the factor page all receive the unified name', async () => {
  serve({ strategies: [{ id: 'ibs_mean_reversion', name: 'QS-IBS' }, { id: 'x', name: '布林突破' }] })
  expect((await api.screenerStrategies(undefined, 'all')).presets.map(s => s.name)).toEqual(['IBS 均值回归', '布林突破'])
  expect((await api.strategyList()).strategies[0].name).toBe('IBS 均值回归')
  serve({ id: 'ibs_mean_reversion', name: 'QS-IBS' })
  expect((await api.strategyGet('ibs_mean_reversion')).name).toBe('IBS 均值回归')
  serve({ catalog: [{ id: 'ibs_mean_reversion', name: 'QS-IBS' }], short_rules: [] })
  expect((await api.watchlistRules()).catalog[0].name).toBe('IBS 均值回归')
  serve({ week: { engine: { legs: [{ id: 'ibs_mean_reversion', name: 'QS-IBS' }, { id: 'z', name: '收盘上穿 ZLSMA' }] } } })
  expect((await api.factorLabPage()).week!.engine!.legs.map(l => l.name)).toEqual(['IBS 均值回归', '收盘上穿 ZLSMA'])
  serve({ week: null })
  expect((await api.factorLabPage()).week).toBeNull()
})

it('factor ids inside a name are replaced by library labels', () => {
  const labels = new Map([['annual_vol_20d', '20日波动率']])
  expect(humanizeFactorIds('因子组合 · annual_vol_20d', labels)).toBe('因子组合 · 20日波动率')
  expect(humanizeFactorIds('因子组合 · no_such_id', labels)).toBe('因子组合 · no_such_id')
})

it('strategy page prints negative R with the same minus sign as the other pages', () => {
  expect(fmtR(-1.0567)).toBe('−1.06R')
  expect(fmtR(-0.004)).toBe('0.00R')
  expect(fmtR(0.2494)).toBe('+0.25R')
})
