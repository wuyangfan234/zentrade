// @vitest-environment jsdom
/** Todo #10 P1 UI: regime 状态条 / 策略账本 / 卡时效 + 逐条归因 / 观察级降级 / 区间条 / 理由行 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const toasts = vi.hoisted(() => ({ calls: [] as unknown[][] }))
vi.mock('@/components/Toast', () => ({ toast: (...a: unknown[]) => { toasts.calls.push(a) } }))

import { ciScale, fR, isObserve, ledgerRows, nextRejudge, parseReason, regimeFp, regimeOf, tierReason, anchorText, gateText } from './p1'
import { RegimeBar } from './RegimeBar'
import { LedgerBlock } from './LedgerBlock'
import { CiBar, CI_MIN_PX } from './CiBar'
import { BoardCards, GRID } from './Cards'
import { ReasonLine, challengerReason } from '@/pages/factors/FactorSlots'
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

it('账本: 五档分组, 计数 = 行数; 待定 / 冷冻带图标 + 「不出卡」文字; 区间缺写「区间未返回」', async () => {
  await render(<LedgerBlock swing={swingWith([])} names={new Map([['tv_halftrend', '半趋势翻空']])} />)
  const rows = $$('[data-ledger-row]')
  expect(rows).toHaveLength(Object.keys(LEDGER.cells).length)
  expect(host.querySelector('[data-ledger-count]')!.textContent).toBe(String(rows.length))
  for (const g of $$('[data-ledger-group]')) {
    const t = g.getAttribute('data-ledger-group')!
    const n = g.querySelectorAll('[data-ledger-row]').length
    expect(host.querySelector(`[data-ledger-tier-count="${t}"]`)!.textContent).toContain(`${t} ${n}`)
  }
  expect($$('[data-ledger-group]').map((g) => g.getAttribute('data-ledger-group'))).toEqual(['在用', '观察', '待定', '冷冻', '复活'])
  const frozen = host.querySelector('[data-ledger-row="volume_price_surge|long"]')!
  expect(frozen.textContent).toContain('❄冷冻·不出卡')
  expect(host.querySelector('[data-ledger-row="tv_halftrend|short"]')!.textContent).toContain('半趋势翻空')
  expect(frozen.querySelector('[data-ci-bar]')!.getAttribute('aria-label')).toBe('区间未返回')
})

it('账本: unavailable / disabled 有明示; 无切片写未返回', async () => {
  await render(<LedgerBlock swing={swingWith([], { ledger: { status: 'unavailable', cells: {} } })} names={new Map()} />)
  expect(host.querySelector('[data-ledger-status="unavailable"]')!.textContent).toContain('不出卡')
  await render(<LedgerBlock swing={swingWith([], {})} names={new Map()} />)
  expect(host.querySelector('[data-ledger-empty]')!.textContent).toContain('未返回')
  expect(ledgerRows(null, new Map())).toEqual([])
})

it('区间条: 0 基准线; 跨 0 标记; 极窄区间也有最小像素; 端点数字', async () => {
  const { min, max } = ciScale([0.14, 0.05, 0.23, 0.11, 0.02, 0.2])
  expect(min).toBeLessThan(0)
  expect(max).toBeLessThan(0.3)
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
