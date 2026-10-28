// @vitest-environment jsdom
// 策略池弹窗删除: 仅自定义/AI/叠加策略有垃圾桶; 两段确认后调后端删除,
// 成功则移出已选、通知父层; 内置策略不出现删除入口。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyPoolDialog } from './StrategyPoolDialog'

const strategies = [
  { id: 'macd_golden', name: 'MACD 金叉', description: '', source: 'builtin', timeframes: ['1d'] },
  { id: 'ai_aaa', name: '动量回调', description: '', source: 'ai', timeframes: ['1d'] },
  { id: 'custom_bbb', name: '自定义B', description: '', source: 'custom', timeframes: ['1d'] },
  { id: 'ai_draft', name: '草稿C', description: '', source: 'ai', research_only: true, timeframes: ['1d'] },
]
let current = [...strategies]
const mockDelete = vi.fn(async (id: string) => {
  current = current.filter(s => s.id !== id)
  return { ok: true }
})

vi.mock('@/lib/api', () => ({
  api: {
    strategyList: vi.fn(async () => ({ strategies: current })),
    strategyDelete: (id: string) => mockDelete(id),
    strategyPublish: vi.fn(async () => ({ ok: true })),
    strategySaveCodeV2: vi.fn(),
  },
}))

let root: Root | null = null
let container: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  current = [...strategies]
  mockDelete.mockClear()
  container = document.createElement('div')
  document.body.appendChild(container)
})

afterEach(() => {
  act(() => root?.unmount())
  root = null
  container.remove()
})

async function render(pool: string[], onDeleted = vi.fn()) {
  root = createRoot(container)
  await act(async () => {
    root!.render(<StrategyPoolDialog pool={pool} onConfirm={vi.fn()} onClose={vi.fn()} onDeleted={onDeleted} />)
  })
  await act(async () => { await Promise.resolve() })
  return onDeleted
}

const trash = (name: string) =>
  document.querySelector<HTMLButtonElement>(`button[aria-label="删除策略 ${name}"]`)
const byText = (text: string) =>
  Array.from(document.querySelectorAll('button')).find(b => b.textContent?.trim() === text)

it('内置策略没有删除入口, 用户策略有', async () => {
  await render(['macd_golden', 'ai_aaa'])
  expect(trash('MACD 金叉')).toBeNull()
  expect(trash('动量回调')).not.toBeNull()   // 已选列表
  expect(trash('自定义B')).not.toBeNull()    // 待选列表
})

it('点垃圾桶需二次确认, 取消不删', async () => {
  await render(['ai_aaa'])
  await act(async () => { trash('动量回调')!.click() })
  expect(mockDelete).not.toHaveBeenCalled()
  await act(async () => { byText('取消')!.click() })
  expect(mockDelete).not.toHaveBeenCalled()
  expect(trash('动量回调')).not.toBeNull()
})

it('确认删除后调后端、通知父层并从列表消失', async () => {
  const onDeleted = await render(['ai_aaa', 'macd_golden'])
  await act(async () => { trash('动量回调')!.click() })
  await act(async () => { byText('确认删除')!.click() })
  await act(async () => { await Promise.resolve() })
  expect(mockDelete).toHaveBeenCalledWith('ai_aaa')
  expect(onDeleted).toHaveBeenCalledWith('ai_aaa')
  expect(trash('动量回调')).toBeNull()
  expect(document.body.textContent).toContain('已删除: 动量回调')
})

it('删除失败时保留策略并显示错误', async () => {
  mockDelete.mockImplementationOnce(async () => { throw new Error('该策略被叠加策略 [x] 引用') })
  const onDeleted = await render([])
  await act(async () => { trash('自定义B')!.click() })
  await act(async () => { byText('确认删除')!.click() })
  await act(async () => { await Promise.resolve() })
  expect(onDeleted).not.toHaveBeenCalled()
  expect(document.body.textContent).toContain('引用')
  expect(trash('自定义B')).not.toBeNull()
})
