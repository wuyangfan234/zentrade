// @vitest-environment jsdom
// 0.18.0: 持仓卡「初始→当前止损 · 调整 N 次」, 点开看每次调整 (数据来自 /api/portfolio/live 的 stop_initial / stop_moves)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { StopTrail } from './Portfolio'

let root: Root
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

const moves = [
  { seq: 10, at: 'a', trade_date: '2026-10-08', from: 9, stop_price: 10, reason: 'trailing_1.0R' },
  { seq: 11, at: 'b', trade_date: '2026-10-09', from: 10, stop_price: 10.5, reason: 'trailing_1.2R' },
]

it('shows initial → current and count, expands to each move', async () => {
  await act(async () => root.render(<StopTrail initial={9} current={10.5} moves={moves} />))
  const el = host.querySelector('[data-stop-trail]')!
  expect(el.textContent).toContain('$9.00→$10.50')
  expect(el.textContent).toContain('调整 2 次')
  expect(host.querySelectorAll('[data-stop-move]').length).toBe(0)
  await act(async () => (host.querySelector('[data-stop-toggle]') as HTMLButtonElement).click())
  const rows = [...host.querySelectorAll('[data-stop-move]')].map((e) => e.textContent)
  expect(rows).toEqual(['2026-10-08 $9.00→$10.00 trailing_1.0R', '2026-10-09 $10.00→$10.50 trailing_1.2R'])
})

it('no moves: no toggle, count 0', async () => {
  await act(async () => root.render(<StopTrail initial={9} current={9} moves={[]} />))
  expect(host.querySelector('[data-stop-trail]')!.textContent).toContain('调整 0 次')
  expect(host.querySelector('[data-stop-toggle]')).toBeNull()
})
