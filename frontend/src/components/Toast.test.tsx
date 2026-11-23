// @vitest-environment jsdom
// 「已保存」类提示带下一步按钮: 点了执行并收起; 带按钮的多留几秒, 普通提示不变
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ToastContainer, toast } from './Toast'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => {
  vi.useFakeTimers()
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  act(() => root.render(<ToastContainer />))
})
afterEach(() => {
  act(() => { vi.runAllTimers() })
  act(() => root.unmount()); host.remove(); vi.useRealTimers()
})

it('a toast with an action renders the button; clicking runs it once and dismisses the toast', () => {
  const go = vi.fn()
  act(() => toast('已保存 · 候选方案', 'success', { action: { label: '去发布', onClick: go } }))
  const button = host.querySelector<HTMLButtonElement>('[data-toast-action]')!
  expect(host.textContent).toContain('已保存 · 候选方案')
  expect(button.textContent).toBe('去发布')
  act(() => button.click())
  expect(go).toHaveBeenCalledTimes(1)
  expect(host.textContent).not.toContain('已保存 · 候选方案')
})

it('action toasts stay 8 seconds, plain toasts keep 4 seconds and have no button', () => {
  act(() => toast('未采用', 'success'))
  act(() => toast('已保存 · 候选方案', 'success', { action: { label: '去发布', onClick: () => {} } }))
  expect(host.querySelectorAll('[data-toast-action]')).toHaveLength(1)
  act(() => { vi.advanceTimersByTime(4100) })
  expect(host.textContent).not.toContain('未采用')
  expect(host.textContent).toContain('已保存 · 候选方案')
  act(() => { vi.advanceTimersByTime(4000) })
  expect(host.textContent).not.toContain('已保存 · 候选方案')
})
