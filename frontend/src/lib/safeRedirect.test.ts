// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { safeRedirect } from './safeRedirect'

describe('safeRedirect', () => {
  it('站内相对路径原样保留 (含查询与锚点)', () => {
    expect(safeRedirect('/')).toBe('/')
    expect(safeRedirect('/paper')).toBe('/paper')
    expect(safeRedirect('/backtest?tab=evidence#cards')).toBe('/backtest?tab=evidence#cards')
  })

  it('空值回落首页', () => {
    expect(safeRedirect(null)).toBe('/')
    expect(safeRedirect(undefined)).toBe('/')
    expect(safeRedirect('   ')).toBe('/')
  })

  it.each([
    'https://evil.example/',
    '//evil.example',
    '/\\evil.example',
    '\\\\evil.example',
    '/\\/evil.example',
    'javascript:alert(1)',
    'evil.example',
    '/\t/evil.example',
  ])('拒绝外部或畸形目标: %s', (raw) => {
    expect(safeRedirect(raw)).toBe('/')
  })

  it('百分号编码的路径只当站内路径处理, 不会跨源', () => {
    const out = safeRedirect('/%0a//evil.example')
    expect(out.startsWith('/')).toBe(true)
    expect(out.startsWith('//')).toBe(false)
    expect(new URL(out, window.location.origin).origin).toBe(window.location.origin)
  })
})
