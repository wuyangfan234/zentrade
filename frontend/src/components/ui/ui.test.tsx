// @vitest-environment jsdom
// 0.22.0 统一组件库: 只用设计 token; PriceLadder 位置换算; 页面禁止硬编码色值/裸 rose-*/rounded-lg
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, it } from 'vitest'
import { Badge, Card, ContextChips, Freshness, PriceLadder, StatTile, ladderPos } from './index'

function html(el: React.ReactElement) {
  const host = document.createElement('div')
  const root = createRoot(host)
  act(() => root.render(el))
  return host
}

it('ladderPos maps stop..target to 0..100 and clamps', () => {
  expect(ladderPos(95, 95, 115)).toBe(0)
  expect(ladderPos(105, 95, 115)).toBe(50)
  expect(ladderPos(130, 95, 115)).toBe(100)
  expect(ladderPos(105, 115, 95)).toBe(50)       // 空头: 止损在上
})

it('PriceLadder renders markers and stop distance', () => {
  const h = html(<PriceLadder side="long" stop={95} entry={100} target={115} price={105} />)
  expect(h.querySelector('[data-ladder-price]')?.getAttribute('style')).toContain('left: 50%')
  expect(h.textContent).toContain('95.00')
  expect(h.textContent).toContain('止损 -9.5%')
})

it('Badge tones use tokens only', () => {
  const h = html(<><Badge tone="long">多</Badge><Badge tone="short">空</Badge><Badge tone="withdrawn">已撤</Badge><Badge tone="ref">参考</Badge></>)
  const cls = Array.from(h.querySelectorAll('[data-badge]')).map((e) => e.className).join(' ')
  expect(cls).toContain('text-bull'); expect(cls).toContain('text-bear'); expect(cls).not.toMatch(/rose-|#[0-9a-f]{3,6}/i)
})

it('StatTile / ContextChips / Freshness / Card render', () => {
  const h = html(<Card title="T" right={<Freshness at="2026-10-14T14:45:00" />}>
    <StatTile label="多" value={3} tone="long" /><ContextChips items={[{ label: '日线', value: '↑', tone: 'long' }]} />
  </Card>)
  expect(h.querySelector('[data-card]')?.className).toContain('rounded-card')
  expect(h.textContent).toContain('14:45 ET')
  expect(h.querySelector('[data-stat]')?.textContent).toContain('3')
  expect(h.querySelector('[data-chip]')?.textContent).toContain('日线')
  expect(html(<Freshness at="2026-10-14T14:45:00" stale />).querySelector('[data-fresh="stale"]')).not.toBeNull()
})

it('ui kit and opportunity desk use design tokens only (no hex / rose- / rounded-lg)', () => {
  const files = [join(__dirname, 'index.tsx'), join(__dirname, '../opportunities/Desk.tsx'), join(__dirname, '../opportunities/StatusRail.tsx')]
  const bad: string[] = []
  for (const f of files) {
    const s = readFileSync(f, 'utf8')
    if (/#[0-9a-fA-F]{6}\b|rose-\d|rounded-lg/.test(s)) bad.push(f)
  }
  expect(bad).toEqual([])
})
