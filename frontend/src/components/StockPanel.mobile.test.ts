// Phone layout guard: daily K and intraday chart stack vertically below md, side by side from md up.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

const src = (f: string) => readFileSync(resolve(__dirname, f), 'utf8')

it('StockPanel stacks charts on phones and keeps side-by-side on desktop', () => {
  const s = src('StockPanel.tsx')
  expect(s).toContain('flex flex-col gap-3 md:flex-row md:items-start')
  expect(s).not.toContain('<div className="flex gap-3 items-start">')
})

it('daily K info bar keeps each OHLC label with its value', () => {
  const s = src('EChartsCandlestick.tsx')
  const pairs = s.match(/white-space:nowrap;display:inline-flex;gap:4px/g) ?? []
  expect(pairs.length).toBe(8)   // 开/高/低/收 in hover bar + initial bar
})
