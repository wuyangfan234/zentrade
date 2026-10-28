// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useCardFlash } from './useCardFlash'

let root: Root
let host: HTMLDivElement
let scrolls = 0
beforeEach(() => {
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  scrolls = 0
  Element.prototype.scrollIntoView = vi.fn(() => { scrolls += 1 })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals() })

let go: (to: string) => void = () => {}
function Target({ highlight, contentKey }: { highlight: string; contentKey?: string }) {
  go = useNavigate()
  const { ref } = useCardFlash(highlight, 'card', contentKey)
  return <div ref={ref} />
}

it('同一链接再次点击 (新导航) 会重新定位, 同一次导航只定位一次', async () => {
  await act(async () => { root.render(<MemoryRouter><Target highlight="card" /></MemoryRouter>) })
  expect(scrolls).toBe(1)
  await act(async () => { root.render(<MemoryRouter><Target highlight="card" /></MemoryRouter>) })
  expect(scrolls).toBe(1)                       // 重渲染不重复
  await act(async () => { go('/settings?tab=x&highlight=card') })
  expect(scrolls).toBe(2)                       // 新导航 → 再定位
})

it('内容切换 (contentKey 变化) 时重新定位', async () => {
  await act(async () => { root.render(<MemoryRouter><Target highlight="card" contentKey="alpaca" /></MemoryRouter>) })
  await act(async () => { root.render(<MemoryRouter><Target highlight="card" contentKey="qveris" /></MemoryRouter>) })
  expect(scrolls).toBe(2)
})

it('highlight 不匹配时不滚动', async () => {
  await act(async () => { root.render(<MemoryRouter><Target highlight="other" /></MemoryRouter>) })
  expect(scrolls).toBe(0)
})
