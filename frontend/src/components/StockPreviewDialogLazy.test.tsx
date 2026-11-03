// @vitest-environment jsdom
// 0.47.2 性能守门: 详情弹窗按需加载; 首屏页面不再静态引入 echarts; 字体不再走第三方外链。
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const renders: (string | null)[] = []
vi.mock('./StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, name }: { symbol: string | null; name?: string }) => {
    renders.push(symbol)
    return symbol ? <div data-impl={symbol} data-name={name ?? ''} /> : <div data-impl-closed="" />
  },
}))
import { StockPreviewDialog } from './StockPreviewDialogLazy'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let host: HTMLDivElement
let root: Root
beforeEach(() => { renders.length = 0; host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })

async function flush() { for (let i = 0; i < 5; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

it('does not load or mount the implementation until a symbol is opened', async () => {
  await act(async () => { root.render(<StockPreviewDialog symbol={null} onClose={() => {}} />) })
  await flush()
  expect(renders).toEqual([])
  expect(host.innerHTML).toBe('')
})

it('forwards props once opened and stays mounted after closing so the exit animation can run', async () => {
  await act(async () => { root.render(<StockPreviewDialog symbol="NVDA" name="NVIDIA" onClose={() => {}} />) })
  await flush()
  expect(host.querySelector('[data-impl="NVDA"]')?.getAttribute('data-name')).toBe('NVIDIA')
  await act(async () => { root.render(<StockPreviewDialog symbol="AMD" onClose={() => {}} />) })
  expect(host.querySelector('[data-impl="AMD"]')).not.toBeNull()
  await act(async () => { root.render(<StockPreviewDialog symbol={null} onClose={() => {}} />) })
  expect(host.querySelector('[data-impl-closed]')).not.toBeNull()
  expect(renders.at(-1)).toBeNull()
})

function staticImports(file: string): string[] {
  const src = readFileSync(resolve('src', file), 'utf8')
  return [...src.matchAll(/^\s*import\s+(?!type\b)[^'"]*?from\s+['"]([^'"]+)['"]/gm)].map(m => m[1])
}

it.each([
  ['pages/Watchlist.tsx'], ['pages/Intel.tsx'], ['pages/Monitor.tsx'], ['pages/Lots.tsx'], ['pages/Screener.tsx'], ['pages/StockAnalysis.tsx'],
])('%s opens stock details through the lazy entry', (file) => {
  const imports = staticImports(file)
  expect(imports).not.toContain('@/components/StockPreviewDialog')
  expect(imports).toContain('@/components/StockPreviewDialogLazy')
})

it('factor page loads the chart-heavy tabs on demand', () => {
  const imports = staticImports('pages/Factors.tsx')
  for (const heavy of ['./backtest/FactorDiscovery', './backtest/MiningWorkbench', './backtest/ResearchCandidatesDialog']) {
    expect(imports).not.toContain(heavy)
  }
  expect(readFileSync(resolve('src/pages/Factors.tsx'), 'utf8')).toMatch(/lazy\(\(\) => import\('\.\/backtest\/FactorDiscovery'\)/)
})

it('fonts are self-hosted: no render-blocking third-party stylesheet, every font file exists', () => {
  const html = readFileSync(resolve('index.html'), 'utf8')
  expect(html).not.toMatch(/rsms\.me|fonts\.googleapis|fonts\.gstatic/)
  const css = readFileSync(resolve('src/fonts.css'), 'utf8')
  const urls = [...css.matchAll(/url\('\.\/([^']+)'\)/g)].map(m => m[1])
  expect(urls.length).toBe(7)
  for (const u of urls) expect(statSync(resolve('src', u)).size).toBeGreaterThan(10_000)
  // OFL 1.1: 许可正文随字体一起分发 (public/ 根目录单层文件, 后端 spa_fallback 原样返回)
  const dir = resolve('public')
  expect(readdirSync(dir).filter(f => f.startsWith('LICENSE-font-')).sort()).toEqual(['LICENSE-font-Inter.txt', 'LICENSE-font-JetBrainsMono.txt'])
  for (const f of ['LICENSE-font-Inter.txt', 'LICENSE-font-JetBrainsMono.txt']) expect(readFileSync(join(dir, f), 'utf8')).toContain('SIL Open Font License')
  expect(readFileSync(resolve('src/main.tsx'), 'utf8')).toMatch(/import '\.\/fonts\.css'/)
})
