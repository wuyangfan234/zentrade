// @vitest-environment jsdom
// P3 (2026-10-11): AI 悬浮球不挡内容 —— 默认贴右下角, 页面滚动时淡出且不拦点击, 停下后恢复; 主区底部留白不被它盖住。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('../store', () => ({ toggleAssistant: vi.fn(), useAssistantStore: () => ({ open: false, sending: false }) }))
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const strip = ({ initial: _i, animate: _a, transition: _t, whileHover: _h, whileTap: _w, ...rest }: Record<string, unknown>) => rest
  return { motion: { button: React.forwardRef((p: Record<string, unknown>, ref) => React.createElement('button', { ...strip(p), ref })) } }
})

import { AssistantFloatingButton } from './AssistantFloatingButton'

let root: Root | null = null
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  vi.useFakeTimers()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  vi.useRealTimers()
  document.body.innerHTML = ''
})

function mount(w: number, h: number) {
  Object.assign(window, { innerWidth: w, innerHeight: h })
  const host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  act(() => { root!.render(<AssistantFloatingButton />) })
  return document.querySelector('button[aria-label="AI 助手"]') as HTMLButtonElement
}

it('桌面默认贴右下角, 不再停在右侧中下部压住内容', () => {
  const b = mount(1440, 900)
  expect(b.style.left).toBe(`${1440 - 44 - 24}px`)
  expect(b.style.top).toBe(`${900 - 44 - 24}px`)
})

it('页面滚动时淡出且不拦点击, 停止滚动后恢复', () => {
  const b = mount(390, 844)
  const scroller = document.createElement('div'); document.body.appendChild(scroller)
  act(() => { scroller.dispatchEvent(new Event('scroll')) })
  expect(b.getAttribute('data-scrolling')).toBe('1')
  expect(b.className).toContain('opacity-30')
  expect(b.className).toContain('pointer-events-none')
  act(() => { vi.advanceTimersByTime(1000) })
  expect(b.getAttribute('data-scrolling')).toBeNull()
  expect(b.className).not.toContain('opacity-30')
})

it('主区底部留白 (桌面也留), 最后一行内容能滚到悬浮球上方', () => {
  const layout = readFileSync(resolve('src/components/Layout.tsx'), 'utf8')
  expect(layout).toMatch(/className="h-full overflow-auto[^"]*md:pb-20/)
})
