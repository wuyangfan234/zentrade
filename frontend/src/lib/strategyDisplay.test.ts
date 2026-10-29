// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanDeskNames, displayStrategyName, stripProvenance, strategyZh } from './strategyDisplay'

describe('P6 内部出处名只在展示层去掉', () => {
  it('去掉「（oasis-quant 原版·吊灯止损）」这类出处括注', () => {
    expect(stripProvenance('Phantom 4h 波段（oasis-quant 原版·吊灯止损）')).toBe('Phantom 4h 波段')
    expect(stripProvenance('Phantom 4h 做空（oasis-quant 原版·空头吊灯止损）')).toBe('Phantom 4h 做空')
    expect(stripProvenance('Phantom 4h Pine 原版 (仪表盘积极做多·吊灯止损)')).toBe('Phantom 4h Pine (仪表盘积极做多·吊灯止损)')
    expect(stripProvenance('新闻催化（只观察）')).toBe('新闻催化（只观察）')
  })
  it('新名「（原版·吊灯止损）」同样去掉，旧台账里的旧名继续兼容', () => {
    expect(stripProvenance('Phantom 4h 波段（原版·吊灯止损）')).toBe('Phantom 4h 波段')
    expect(stripProvenance('Phantom 4h 做空（原版·空头吊灯止损）')).toBe('Phantom 4h 做空')
    expect(displayStrategyName('Phantom 4h 波段（原版·吊灯止损）')).toBe('Phantom 4h（幻影）波段')
  })
  it('cleanDeskNames 清洗 names 映射与各处 name / source_name 字段，不动其他文本与 id', () => {
    const p = {
      names: { phantom_4h: 'Phantom 4h 波段（oasis-quant 原版·吊灯止损）', 'strategy:ut_bot': 'UT Bot 翻多' },
      sources: [{ id: 'phantom_4h', name: 'Phantom 4h 做空（oasis-quant 原版·空头吊灯止损）', note: 'oasis-quant 原版' }],
      rows: [{ source: 'phantom_4h', source_name: 'Phantom 4h 波段（oasis-quant 原版·吊灯止损）' }],
    }
    const out = cleanDeskNames(p)
    expect(out.names.phantom_4h).toBe('Phantom 4h（幻影）波段')
    expect(out.names['strategy:ut_bot']).toBe('UT Bot（ATR 追踪止损）翻多')
    expect(out.sources[0].name).toBe('Phantom 4h（幻影）做空')
    expect(out.sources[0].id).toBe('phantom_4h')
    expect(out.sources[0].note).toBe('oasis-quant 原版')
    expect(out.rows[0].source_name).toBe('Phantom 4h（幻影）波段')
  })
})

describe('P2 英文策略名加中文名（只在展示层）', () => {
  it('常见指标名给出中文名', () => {
    expect(strategyZh('UT Bot 翻多')).toBe('ATR 追踪止损')
    expect(strategyZh('Supertrend 翻空')).toBe('超级趋势')
    expect(strategyZh('收盘上穿 ZLSMA')).toBe('零滞后均线')
    expect(strategyZh('HalfTrend 翻多')).toBe('半趋势')
    expect(strategyZh('WaveTrend 超卖强金叉')).toBe('波浪趋势')
    expect(strategyZh('SMC 波段结构转多')).toBe('聪明钱结构')
    expect(strategyZh('Phantom 4h')).toBe('幻影')
    expect(strategyZh('均线多头')).toBeNull()
    expect(strategyZh('MACD 金叉放量')).toBeNull()
  })
  it('displayStrategyName：英文名后紧跟中文名，已含中文名或纯中文名不重复', () => {
    expect(displayStrategyName('UT Bot 翻多')).toBe('UT Bot（ATR 追踪止损）翻多')
    expect(displayStrategyName('收盘上穿 ZLSMA')).toBe('收盘上穿 ZLSMA（零滞后均线）')
    expect(displayStrategyName('Phantom 趋势换挡买入')).toBe('Phantom（幻影）趋势换挡买入')
    expect(displayStrategyName('Phantom 4h 波段（oasis-quant 原版·吊灯止损）')).toBe('Phantom 4h（幻影）波段')
    expect(displayStrategyName('布林突破')).toBe('布林突破')
    expect(displayStrategyName('UT Bot（ATR 追踪止损）翻多')).toBe('UT Bot（ATR 追踪止损）翻多')
  })
})

describe('api 层对决策台载荷做展示清洗', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('deskSources / deskOverview 返回的名称不含内部出处名', async () => {
    const body = { sources: [{ id: 'phantom_4h', name: 'Phantom 4h 波段（oasis-quant 原版·吊灯止损）' }], names: { phantom_4h: 'Phantom 4h 波段（oasis-quant 原版·吊灯止损）' } }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })))
    const { api } = await import('./api')
    const s = await api.deskSources()
    expect(JSON.stringify(s)).not.toContain('oasis-quant')
    const o = await api.deskOverview()
    expect(JSON.stringify(o)).not.toContain('oasis-quant')
  })
})
