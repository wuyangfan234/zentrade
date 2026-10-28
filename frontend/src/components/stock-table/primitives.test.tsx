// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ColumnConfig } from '@/lib/list-columns'
import { renderBuiltinDataCell } from './primitives'

function deviationCell(days: number, value: number | null | undefined) {
  const key = `deviate_${days}d`
  const col: ColumnConfig = {
    id: `builtin:${key}`, source: { type: 'builtin', key },
    label: key, visible: true, align: 'right',
  }
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(
    <table><tbody><tr>{renderBuiltinDataCell({ [key]: value }, col)}</tr></tbody></table>,
  )
  return host.querySelector('td')!
}

describe.each([3, 10, 30])('retired deviate_%id renderer', days => {
  it('does not retain an active deviation tooltip for old columns', () => {
    expect(deviationCell(days, 0.1234).title).toBe('')
  })

  it.each([0.1234, -0.0567, 0, null, undefined])('does not revive legacy value %s or its directional color', value => {
    const cell = deviationCell(days, value)
    expect(cell.textContent).toBe('—')
    expect(cell.classList.contains('text-muted')).toBe(true)
    expect(cell.classList.contains('text-bull')).toBe(false)
    expect(cell.classList.contains('text-bear')).toBe(false)
  })
})
