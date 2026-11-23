// 挖掘候选 → 发布链路: 去发布深链 + 候选方案 / 挖掘页 / 策略池三处同名 (2026-11-22)
import { expect, it } from 'vitest'
import type { ResearchCandidate } from './api'
import {
  minedCandidateRef,
  minedFactorName,
  miningCandidateHref,
  miningFocusState,
  publishedStrategyName,
  researchCandidateName,
} from './minedCandidate'
import { humanizeFactorIds } from './strategyDisplay'

const LABELS = new Map([
  ['amplitude', '日振幅'], ['boll_width', '布林带宽'], ['atr_14', 'ATR(14)原值'], ['volume_ratio_5d', '5日量比'],
])

// 生产 user_data/research_candidates.json 的三条 (2026-11-23 只读取证), 只留与显示有关的字段
function candidate(name: string, factor: string, direction: 'high' | 'low', run: string, strategyId: string, extra: Partial<ResearchCandidate> = {}): ResearchCandidate {
  return {
    id: `rc_${factor}`, kind: 'strategy', name, source_id: strategyId, status: 'validated', data_as_of: '2026-11-20',
    created_at: '', updated_at: '', metrics: {},
    config: {
      origin_run_id: run, candidate_signature: `factor:${factor}|w:1|d:${direction === 'low' ? -1 : 1}`,
      strategy_id: strategyId, factor_names: [factor], directions: [direction],
    },
    ...extra,
  }
}
const AMPLITUDE = candidate('因子组合 · amplitude', 'amplitude', 'low', 'weekly-2026-W47', 'mined_factor_7df8d78a3ca7c60f3b4e')
const BOLL = candidate('因子组合 · boll_width', 'boll_width', 'high', 'weekly-2026-W47', 'mined_factor_abe24aef5a06a655bc01')
const ATR = candidate('因子组合 · atr_14', 'atr_14', 'low', 'weekly-2026-W46', 'mined_factor_797ee728df832027ee16')
// 线上已发布的 atr_14 策略文件名是旧版默认名
const PUBLISHED = [{ id: 'mined_factor_797ee728df832027ee16', name: '因子组合 · atr_14' }]

it('the publish deep link opens the candidate\'s own mining run with that candidate selected', () => {
  const href = miningCandidateHref('weekly-2026-W47', 'factor:amplitude|w:1|d:-1')
  const url = new URL(href, 'http://x')
  expect(url.pathname).toBe('/factors')
  expect(url.searchParams.get('tab')).toBe('mining')
  expect(url.searchParams.get('run')).toBe('weekly-2026-W47')
  // 签名里的 | 与 : 编码后能原样读回 (挖掘页按签名选中)
  expect(url.searchParams.get('candidate')).toBe('factor:amplitude|w:1|d:-1')
  // 没有溯源的旧候选只到挖掘页, 不带一个空 run / 孤立 candidate
  expect(miningCandidateHref(null, 'factor:amplitude|w:1|d:-1')).toBe('/factors?tab=mining')
  expect(miningCandidateHref('weekly-2026-W47', null)).toBe('/factors?tab=mining&run=weekly-2026-W47')
  expect(miningFocusState('factor:amplitude|w:1|d:-1')).toEqual({ miningFocus: 'factor:amplitude|w:1|d:-1' })
  expect(miningFocusState(null)).toBeUndefined()
})

it('default name matches backend default_strategy_name (factor label + direction)', () => {
  // 与 backend/tests/test_mined_strategy_publish_name.py 同一组期望
  expect(minedFactorName(['atr_14'], { atr_14: 'low' }, LABELS)).toBe('ATR(14)原值 低')
  expect(minedFactorName(['atr_14', 'volume_ratio_5d'], { atr_14: 'low', volume_ratio_5d: 'high' }, LABELS)).toBe('ATR(14)原值 低 + 5日量比 高')
  expect(minedFactorName(['uf_x'], {}, LABELS)).toBe('uf_x 高')
  // 候选方案里 directions 是与 factor_names 对齐的列表
  expect(minedFactorName(['amplitude', 'boll_width'], ['low', 'high'], LABELS)).toBe('日振幅 低 + 布林带宽 高')
  expect(minedFactorName([], [], LABELS)).toBe('因子组合')
  // 超过 40 字截断到 40 字并以 … 结尾 (后端 name[:39] + '…')
  const long = minedFactorName(['a', 'b', 'c', 'd'], [], new Map([['a', '甲'.repeat(12)], ['b', '乙'.repeat(12)], ['c', '丙'.repeat(12)], ['d', '丁'.repeat(12)]]))
  expect(Array.from(long)).toHaveLength(40)
  expect(long.endsWith('…')).toBe(true)
})

it('candidate list names: unpublished = publish default name, published = the strategy\'s own pool name', () => {
  expect(researchCandidateName(AMPLITUDE, LABELS, PUBLISHED)).toBe('日振幅 低')
  expect(researchCandidateName(BOLL, LABELS, PUBLISHED)).toBe('布林带宽 高')
  expect(researchCandidateName(ATR, LABELS, PUBLISHED)).toBe('因子组合 · ATR(14)原值')
  // 与策略池 (Screener 的 humanizeFactorIds) 逐字相同
  expect(researchCandidateName(ATR, LABELS, PUBLISHED)).toBe(humanizeFactorIds(PUBLISHED[0].name, LABELS))
  // 新版发布名本来就是中文, 原样
  expect(researchCandidateName(AMPLITUDE, LABELS, [{ id: 'mined_factor_7df8d78a3ca7c60f3b4e', name: '日振幅 低' }])).toBe('日振幅 低')
  // 因子目录或策略列表还没到: 先用保存时的名字, 不闪一个随后会变的名字
  expect(researchCandidateName(AMPLITUDE, new Map(), PUBLISHED)).toBe('因子组合 · amplitude')
  expect(researchCandidateName(ATR, LABELS, undefined)).toBe('因子组合 · atr_14')
  // 回测页保存的策略候选、因子候选不是挖掘候选: 名字不动
  const backtest = { ...AMPLITUDE, name: '趋势突破候选', config: { strategy_id: 'trend_breakout' } }
  expect(minedCandidateRef(backtest)).toBeNull()
  expect(researchCandidateName(backtest, LABELS, [])).toBe('趋势突破候选')
  expect(minedCandidateRef({ ...AMPLITUDE, kind: 'factor' })).toBeNull()
})

it('mined candidate ref carries the provenance the deep link needs', () => {
  expect(minedCandidateRef(AMPLITUDE)).toEqual({
    runId: 'weekly-2026-W47', signature: 'factor:amplitude|w:1|d:-1', strategyId: 'mined_factor_7df8d78a3ca7c60f3b4e',
    factorIds: ['amplitude'], directions: ['low'],
  })
  const noProvenance = { ...AMPLITUDE, config: { factor_names: ['amplitude'], directions: ['low'] } }
  expect(minedCandidateRef(noProvenance)).toMatchObject({ runId: null, signature: null, strategyId: '' })
})

it('legacy mined names are humanized id by id, including ids without an underscore', () => {
  expect(humanizeFactorIds('因子组合 · amplitude', LABELS)).toBe('因子组合 · 日振幅')
  expect(humanizeFactorIds('因子组合 · amplitude + atr_14', LABELS)).toBe('因子组合 · 日振幅 + ATR(14)原值')
  expect(humanizeFactorIds('因子组合 · 我的组合', LABELS)).toBe('因子组合 · 我的组合')
  // 其他名字仍只换带下划线的 id, 不误伤普通英文词
  expect(humanizeFactorIds('amplitude breakout', LABELS)).toBe('amplitude breakout')
  expect(humanizeFactorIds('低波 · atr_14', LABELS)).toBe('低波 · ATR(14)原值')
  expect(publishedStrategyName('因子组合 · atr_14', new Map())).toBe('因子组合 · atr_14')
})
