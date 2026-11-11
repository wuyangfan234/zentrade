// 策略页面板的展示换算: 一个策略一行、默认看哪一面、R 的写法、机会板现状叫法、累计曲线的点。
import { expect, it } from 'vitest'
import type { StrategyPanelEntry, StrategyPanelPaper, StrategyPanelSide } from '@/lib/api'
import {
  boardLabel,
  buildRailRow,
  curvePoints,
  dayPoints,
  defaultSide,
  filterRailRows,
  fmtP,
  fmtR,
  fmtRate,
  hasFullReplay,
  md,
  paperState,
  selectedLabel,
  selectedSides,
  sideOf,
  sortRailRows,
  toneClass,
  whyLabel,
} from './strategyPanel'

const side = (over: Partial<StrategyPanelSide> = {}): StrategyPanelSide => ({
  selected: false, replay: null, hits: [], hist: [], board: [], paper: null, ...over,
})
const paper = (over: Partial<StrategyPanelPaper> = {}): StrategyPanelPaper => ({
  cards: 4, fills: 3, days: 3, fill_rate: 0.75, sum_r: 1.5, mean_r: 0.5, win: 2, loss: 1, ci: null, diverged: false,
  replay_mean_r: 0.3, replay_fill: 0.6, trades: [], ...over,
})
const entry = (long: Partial<StrategyPanelSide>, short?: Partial<StrategyPanelSide>): StrategyPanelEntry => ({
  short_capable: !!short, name_short: short ? '翻空' : null,
  sides: short ? { long: side(long), short: side(short) } : { long: side(long) },
})

it('默认看入选的那一面: 只有做空入选时看空, 其余看多', () => {
  expect(defaultSide(entry({ selected: true }))).toBe('long')
  expect(defaultSide(entry({}, { selected: true }))).toBe('short')
  expect(defaultSide(entry({ selected: true }, { selected: true }))).toBe('long')
  expect(defaultSide(entry({}, {}))).toBe('long')
  expect(defaultSide(null)).toBe('long')
})

it('没有做空那一面的策略, 要空也只给做多 (不出空白详情)', () => {
  const e = entry({ hits: ['AAPL'] })
  expect(sideOf(e, 'short')?.hits).toEqual(['AAPL'])
  expect(sideOf(null, 'long')).toBeNull()
})

it('入选标签写清是哪一面', () => {
  expect(selectedSides(entry({ selected: true }, { selected: true }))).toEqual(['long', 'short'])
  expect(selectedLabel(['long'])).toBe('入选 · 多')
  expect(selectedLabel(['short'])).toBe('入选 · 空')
  expect(selectedLabel(['long', 'short'])).toBe('入选 · 多空')
})

it('R 的写法: 带正负号, 四舍五入到 0 时不带负号, 没有数给破折号', () => {
  expect(fmtR(0.2494)).toBe('+0.25R')
  expect(fmtR(-1.0567)).toBe('-1.06R')
  expect(fmtR(-0.004)).toBe('0.00R')
  expect(fmtR(7.61, 1)).toBe('+7.6R')
  expect(fmtR(null)).toBe('—')
  expect(fmtR(Number.NaN)).toBe('—')
})

it('红涨绿跌: 正 = 红, 负 = 绿, 0 与没有数不着色', () => {
  expect(toneClass(0.3)).toBe('text-bull')
  expect(toneClass(-0.3)).toBe('text-bear')
  expect(toneClass(-0.004)).toBe('text-secondary')
  expect(toneClass(null)).toBe('text-muted')
})

it('比例、p 值、日期的写法', () => {
  expect(fmtRate(0.613)).toBe('61%')
  expect(fmtRate(null)).toBe('—')
  expect(fmtP(0)).toBe('<0.01')
  expect(fmtP(0.035)).toBe('0.04')
  expect(fmtP(undefined)).toBe('—')
  expect(md('2026-11-05')).toBe('11-05')
  expect(md(null)).toBe('—')
})

it('回放成交不足 5 笔 (thin) 不算完整回放', () => {
  expect(hasFullReplay({ n: 503, fill: 0.62, mean_r: 0.2, excess: 0.01, p: 0.46 })).toBe(true)
  expect(hasFullReplay({ n: 2, signals: 3, raw: 201, thin: true })).toBe(false)
  expect(hasFullReplay(null)).toBe(false)
})

it('机会板现状: 持仓优先于不下单, 入场区内与挂单分开', () => {
  expect(boardLabel({ state: 'holding', skip: false })).toBe('持仓')
  expect(boardLabel({ state: 'holding', skip: true })).toBe('持仓')
  expect(boardLabel({ state: 'pending', skip: true })).toBe('不下单')
  expect(boardLabel({ state: 'in_zone', skip: false })).toBe('入场区内')
  expect(boardLabel({ state: 'pending', skip: false })).toBe('挂单')
})

it('平仓原因不露英文码, 不认识的写「其他」', () => {
  expect(whyLabel('stop')).toBe('止损')
  expect(whyLabel('target')).toBe('目标')
  expect(whyLabel('time')).toBe('到期')
  expect(whyLabel('gap_void')).toBe('其他')
  expect(whyLabel(null)).toBe('其他')
})

it('模拟成绩的可信度: 没成交 / 样本少 (没有区间) / 背离 / 正常', () => {
  expect(paperState(null)).toBe('none')
  expect(paperState(paper({ fills: 0 }))).toBe('none')
  expect(paperState(paper())).toBe('thin')
  expect(paperState(paper({ ci: [0.1, 0.9] }))).toBe('ok')
  expect(paperState(paper({ ci: [-1.2, -0.4], diverged: true }))).toBe('diverged')
})

it('累计曲线: 一笔成交一个点, 回放期望 = 笔数 × 回放每笔 R; 没有回放不给期望', () => {
  const trades = [
    { symbol: 'BABA', signal_day: '2026-10-20', fill_date: '2026-10-21', exit_date: '2026-10-22', why: 'stop', r: -2.42 },
    { symbol: 'SMCI', signal_day: '2026-10-30', fill_date: '2026-10-31', exit_date: '2026-11-03', why: 'stop', r: -0.95 },
  ]
  expect(curvePoints(trades, 0.3094)).toEqual([
    { d: '2026-10-22', symbol: 'BABA', r: -2.42, cum: -2.42, exp: 0.309 },
    { d: '2026-11-03', symbol: 'SMCI', r: -0.95, cum: -3.37, exp: 0.619 },
  ])
  expect(curvePoints(trades, null).map(p => p.exp)).toEqual([null, null])
  expect(curvePoints([], 0.3)).toEqual([])
})

it('曲线按平仓日画: 同一天的几笔并成一个点, 累计取当日收尾值 (同一横坐标不来回画竖线)', () => {
  const mk = (symbol: string, exit_date: string, r: number) => ({ symbol, signal_day: null, fill_date: null, exit_date, why: 'stop', r })
  const pts = curvePoints([mk('A', '2026-11-09', 0.5), mk('HIMS', '2026-11-10', 2.55), mk('MU', '2026-11-10', -0.66)], 0.1)
  expect(dayPoints(pts)).toEqual([
    { d: '2026-11-09', n: 1, r: 0.5, symbol: 'A', cum: 0.5, exp: 0.1 },
    { d: '2026-11-10', n: 2, r: 1.89, symbol: null, cum: 2.39, exp: 0.3 },
  ])
  expect(dayPoints([])).toEqual([])
})

it('一个策略一行: 多空命中相加, 超额与模拟取默认那一面, 任一面背离都标出', () => {
  const e = entry(
    { hits: ['TSLA'], replay: { n: 539, fill: 0.62, mean_r: 0.04, excess: -0.1, p: 0.97 } },
    { selected: true, hits: ['MU', 'AMD'], replay: { n: 164, fill: 0.65, mean_r: 0.25, excess: 0.25, p: 0 }, paper: paper({ mean_r: 1.06 }) },
  )
  const row = buildRailRow({ id: 'tv_halftrend', name: 'HalfTrend' }, e, { inPool: true, monitored: false })
  expect(row).toMatchObject({ group: 'selected', selected: ['short'], hits: 3, excess: 0.25, paperMean: 1.06, diverged: false })

  const div = buildRailRow({ id: 'x', name: 'X' }, entry({ paper: paper({ diverged: true }) }), { inPool: false, monitored: false })
  expect(div.diverged).toBe(true)

  // 面板还没到 / 读不到: 只按策略池与监控分组, 数字留空
  const bare = buildRailRow({ id: 'y', name: 'Y' }, undefined, { inPool: false, monitored: true })
  expect(bare).toMatchObject({ group: 'active', hits: 0, excess: null, paperMean: null })
  expect(buildRailRow({ id: 'z', name: 'Z' }, undefined, { inPool: false, monitored: false }).group).toBe('idle')
})

it('样本少的回放与没有成交的模拟不进行内数字', () => {
  const e = entry({ replay: { n: 2, thin: true }, paper: paper({ fills: 0, mean_r: null }) })
  expect(buildRailRow({ id: 'a', name: 'A' }, e, { inPool: true, monitored: false })).toMatchObject({ excess: null, paperMean: null })
})

it('列表顺序: 入选 → 扫描 / 监控 → 未启用; 组内超额从高到低, 没有回放的排后', () => {
  const mk = (id: string, group: 'selected' | 'active' | 'idle', excess: number | null) =>
    ({ id, name: id, group, selected: [], inPool: false, monitored: false, hits: 0, excess, paperMean: null, diverged: false })
  const rows = sortRailRows([mk('d', 'idle', 0.9), mk('b', 'active', null), mk('a', 'selected', -0.1), mk('c', 'selected', 0.3), mk('e', 'active', 0.1)])
  expect(rows.map(r => r.id)).toEqual(['c', 'a', 'e', 'b', 'd'])
  expect(filterRailRows(rows, 'selected').map(r => r.id)).toEqual(['c', 'a'])
  expect(filterRailRows(rows, 'active').map(r => r.id)).toEqual(['c', 'a', 'e', 'b'])
  expect(filterRailRows(rows, 'all')).toHaveLength(5)
})
