// @vitest-environment jsdom
// 分钟策略卡片数字未出时的占位引导: awaitRun → 「待计算」; computing → 脉冲 ···。
// 分钟策略不在 run_all/盘后缓存覆盖内 (Screener.tsx dailyPoolIds 过滤), 首屏
// 数字必为空, 纯空白无引导会让用户以为坏了 — 占位明确引导点击单跑。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { StrategyCard, cardWrapCls } from './StrategyCard'

const noop = () => {}
const base = {
  name: '分钟突破', description: '', source: 'custom' as const,
  active: false, loading: false, cardSize: 'normal' as const,
  onRun: noop, disabled: false, onSettings: noop,
  timeframeBadge: '分钟',
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

function render(props: Record<string, unknown>) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  act(() => { root!.render(<StrategyCard {...base} {...props} />) })
}

it('shows 待计算 hint when awaitRun and count is missing', () => {
  render({ awaitRun: true })

  expect(container.textContent).toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('shows pulsing placeholder (not hint) when computing', () => {
  render({ awaitRun: true, computing: true })

  expect(container.textContent).toContain('···')
  expect(container.textContent).not.toContain('待计算')
})

it('shows nothing extra for daily strategies without awaitRun', () => {
  render({})

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).not.toContain('···')
})

it('hint yields to the real count once available', () => {
  render({ awaitRun: true, count: 7 })

  expect(container.textContent).not.toContain('待计算')
  expect(container.textContent).toContain('7')
})

it('mini size also renders the hint', () => {
  render({ awaitRun: true, cardSize: 'mini' })

  expect(container.textContent).toContain('待算')
})

it('onRun fires when the card is clicked', () => {
  const onRun = vi.fn()
  render({ awaitRun: true, onRun })

  act(() => { container.querySelector('button')!.click() })

  expect(onRun).toHaveBeenCalledTimes(1)
})

// ===== 等宽等高: 长描述不得撑宽卡片 (10-10 策略页「选择策略后策略块长度不一」) =====
// 生产 30 条策略描述 0~88 字 (TV 移植策略 53~88 字)。「详细」档描述原先只有
// line-clamp-1 没有宽度上限, 卡片按描述全文变宽 (实测 146~665px)。
const LONG_DESC = 'TradingView 开源 Chandelier Exit (everget) 逐行移植, 可选 CE Ultimate 的量能/EMA 趋势/ATR 扩张过滤 (默认关闭)'

function descEl() {
  return [...container.querySelectorAll('[data-strategy-desc]')][0] as HTMLElement | undefined
}

it('large card clamps long description to 2 lines and keeps full text reachable', () => {
  render({ cardSize: 'large', name: '吊灯止损翻多', description: LONG_DESC, count: 56 })

  const d = descEl()!
  expect(d).toBeTruthy()
  expect(d.className).toContain('line-clamp-2')
  // 描述区固定 2 行高度, 无描述/短描述的卡同高
  expect(d.className).toMatch(/min-h-\[/)
  const root = container.firstElementChild as HTMLElement
  expect(root.getAttribute('title') ?? root.querySelector('[title*="Chandelier"]')?.getAttribute('title')).toContain(LONG_DESC)
})

it('normal card truncates description to one line and keeps full text reachable', () => {
  render({ cardSize: 'normal', name: '吊灯止损翻多', description: LONG_DESC, count: 56 })

  const d = descEl()!
  expect(d.className).toContain('truncate')
  expect(container.querySelector(`[title*="Chandelier"]`)).toBeTruthy()
})

it('large card reserves the description and count rows even when empty (equal heights)', () => {
  render({ cardSize: 'large', name: '超级趋势', description: '', source: 'composite' })

  expect(descEl()).toBeTruthy()
  expect(container.querySelector('[data-strategy-count-row]')).toBeTruthy()
})

it('card fills its grid cell; selected state does not change layout classes', () => {
  const layout = (cls: string) => cls.split(/\s+/).filter(c => /^(w-|h-|min-|max-|p[xytrbl]?-|inline-|flex$|grid)/.test(c)).sort().join(' ')
  render({ cardSize: 'large', description: LONG_DESC, active: false })
  const a = layout((container.firstElementChild as HTMLElement).className)
  act(() => { root!.unmount() }); root = null; container.innerHTML = ''
  render({ cardSize: 'large', description: LONG_DESC, active: true })
  const b = layout((container.firstElementChild as HTMLElement).className)

  expect(a).toContain('w-full')
  expect(a).not.toContain('inline-flex')
  expect(b).toBe(a)
})

it('normal and large sizes lay cards out on an equal-width grid; mini stays chips', () => {
  expect(cardWrapCls('normal')).toContain('grid')
  expect(cardWrapCls('large')).toContain('grid')
  expect(cardWrapCls('normal')).toMatch(/minmax\(/)
  expect(cardWrapCls('mini')).toContain('flex-wrap')
})
