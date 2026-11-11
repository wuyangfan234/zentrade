// @vitest-environment jsdom
// 策略页详情台: 一个策略一行的列表 + 单策略详情 (只读)。
// 守的口径: 入选策略只显示不修改 (要改去机会页规则设置); 多空两面分开看; 模拟成绩样本少 / 背离有标记;
// 稀疏数据 (没有成交、没有回放、没有信号) 都有明确空状态, 不留空白。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import type { StrategyDetail as StrategyMeta, StrategyPanelEntry, StrategyPanelPayload, StrategyPanelSide } from '@/lib/api'
import { buildRailRow, sortRailRows } from '@/lib/strategyPanel'
import { StrategyDetail } from './StrategyDetail'
import { StrategyRail } from './StrategyRail'

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

function render(node: React.ReactNode) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => { root!.render(<MemoryRouter>{node}</MemoryRouter>) })
}
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})
const q = (sel: string) => container.querySelector(sel)
const qa = (sel: string) => [...container.querySelectorAll(sel)]
const click = (el: Element | null) => act(() => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })

const meta = (over: Partial<StrategyMeta> = {}): StrategyMeta => ({
  id: 'tv_halftrend', name: 'HalfTrend 翻多', description: 'HalfTrend 移植', tags: [], source: 'builtin',
  execution_backend: 'matrix_native', asset_types: ['stock'], timeframes: ['1d'], version: '1.0.0', basic_filter: {},
  params: [
    { id: 'amplitude', label: '振幅', type: 'int', default: 2 },
    { id: 'use_vol', label: '启用量能过滤', type: 'bool', default: false },
  ],
  params_defaults: { amplitude: 3, use_vol: false }, scoring: {}, scoring_directions: {}, entry_signals: [], exit_signals: [],
  minute_exit_trigger_supported_signals: [], stop_loss: null, take_profit: null, trailing_stop: null,
  trailing_take_profit_activate: null, trailing_take_profit_drawdown: null, max_hold_days: null, order_by: 'score',
  descending: true, limit: 100, ...over,
})
const side = (over: Partial<StrategyPanelSide> = {}): StrategyPanelSide => ({
  selected: false, replay: null, hits: [], hist: [], board: [], paper: null, ...over,
})
const trades = Array.from({ length: 10 }, (_, i) => ({
  symbol: `S${i}`, signal_day: '2026-10-20', fill_date: '2026-10-21', exit_date: `2026-10-${String(22 + i).padStart(2, '0')}`,
  why: i % 2 ? 'target' : 'stop', r: i % 2 ? 2.43 : -1,
}))
const entry: StrategyPanelEntry = {
  short_capable: true, name_short: 'HalfTrend 翻空',
  sides: {
    long: side({ replay: { n: 539, fill: 0.624, mean_r: 0.0403, excess: -0.1034, p: 0.972 }, hits: ['TSLA'], hist: [0, 2, 1] }),
    short: side({
      selected: true, replay: { n: 164, fill: 0.648, mean_r: 0.2482, excess: 0.2494, p: 0 },
      hits: ['MU', 'AXTI'], hist: [3, 0, 2],
      board: [
        { symbol: 'AXTI', state: 'holding', skip: false, signal_day: '2026-11-05', r_now: 0.12, card_id: 'c1' },
        { symbol: 'NVDA', state: 'pending', skip: true, signal_day: '2026-11-09', r_now: null, card_id: 'c2' },
      ],
      paper: { cards: 15, fills: 10, days: 6, fill_rate: 0.667, sum_r: 7.15, mean_r: 0.72, win: 5, loss: 5, ci: null,
        diverged: false, replay_mean_r: 0.2482, replay_fill: 0.648, trades },
    }),
  },
}
const panel = (over: Partial<StrategyPanelPayload> = {}): StrategyPanelPayload => ({
  contract: 'strategy-panel-v1', generated_at: '2026-11-11T00:00:00-05:00',
  selected: { long: [], short: ['tv_halftrend'] },
  replay_meta: { window: ['2023-01-03', '2026-10-30'], universe: 'watchlist 46', data_end: '2026-11-09', generated: null },
  signals: { day: '2026-11-10', requested: null, days: ['2026-11-06', '2026-11-09', '2026-11-10'], universe: 46 },
  paper_since: '2026-10-15', strategies: { tv_halftrend: entry }, errors: {}, ...over,
})

function renderDetail(over: Partial<React.ComponentProps<typeof StrategyDetail>> = {}) {
  const props = {
    strategy: meta(), entry, panel: panel(), panelLoading: false, side: 'short' as const, onSideChange: vi.fn(),
    inPool: false, onTogglePool: vi.fn(), monitored: false, onToggleMonitor: vi.fn(), onSettings: vi.fn(), onPreview: vi.fn(),
    ...over,
  }
  render(<StrategyDetail {...props} />)
  return props
}

it('做空那一面: 四格指标、入选标签、做空名、样本少标记', () => {
  renderDetail()
  // 看做空那一面: 标题用做空名, 原名跟在后面 (不让「…翻多」配着「入选 · 空」)
  expect(q('[data-detail-name]')!.textContent).toBe('HalfTrend 翻空')
  expect(q('[data-detail-base]')!.textContent).toBe('HalfTrend 翻多')
  expect(q('[data-detail-selected]')!.textContent).toBe('入选 · 空')
  expect(q('[data-kpi="replay-mean"]')!.textContent).toContain('+0.25R')
  expect(q('[data-kpi="replay-mean"]')!.textContent).toContain('164 笔 · 成交率 65%')
  expect(q('[data-kpi="replay-excess"]')!.textContent).toContain('p <0.01 · 对比随机基准')
  expect(q('[data-kpi="paper-mean"]')!.textContent).toContain('+0.72R')
  expect(q('[data-kpi="paper-mean"]')!.textContent).toContain('10 笔 · 胜 5 负 5')
  expect(q('[data-kpi="paper-fill"]')!.textContent).toContain('67%')
  expect(q('[data-kpi="paper-fill"]')!.textContent).toContain('机会 15 · 回放 65%')
  expect(q('[data-paper-state="thin"]')!.textContent).toBe('样本少')    // 没有区间 = 样本少
  expect(q('[data-paper-ci]')).toBeNull()
})

it('做多那一面用做多的回放与命中, 没有模拟成交时给空状态而不是空白', () => {
  renderDetail({ side: 'long' })
  expect(q('[data-detail-name]')!.textContent).toBe('HalfTrend 翻多')
  expect(q('[data-detail-base]')).toBeNull()
  expect(q('[data-kpi="replay-mean"]')!.textContent).toContain('+0.04R')
  expect(q('[data-kpi="replay-excess"]')!.textContent).toContain('-0.10R')
  expect(q('[data-kpi="paper-mean"]')!.textContent).toContain('暂无成交')
  expect(q('[data-curve-empty]')!.textContent).toBe('暂无成交')
  expect(q('[data-trades-empty]')!.textContent).toBe('暂无成交')
  expect(qa('[data-hits] [data-chip]').map(e => e.getAttribute('data-chip'))).toEqual(['TSLA'])
  expect(q('[data-board]')).toBeNull()
})

it('方向切换只在可做空的策略上出现, 点击回调目标方向', () => {
  const props = renderDetail()
  expect(q('[data-side="short"]')!.getAttribute('aria-selected')).toBe('true')
  click(q('[data-side="long"]'))
  expect(props.onSideChange).toHaveBeenCalledWith('long')
})

it('不可做空的策略没有方向切换', () => {
  renderDetail({ entry: { short_capable: false, name_short: null, sides: { long: entry.sides.long } }, side: 'long' })
  expect(q('[data-side]')).toBeNull()
})

it('入选策略只读: 没有任何修改入选的控件, 只有跳到机会页规则设置的链接', () => {
  renderDetail()
  const link = q('[data-detail-rules]') as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/?rules=1')
  expect(qa('[role="switch"]').map(e => e.getAttribute('data-switch'))).toEqual(['pool', 'monitor'])
  expect(container.querySelector('input[type="checkbox"]')).toBeNull()
})

it('全市场扫描 / 监控是开关, 参数设置是按钮, 各自回调', () => {
  const props = renderDetail({ inPool: true })
  expect(q('[data-switch="pool"]')!.getAttribute('aria-checked')).toBe('true')
  expect(q('[data-switch="monitor"]')!.getAttribute('aria-checked')).toBe('false')
  click(q('[data-switch="pool"]'))
  click(q('[data-switch="monitor"]'))
  click(q('[data-detail-settings]'))
  expect(props.onTogglePool).toHaveBeenCalledTimes(1)
  expect(props.onToggleMonitor).toHaveBeenCalledTimes(1)
  expect(props.onSettings).toHaveBeenCalledTimes(1)
})

it('自选命中: 命中的代码带机会板现状; 不在当日命中里的机会板卡另列一行', () => {
  const props = renderDetail()
  expect(q('[data-signal-day]')!.textContent).toBe('11-10')
  expect(q('[data-hits]')!.textContent).toContain('自选 2')
  expect(q('[data-hits] [data-chip="AXTI"]')!.textContent).toBe('AXTI持仓')
  expect(q('[data-hits] [data-chip="MU"]')!.textContent).toBe('MU')
  expect(q('[data-board]')!.textContent).toContain('机会板 1')
  expect(q('[data-board] [data-chip="NVDA"]')!.textContent).toBe('NVDA不下单')
  expect(qa('[data-hist-bars] i')).toHaveLength(3)
  click(q('[data-hits] [data-chip="MU"]'))
  expect(props.onPreview).toHaveBeenCalledWith('MU')
})

it('模拟成交: 新的在前, 默认 6 行, 计数写清「1–6 / 10」, 可展开全部; 平仓原因不露英文码', () => {
  renderDetail()
  const rows = () => qa('[data-trades] tbody tr')
  expect(rows()).toHaveLength(6)
  expect(rows()[0].textContent).toContain('S9')
  expect(rows()[0].textContent).toContain('目标')
  expect(rows()[1].textContent).toContain('止损')
  expect(q('[data-trades-count]')!.textContent).toBe('1–6 / 10')
  click(q('[data-trades-toggle]'))
  expect(rows()).toHaveLength(10)
  expect(q('[data-trades-count]')!.textContent).toBe('1–10 / 10')
  expect(container.textContent).not.toMatch(/\bstop\b|\btarget\b/)
})

it('累计 R 曲线: 一笔一个点, 只有一个高亮点, 终值与回放期望都有标注, 读数行在图上方', () => {
  renderDetail()
  expect(q('[data-curve-line]')).not.toBeNull()
  expect(q('[data-curve-exp]')).not.toBeNull()
  expect(qa('[data-curve-dot]')).toHaveLength(1)
  expect(qa('[data-curve-pt]')).toHaveLength(9)
  expect(q('[data-curve-end="cum"]')!.textContent).toBe('+7.2R')
  expect(q('[data-curve-end="exp"]')!.textContent).toBe('+2.5R')
  const read = q('[data-curve-read]')!.textContent!
  expect(read).toContain('截至 10-31')
  expect(read).toContain('模拟累计 +7.2R')
  expect(read).toContain('回放期望 +2.5R')
  // 横轴从模拟起始日画起, 末端是最后一笔的平仓日 (不偏一天)
  const xs = qa('[data-curve-x]').map(e => e.textContent)
  expect(xs[0]).toBe('10-15')
  expect(xs[xs.length - 1]).toBe('10-31')
})

it('背离与区间: 有区间时写出区间, 区间不含回放值标背离', () => {
  const diverged: StrategyPanelEntry = {
    ...entry,
    sides: { ...entry.sides, short: { ...entry.sides.short!, paper: { ...entry.sides.short!.paper!, ci: [-1.2, -0.4], diverged: true } } },
  }
  renderDetail({ entry: diverged })
  expect(q('[data-paper-state="diverged"]')!.textContent).toBe('背离')
  expect(q('[data-paper-ci]')!.textContent).toBe('每笔均值 95% 区间 -1.20R ~ -0.40R')
})

it('参数显示现值 (含已保存的覆盖), 布尔写开 / 关', () => {
  renderDetail()
  const text = q('[data-params]')!.textContent!
  expect(text).toContain('振幅3')
  expect(text).toContain('启用量能过滤关')
})

it('某一段读失败时写明是哪一段; 面板没到时不崩', () => {
  renderDetail({ panel: panel({ errors: { board: 'RuntimeError', paper: 'OSError' } }) })
  expect(q('[data-panel-errors]')!.textContent).toBe('读取失败 · 机会板 / 模拟成绩')
  act(() => { root!.unmount() }); root = null
  container.innerHTML = ''
  renderDetail({ entry: null, panel: null, panelLoading: true, side: 'long' })
  expect(q('[data-kpi="replay-mean"]')!.textContent).toContain('加载中')
  expect(q('[data-signal-day]')!.textContent).toBe('暂无信号')
})

it('样本少的回放只给笔数, 不给每笔 R 与超额', () => {
  const thin: StrategyPanelEntry = { short_capable: false, name_short: null, sides: { long: side({ replay: { n: 2, signals: 3, raw: 201, thin: true } }) } }
  renderDetail({ entry: thin, side: 'long' })
  expect(q('[data-kpi="replay-mean"]')!.textContent).toContain('样本少 · 2 笔')
  expect(q('[data-kpi="replay-mean"]')!.textContent).not.toContain('R')
})

// ── 列表 ──────────────────────────────────────────────────────

function renderRail(over: Partial<React.ComponentProps<typeof StrategyRail>> = {}) {
  const rows = sortRailRows([
    buildRailRow({ id: 'tv_halftrend', name: 'HalfTrend 翻多' }, entry, { inPool: false, monitored: false }),
    buildRailRow({ id: 'boll_breakout', name: '布林突破' }, { short_capable: false, name_short: null, sides: { long: side({ hits: ['A', 'B'] }) } }, { inPool: true, monitored: false }),
    buildRailRow({ id: 'macd_golden', name: 'MACD 金叉放量' }, undefined, { inPool: false, monitored: false }),
  ])
  const props = {
    rows, filter: 'all' as const, counts: { selected: 1, active: 2, all: 3 }, onFilter: vi.fn(), activeId: 'tv_halftrend',
    onSelect: vi.fn(), marketHits: { boll_breakout: 24 } as Record<string, number | undefined>, computing: new Set<string>(), ...over,
  }
  render(<StrategyRail {...props} />)
  return props
}

it('列表一个策略一行 (可做空的策略不拆成两行), 按 入选 → 扫描 / 监控 → 未启用 分组', () => {
  renderRail()
  expect(qa('[data-rail-item]').map(e => e.getAttribute('data-rail-item'))).toEqual(['tv_halftrend', 'boll_breakout', 'macd_golden'])
  expect(qa('[data-rail-group]').map(e => e.getAttribute('data-rail-group'))).toEqual(['selected', 'active', 'idle'])
  const first = q('[data-rail-item="tv_halftrend"]')!
  expect(first.getAttribute('aria-current')).toBe('true')
  expect(first.querySelector('[data-rail-selected]')!.textContent).toBe('入选 · 空')
  expect(first.textContent).toContain('自选3')                         // 多空命中相加
  expect(first.textContent).toContain('超额+0.25R')                    // 入选的那一面
  expect(first.textContent).toContain('模拟+0.72R')
  expect(first.textContent).toContain('全市场—')                       // 没扫描过
  expect(q('[data-rail-item="boll_breakout"]')!.textContent).toContain('全市场24')
})

it('列表范围切换与点选回调; 计数写在页签上', () => {
  const props = renderRail()
  expect(q('[data-rail-filter="selected"]')!.textContent).toBe('入选1')
  expect(q('[data-rail-filter="active"]')!.textContent).toBe('已启用2')
  expect(q('[data-rail-filter="all"]')!.getAttribute('aria-selected')).toBe('true')
  click(q('[data-rail-filter="active"]'))
  expect(props.onFilter).toHaveBeenCalledWith('active')
  click(q('[data-rail-item="macd_golden"]'))
  expect(props.onSelect).toHaveBeenCalledWith('macd_golden')
})

it('列表为空与扫描中的占位', () => {
  renderRail({ rows: [], counts: { selected: 0, active: 0, all: 0 } })
  expect(q('[data-rail-empty]')!.textContent).toBe('暂无策略')
  act(() => { root!.unmount() }); root = null
  container.innerHTML = ''
  renderRail({ computing: new Set(['boll_breakout']) })
  expect(q('[data-rail-item="boll_breakout"] .animate-pulse')).not.toBeNull()
})
