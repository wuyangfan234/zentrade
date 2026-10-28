// @vitest-environment jsdom
// 0.38.0 自选股公司图标: 有版本出图片, 没有/加载失败退回字母头像 (同尺寸), 字母头像色相固定且避开红绿。
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, it } from 'vitest'
import { StockLogo, fallbackHue, fallbackText, logoUrl } from './StockLogo'

function mount(el: React.ReactElement) {
  const host = document.createElement('div')
  const root = createRoot(host)
  act(() => root.render(el))
  return host
}

it('renders the cached image with a versioned, encoded url', () => {
  const h = mount(<StockLogo symbol="BRK.B" version="1729" />)
  const box = h.querySelector('[data-stock-logo="BRK.B"]')!
  expect(box.getAttribute('data-logo-kind')).toBe('image')
  expect(h.querySelector('img')!.getAttribute('src')).toBe('/api/watchlist/logos/BRK.B?v=1729')
  expect(logoUrl('A/B', '1')).toBe('/api/watchlist/logos/A%2FB?v=1')
})

it('falls back to a letter avatar when there is no version', () => {
  const h = mount(<StockLogo symbol="NVDA" />)
  const box = h.querySelector('[data-stock-logo="NVDA"]')!
  expect(box.getAttribute('data-logo-kind')).toBe('fallback')
  expect(box.textContent).toBe('NV')
  expect(h.querySelector('img')).toBeNull()
})

it('falls back to the letter avatar when the image fails to load', () => {
  const h = mount(<StockLogo symbol="MU" version="1" />)
  act(() => { h.querySelector('img')!.dispatchEvent(new Event('error')) })
  const box = h.querySelector('[data-stock-logo="MU"]')!
  expect(box.getAttribute('data-logo-kind')).toBe('fallback')
  expect(box.textContent).toBe('M')
})

it('image and fallback share the same box size', () => {
  const img = mount(<StockLogo symbol="AAPL" version="1" size="lg" />).querySelector('[data-stock-logo]')!
  const fb = mount(<StockLogo symbol="AAPL" size="lg" />).querySelector('[data-stock-logo]')!
  const sizeOf = (e: Element) => e.className.split(/\s+/).filter(c => /^(h|w)-/.test(c)).sort().join(' ')
  expect(sizeOf(img)).toBe('h-9 w-9')
  expect(sizeOf(fb)).toBe(sizeOf(img))
})

it('fallback hue is stable per symbol and never red/green', () => {
  for (const s of ['NVDA', 'AAPL', 'TSLA', 'SKHY', 'BRK.B', 'X']) {
    const h = fallbackHue(s)
    expect(fallbackHue(s)).toBe(h)
    expect(h < 20 || h > 340).toBe(false)        // 红 = 涨
    expect(h > 90 && h < 170).toBe(false)        // 绿 = 跌
  }
  expect(fallbackText('BRK.B')).toBe('BR')
  expect(fallbackText('MU')).toBe('M')
})

it('watchlist table, card and group views all render StockLogo', () => {
  const src = (p: string) => readFileSync(join(__dirname, '..', p), 'utf-8')
  const page = src('pages/Watchlist.tsx')
  expect(page).toContain('useStockLogos(')
  expect(page).toContain('<StockLogo symbol={r.symbol} version={logoMap[r.symbol]} size="md" />')
  expect(page).toContain('<StockLogo symbol={r.symbol} version={logoVersion} size="lg" />')
  expect(page).toContain('logos={logoMap}')
  expect(src('components/WatchlistGroupCards.tsx')).toContain('<StockLogo symbol={r.symbol} version={logos[r.symbol]}')
})
