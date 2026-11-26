// @vitest-environment jsdom
/** Todo #10 P1 UI: regime 状态条 / 策略账本 / 卡时效 + 逐条归因 / 观察级降级 / 区间条 / 理由行 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const toasts = vi.hoisted(() => ({ calls: [] as unknown[][] }))
vi.mock('@/components/Toast', () => ({ toast: (...a: unknown[]) => { toasts.calls.push(a) } }))

import { ciScale, FROZEN_UNKNOWN, fR, isObserve, ledgerRows, nextRejudge, parseReason, regimeFp, regimeOf, tierReason, anchorText, gateText } from './p1'
import { RegimeBar } from './RegimeBar'
import { LedgerBlock } from './LedgerBlock'
import { CiBar, CI_MIN_PX } from './CiBar'
import { BoardCards, FillGrid, GRID, lastRowLayout } from './Cards'
import { ReasonLine, ScoringLoop, challengerReason, ledgerSrc, type ScoreUse } from '@/pages/factors/FactorSlots'
import { CARDS, LEDGER, REGIME, REGIME_CALM, swingWith } from './__fixtures__/p1'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); toasts.calls = [] })
afterEach(() => { act(() => root.unmount()); host.remove() })
const render = async (el: React.ReactNode) => { await act(async () => { root.render(el) }) }
const $$ = (s: string) => [...host.querySelectorAll(s)]

it('regime 依据: 字符串还原成 读数 / 门槛 / 是否满足 (门槛 = regime.py 常量)', () => {
  expect(parseReason('自选宽度 61%')).toMatchObject({ value: '61%', ok: true })
  expect(parseReason('自选宽度 48%')).toMatchObject({ ok: false })
  expect(parseReason('波动比5/60 1.62')).toMatchObject({ value: '1.62', ok: false })
  expect(parseReason('SPY > MA50').ok).toBe(true)
  expect(parseReason('跌破前20日低').ok).toBe(false)
  expect(parseReason('某新依据')).toMatchObject({ label: '某新依据', ok: null })
})

it('下次重判: 交易日取下一个整点槽位, 15:00 后写盘后慢层, 非交易日写下一交易日', () => {
  expect(nextRejudge('2026-11-25T10:24:00', true)).toBe('11:00 ET 整点快层')
  expect(nextRejudge('2026-11-25T09:10:00', true)).toBe('09:25 ET 整点快层')
  expect(nextRejudge('2026-11-25T15:30:00', true)).toBe('今日盘后扫描 · 慢层')
  expect(nextRejudge('2026-11-29T10:00:00', false)).toBe('下一交易日 09:25 ET 快层')
})

it('状态条: 三态有图标 + 文字 + 当前段 aria-current; 依据逐项; 盘中降级; 指纹; 无 regime 降级', async () => {
  await render(<RegimeBar regime={REGIME} clockEt="2026-11-25T11:20:00" tradingDay />)
  const bar = host.querySelector('[data-regime-bar]')!
  expect(bar.getAttribute('data-regime-state')).toBe('防守')
  expect(host.querySelector('[data-regime-chip]')!.textContent).toBe('防守')
  expect(host.querySelector('[data-regime-chip] svg')).not.toBeNull()
  expect(host.querySelector('[aria-current="true"]')!.textContent).toBe('防守')
  expect($$('[data-regime-reason]')).toHaveLength(3)
  expect($$('[data-regime-reason="no"]')).toHaveLength(3)
  expect(host.querySelector('[data-regime-down]')!.textContent).toContain('快照为震荡')
  expect(host.querySelector('[data-regime-next]')!.textContent).toBe('下次重判 12:00 ET 整点快层')
  expect(bar.getAttribute('data-regime-fp')).toBe(regimeFp(REGIME))
  await render(<RegimeBar regime={null} />)
  expect(host.querySelector('[data-regime-missing]')!.textContent).toContain('未提供')
  expect(regimeOf(swingWith([], {}))).toBeNull()
  expect(regimeOf(swingWith([], { regime: { state: '乱写' } }))).toBeNull()
})

it('账本: 五个档位分组, 计数 = 行数; 待定 / 冷冻带图标 + 「不出卡」文字; 区间缺写「区间未返回」', async () => {
  await render(<LedgerBlock swing={swingWith([])} names={new Map([['tv_halftrend', '半趋势翻空']])} />)
  // 期望值从夹具独立数出来, 再与渲染出的 DOM 行数、页面上的计数文字三方对比 (不拿同一个数组自比)
  const want = new Map<string, number>()
  for (const c of Object.values(LEDGER.cells)) want.set(String(c.tier), (want.get(String(c.tier)) ?? 0) + 1)
  const rows = $$('[data-ledger-row]')
  expect(rows).toHaveLength(7)
  expect(host.querySelector('[data-ledger-count]')!.textContent).toBe('7')
  expect([...want.values()].reduce((a, b) => a + b, 0)).toBe(7)
  for (const [t, n] of want) {
    const g = host.querySelector(`[data-ledger-group="${t}"]`)!
    expect(g.querySelectorAll(`tr[data-ledger-row][data-tier="${t}"]`)).toHaveLength(n)
    expect(host.querySelector(`[data-ledger-tier-count="${t}"]`)!.textContent!.trim().endsWith(`${t} ${n}`)).toBe(true)
  }
  expect($$('[data-ledger-group]').map((g) => g.getAttribute('data-ledger-group'))).toEqual(['在用', '观察', '待定', '冷冻', '复活'])
  const frozen = host.querySelector('[data-ledger-row="volume_price_surge|long"]')!
  expect(frozen.textContent).toContain('❄冷冻·不出卡')
  expect(host.querySelector('[data-ledger-row="tv_halftrend|short"]')!.textContent).toContain('半趋势翻空')
  expect(frozen.querySelector('[data-ci-bar]')!.getAttribute('aria-label')).toBe('区间未返回')
})

it('账本向前兼容: cells 带 ci_low / ci_high → 画区间条 (同一刻度); 缺值 → 区间未返回, 条宽不变', async () => {
  const cells = { 'tv_halftrend|short': { n: 164, excess: 0.249, tier: '在用', ci_low: 0.147, ci_high: 0.352 },
    'tv_chandelier_exit|short': { n: 210, excess: 0.031, tier: '观察', ci_low: -0.05, ci_high: 0.11 },
    'macd_golden|long': { n: 388, excess: -0.112, tier: '待定' } }
  await render(<LedgerBlock swing={swingWith([], { ledger: { ...LEDGER, cells } })} names={new Map()} />)
  const bar = (k: string) => host.querySelector(`[data-ledger-row="${k}"] [data-ci-bar]`)!
  expect(bar('tv_halftrend|short').getAttribute('data-ci-cross')).toBe('0')
  expect(bar('tv_halftrend|short').getAttribute('aria-label')).toBe('95% 区间 +0.15 ~ +0.35')
  expect(bar('tv_chandelier_exit|short').getAttribute('data-ci-cross')).toBe('1')
  expect(bar('macd_golden|long').getAttribute('aria-label')).toBe('区间未返回')
  expect(bar('macd_golden|long').getAttribute('width')).toBe(bar('tv_halftrend|short').getAttribute('width'))
  // 同一刻度: 三条的 0 基准线横坐标相同
  const zx = $$('[data-ci-zero]').map((l) => l.getAttribute('x1'))
  expect(new Set(zx).size).toBe(1)
  expect(ledgerRows({ status: 'ok', cells }, new Map()).find((r) => r.rule === 'tv_halftrend')).toMatchObject({ lo: 0.147, hi: 0.352 })
})

it('账本: unavailable / disabled 有明示; 无切片写未返回', async () => {
  await render(<LedgerBlock swing={swingWith([], { ledger: { status: 'unavailable', cells: {} } })} names={new Map()} />)
  expect(host.querySelector('[data-ledger-status="unavailable"]')!.textContent).toContain('不出卡')
  await render(<LedgerBlock swing={swingWith([], {})} names={new Map()} />)
  expect(host.querySelector('[data-ledger-empty]')!.textContent).toContain('未返回')
  expect(ledgerRows(null, new Map())).toEqual([])
})

it('区间条: 0 基准线; 跨 0 标记; 极窄区间也有最小像素; 端点数字', async () => {
  const ends = [0.14, 0.05, 0.23, 0.11, 0.02, 0.2]
  const { min, max } = ciScale(ends)
  expect(min).toBeLessThan(0)
  expect(max).toBeLessThan(0.3)
  // 所有端点 (含 0) 都在轴内, 且两侧留白不超过跨度的 15%
  expect(min).toBeLessThanOrEqual(Math.min(0, ...ends))
  expect(max).toBeGreaterThanOrEqual(Math.max(...ends))
  expect(max - Math.max(...ends)).toBeLessThanOrEqual((max - min) * 0.15)
  const neg = ciScale([-0.2, -0.02, 0.36, -0.11])
  expect(neg.min).toBeLessThanOrEqual(-0.2)
  expect(neg.max).toBeGreaterThanOrEqual(0.36)
  await render(<><CiBar v={0.14} lo={0.05} hi={0.23} min={min} max={max} /><CiBar v={0.01} lo={0.0099} hi={0.0101} min={-1} max={1} /><CiBar v={0.03} lo={-0.09} hi={0.15} min={min} max={max} /></>)
  const bars = $$('[data-ci-bar]')
  expect(bars.every((b) => b.querySelector('[data-ci-zero]'))).toBe(true)
  expect(bars.map((b) => b.getAttribute('data-ci-cross'))).toEqual(['0', '0', '1'])
  expect(Number(bars[1].getAttribute('data-ci-w'))).toBeGreaterThanOrEqual(CI_MIN_PX)
  expect(bars[0].querySelectorAll('[data-ci-label]')).toHaveLength(2)
  expect(bars[0].textContent).toContain('+0.05')
})

it('理由: 留用 / 换下带数字; 换下行加底 + 加粗 + 触发条件高亮', async () => {
  expect(tierReason('在用', { n: 214, excess: 0.14, lo: 0.05, hi: 0.23 }).parts.map((p) => p.text)).toEqual(['区间下限 +0.05R > 0', '样本 214 ≥ 30'])
  const fz = tierReason('冷冻', { n: 188, excess: -0.11, lo: -0.2, hi: -0.02 })
  expect(fz.word).toBe('换下')
  expect(fz.parts[0]).toEqual({ text: '区间上限 −0.02R < 0', hot: true })
  expect(tierReason('待定', { n: 29, excess: 0.08, lo: null, hi: null }).parts[0]).toEqual({ text: '样本 29 < 30', hot: true })
  expect(challengerReason({ n: 58, excess: 0.17, lo: -0.02, hi: 0.36, passed: false, note: null }).parts[0]).toEqual({ text: '区间 −0.02R ~ +0.36R 跨 0', hot: true })
  await render(<ReasonLine r={fz} />)
  const el = host.querySelector('[data-reason="换下"]')!
  expect(el.className).toContain('bg-warning/10')
  expect(el.className).toContain('font-semibold')
  expect(el.querySelector('mark[data-reason-hot]')!.textContent).toBe('区间上限 −0.02R < 0')
  expect(fR(-0.02)).toBe('−0.02R')
})

it('卡: T0 跨日有效; 观察级 / T1 单独成组 + 虚线 + 徽章; T1 写锚定价与来源; 归因逐条; 观察级下单提示', async () => {
  const sw = swingWith([CARDS.t0, CARDS.t0b, CARDS.observe, CARDS.t1])
  await render(<BoardCards swing={sw} quotes={{}} quotesAt={null} model={new Map()} ctx={{}} nav={100000} onOpen={() => {}} names={new Map([['tv_halftrend', '半趋势翻空']])} />)
  expect($$('[data-board-grid] [data-board-card]')).toHaveLength(2)
  expect($$('[data-observe-grid] [data-board-card]')).toHaveLength(2)
  expect(host.querySelector('[data-observe-count]')!.textContent).toBe('观察级 2')
  expect(host.querySelector('[data-board-card="NVDA"] [data-timing="T0"]')!.textContent).toBe('跨日有效')
  const meta = host.querySelector('[data-board-card="META"]')!
  expect(meta.className).toContain('border-dashed')
  expect(meta.querySelector('[data-timing="T1"]')!.textContent).toContain('盘中切换')
  expect(meta.querySelector('[data-anchor]')!.textContent).toBe('锚定 591.37 · 实时报价 11:02')
  expect(meta.querySelector('[data-observe-badge]')!.textContent).toBe('观察级·不进纸面下单')
  expect(host.querySelector('[data-board-card="TSLA"] [data-observe-badge]')!.textContent).toBe('观察级')
  expect(host.querySelector('[data-board-skips]')).toBeNull()
  // 归因展开: 逐条规则 + 合计 + k + 状态 + 门
  await act(async () => { (host.querySelector('[data-attr-toggle="AMD"]') as HTMLElement).click() })
  const panel = host.querySelector('[data-attr-panel="AMD"]')!
  expect(panel.querySelectorAll('[data-attr-rule]')).toHaveLength(2)
  expect(panel.querySelector('[data-attr-rule="tv_halftrend"]')!.textContent).toContain('半趋势翻空')
  expect(panel.querySelector('[data-attr-total]')!.textContent).toContain('合计 +0.24R · k = 30')
  expect(panel.textContent).toContain('市场状态 防守')
  expect(panel.textContent).toContain('multi_scale_v1 · 4h 窗口条件齐备 · tight-v1')
  // 观察级下单: 提示, 不打开预览
  const opened: string[] = []
  await render(<BoardCards swing={sw} quotes={{}} quotesAt={null} model={new Map()} ctx={{}} nav={null} onOpen={(s) => opened.push(s)} />)
  await act(async () => { (host.querySelector('[data-observe-order="META"]') as HTMLElement).click() })
  expect(toasts.calls[0][0]).toBe('META · 观察级不进纸面下单簿')
  expect(opened).toEqual([])
})

it('末行补齐: 3 张卡 2 列 → 末张横跨 2 列; 4 张卡 2 列 → 不动; 无布局 (jsdom) → 不动', async () => {
  const real = window.getComputedStyle
  const spy = vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => (el.hasAttribute('data-grid-fill')
    ? ({ gridTemplateColumns: '300px 300px 0px' } as CSSStyleDeclaration) : real(el)))
  await render(<FillGrid className="grid">{['a', 'b', 'c'].map((k) => <div key={k} data-k={k} />)}</FillGrid>)
  expect((host.querySelector('[data-k="c"]') as HTMLElement).style.gridColumn).toBe('span 2')
  expect(host.querySelector('[data-grid-fill]')!.getAttribute('data-grid-cols')).toBe('2')
  await render(<FillGrid className="grid">{['a', 'b', 'c', 'd'].map((k) => <div key={k} data-k={k} />)}</FillGrid>)
  expect((host.querySelector('[data-k="d"]') as HTMLElement).style.gridColumn).toBe('')
  spy.mockRestore()
  await render(<FillGrid className="grid">{['a', 'b', 'c'].map((k) => <div key={k} data-k={k} />)}</FillGrid>)
  expect((host.querySelector('[data-k="c"]') as HTMLElement).style.gridColumn).toBe('')
})

it('理由: 待定 = 待定 (不写换下), 冷冻 = 换下; 都按不出卡加重', async () => {
  const pd = tierReason('待定', { n: 29, excess: 0.08, lo: null, hi: null })
  expect(pd).toMatchObject({ word: '待定', off: true })
  await render(<ReasonLine r={pd} />)
  expect(host.querySelector('[data-reason="待定"]')!.textContent).toContain('待定·不出卡')
  expect(host.querySelector('mark[data-reason-hot]')!.className).toContain('underline')
})

it('冷冻 + 无区间数据: 不断言具体触发条件 (不出现「连续 2 次」); 带区间且上限 < 0 才指名', async () => {
  const fz = tierReason('冷冻', { n: 410, excess: -0.203, lo: null, hi: null })
  expect(fz.parts[0]).toEqual({ text: FROZEN_UNKNOWN, hot: true })
  await render(<ReasonLine r={fz} />)
  expect(host.textContent).not.toContain('连续 2 次')
  expect(host.textContent).toContain('触发条件未返回')
  // 有区间但上限 ≥ 0 (冷冻来自复核连负或延续上月): 仍不指名
  expect(tierReason('冷冻', { n: 410, excess: -0.05, lo: -0.12, hi: 0.02 }).parts[0].text).toBe(FROZEN_UNKNOWN)
  expect(tierReason('冷冻', { n: 188, excess: -0.11, lo: -0.2, hi: -0.02 }).parts[0]).toEqual({ text: '区间上限 −0.02R < 0', hot: true })
  // 机会页账本表行里也不出现
  await render(<LedgerBlock swing={swingWith([])} names={new Map()} />)
  expect(host.textContent).not.toContain('连续 2 次')
})

it('样本 n < 30 不写「≥ 30」; n ≥ 30 才写; 理由出处标账本·regime 格', () => {
  const t = (n: number) => tierReason('在用', { n, excess: 0.1, lo: null, hi: null }).parts.map((p) => p.text)
  expect(t(12)).toEqual(['超额 +0.10R', '样本 12'])
  expect(t(12).join(' ')).not.toContain('≥ 30')
  expect(t(40)).toContain('样本 40 ≥ 30')
  expect(tierReason('复活', { n: 20, excess: 0.1, lo: -0.01, hi: 0.2 }).parts.map((p) => p.text)).toEqual(['超额 +0.10R', '样本 20'])
  expect(ledgerSrc({ regime: '防守', month: '2026-11' })).toBe('账本·防守格 2026-11')
})

it('末行布局: ≤3 列补满; ≥4 列每张最多跨 2 列, 奇数空列第一张加 1, 其余居中对称; 末行满 / 单列不动', () => {
  expect(lastRowLayout(2, 1)).toEqual({ spans: [2], offset: 0 })
  expect(lastRowLayout(4, 1)).toEqual({ spans: [2], offset: 1 })
  expect(lastRowLayout(4, 2)).toEqual({ spans: [2, 2], offset: 0 })
  expect(lastRowLayout(4, 3)).toEqual({ spans: [2, 1, 1], offset: 0 })
  // 奇数空列: 第一张加 1 列 → 补满或左右对称 (2026-11-26 第二轮审查: 旧期望 (5,1)→[2]@1 是左 1 右 2 不对称, 已改)
  expect(lastRowLayout(5, 1)).toEqual({ spans: [3], offset: 1 })
  expect(lastRowLayout(5, 2)).toEqual({ spans: [3, 2], offset: 0 })
  expect(lastRowLayout(3, 1)).toEqual({ spans: [3], offset: 0 })
  expect(lastRowLayout(3, 2)).toEqual({ spans: [2, 1], offset: 0 })
  expect(lastRowLayout(7, 1)).toEqual({ spans: [3], offset: 2 })
  // 不变式: 所有 cols ≤ 8 / rem 组合, 左右空列相等 (末行补满或居中)
  for (let c = 2; c <= 8; c += 1) for (let r = 1; r < c; r += 1) {
    const { spans, offset } = lastRowLayout(c, r)
    const used = spans.reduce((a, b) => a + b, 0)
    expect(c - used - offset).toBe(offset)
    if (c <= 3) expect(used).toBe(c)
  }
  expect(lastRowLayout(3, 0)).toEqual({ spans: [], offset: 0 })
  expect(lastRowLayout(1, 0)).toEqual({ spans: [], offset: 0 })
})

it('槽位 3 闭环: 因子 → 引用策略 + 近期表现; 写明不在策略账本', async () => {
  const uses = [{ id: 'rs20', label: '相对强度 20 日', n: 2, share: 0.6, sign: 1, inLib: true, ics: [0.03, 0.02], dirIcs: [0.03, 0.02], dirMean: 0.025, flag: null,
    sel: [true, false], uses: [{ id: 's1', name: '强势回踩', fam: '回踩', w: 0.4, inPlay: true }, { id: 's2', name: '突破', fam: '突破', w: 0.6, inPlay: false }] }] as ScoreUse[]
  await render(<ScoringLoop uses={uses} weeks={['W46', 'W47']} />)
  expect(host.querySelector('[data-loop-note]')!.textContent).toContain('不在策略账本 · 口径 = 周度 IC')
  const f = host.querySelector('[data-loop-factor="rs20"]')!
  expect(f.querySelectorAll('[data-loop-strategy]')).toHaveLength(Number(f.querySelector('[data-loop-count]')!.getAttribute('data-loop-count')))
  expect(f.textContent).toContain('周度入选 1/2')
  expect(f.querySelector('[data-loop-strategy]')!.textContent).toBe('突破 · 权重 0.60')
})

it('辅助: isObserve / 锚定来源中文 / 门 / 网格 auto-fit', () => {
  expect(isObserve(CARDS.t0)).toBe(false)
  expect(isObserve({ grade: null, blocked: 'observe_only', timing_tier: 'T0' })).toBe(true)
  expect(anchorText({ anchor_price: 10, anchor_source: 'premarket' })).toBe('锚定 10.00 · 盘前价')
  expect(anchorText(null)).toBe('锚定价 —')
  expect(gateText('unknown+4h.state@tight-v1')).toBe('方向门未知 · 4h 方向同向 · tight-v1')
  expect(GRID).toContain('auto-fit')
  expect(GRID).not.toContain('auto-fill')
  expect(regimeFp(REGIME_CALM)).toBe('进攻|2026-11-24|snapshot')
})
