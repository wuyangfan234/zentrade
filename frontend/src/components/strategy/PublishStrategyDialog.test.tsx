// @vitest-environment jsdom
// 11-16 易用性: 挖掘候选「发布为策略」弹窗 —— 默认名称可改、名称校验、发布后在弹窗里直达回测 / 策略页;
// 已发布挖掘策略的改名弹窗。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import {
  PublishStrategyDialog,
  RenameStrategyDialog,
  backtestHref,
  cleanStrategyName,
  strategyHref,
  strategyNameError,
} from './PublishStrategyDialog'

let root: Root | null = null
let host: HTMLDivElement | null = null

function mount(node: React.ReactNode) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => { root!.render(<MemoryRouter>{node}</MemoryRouter>) })
}

afterEach(() => {
  act(() => root?.unmount())
  host?.remove()
  root = null
  host = null
})

const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector<T>(sel)

function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    setter.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('名称规则与后端一致: 压空白, 1–40 字', () => {
  expect(cleanStrategyName('  ATR(14)原值   低 ')).toBe('ATR(14)原值 低')
  expect(strategyNameError('   ')).toBe('名称不能为空')
  expect(strategyNameError('x'.repeat(41))).toBe('名称最多 40 个字')
  expect(strategyNameError('中'.repeat(40))).toBeNull()
  expect(backtestHref('mined_factor_ab')).toBe('/backtest?strategy=mined_factor_ab&run=1')
  expect(strategyHref('mined_factor_ab')).toBe('/screener?s=mined_factor_ab')
})

it('默认名称预填, 改名后发布, 成功后直接给「回测 / 查看策略」', async () => {
  const onPublish = vi.fn(async (name: string) => ({ strategy_id: 'mined_factor_ab', name }))
  mount(<PublishStrategyDialog defaultName="ATR(14)原值 低" facts="样本外夏普 2.87" onPublish={onPublish} onClose={() => {}} />)

  const input = q<HTMLInputElement>('[data-publish-name]')!
  expect(input.value).toBe('ATR(14)原值 低')
  expect(document.body.textContent).toContain('样本外夏普 2.87')

  typeInto(input, '  低波 ATR  ')
  await act(async () => { q<HTMLButtonElement>('[data-publish-submit]')!.click() })

  expect(onPublish).toHaveBeenCalledWith('低波 ATR')
  expect(q('[data-publish-done]')?.textContent).toContain('低波 ATR')
  expect(q<HTMLAnchorElement>('[data-publish-backtest]')!.getAttribute('href')).toBe('/backtest?strategy=mined_factor_ab&run=1')
  expect(q<HTMLAnchorElement>('[data-publish-view]')!.getAttribute('href')).toBe('/screener?s=mined_factor_ab')
})

it('空名称不能提交; 发布失败原因留在弹窗里', async () => {
  const onPublish = vi.fn(async () => { throw new Error('名称已被其他策略使用: MA 金叉') })
  mount(<PublishStrategyDialog defaultName="ATR(14)原值 低" onPublish={onPublish} onClose={() => {}} />)

  const input = q<HTMLInputElement>('[data-publish-name]')!
  typeInto(input, '   ')
  expect(q<HTMLButtonElement>('[data-publish-submit]')!.disabled).toBe(true)
  expect(document.body.textContent).toContain('名称不能为空')

  typeInto(input, 'MA 金叉')
  await act(async () => { q<HTMLButtonElement>('[data-publish-submit]')!.click() })
  expect(q('[data-publish-done]')).toBeNull()
  expect(q('[data-publish-error]')?.textContent).toBe('发布失败 · 名称已被其他策略使用: MA 金叉')
})

it('改名弹窗: 未改动不能保存, 保存后关闭', async () => {
  const onRename = vi.fn(async () => ({}))
  const onClose = vi.fn()
  mount(<RenameStrategyDialog currentName="因子组合 · atr_14" onRename={onRename} onClose={onClose} />)

  const submit = q<HTMLButtonElement>('[data-rename-submit]')!
  expect(submit.disabled).toBe(true)
  typeInto(q<HTMLInputElement>('[data-rename-name]')!, 'ATR(14)原值 低')
  await act(async () => { q<HTMLButtonElement>('[data-rename-submit]')!.click() })
  expect(onRename).toHaveBeenCalledWith('ATR(14)原值 低')
  expect(onClose).toHaveBeenCalled()
})
