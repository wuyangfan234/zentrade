// @vitest-environment jsdom
// 个股分析页必须有股票搜索入口: 输入代码/名称 → 下拉候选 → 选中即切换分析标的。
// 回归 (2026-09-29): P1-5 删财务模块时连带删掉了 StockFinancialSearch, 页面搜索位
// 只剩空 div, 首次进入 (无「上次」记录) 时完全无法选择股票。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const searchMock = vi.fn(async (q: string) => ({
  results: q.toUpperCase().startsWith('NV')
    ? [{ symbol: 'NVDA', name: 'NVIDIA Corporation Common Stock', code: 'NVDA', asset_type: 'stock' }]
    : [],
}))

vi.mock('@/lib/api', () => ({
  api: {
    instrumentSearch: (q: string) => searchMock(q),
    klineDaily: vi.fn(() => new Promise(() => {})),
    stockAnalysisLevels: vi.fn(() => new Promise(() => {})),
  },
}))
vi.mock('@/lib/stockAnalysisStore', () => ({
  startAnalysis: vi.fn(), findTodayReport: vi.fn(), deleteReport: vi.fn(),
  openHistoryReport: vi.fn(), loadHistory: vi.fn(),
  useHistoryReports: () => ({ reports: [], loaded: true }),
}))
vi.mock('@/components/stock-analysis/AnalysisKChart', () => ({ AnalysisKChart: () => null }))
vi.mock('@/components/stock-analysis/PriceAlertDialog', () => ({ PriceAlertDialog: () => null }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

import { StockAnalysis } from './StockAnalysis'

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

beforeEach(() => {
  localStorage.clear()
  searchMock.mockClear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

async function render() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(<QueryClientProvider client={qc}><StockAnalysis /></QueryClientProvider>)
  })
}

function searchInput(): HTMLInputElement | null {
  return container.querySelector('input[type="text"]')
}

async function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    setter.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  // 等 react-query 拉到候选并渲染
  for (let i = 0; i < 20 && !container.textContent?.includes('NVIDIA'); i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
  }
}

it('renders a stock search box when no stock is selected', async () => {
  await render()

  expect(container.textContent).toContain('选择一只股票开始分析')
  expect(searchInput()).not.toBeNull()
})

it('selecting a search result switches the analysis target', async () => {
  await render()
  const input = searchInput()
  expect(input).not.toBeNull()

  await typeInto(input!, 'nv')
  expect(searchMock).toHaveBeenCalledWith('nv')
  const option = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('NVDA'))
  expect(option).toBeTruthy()

  await act(async () => { option!.click() })

  expect(container.textContent).not.toContain('选择一只股票开始分析')
  expect(container.textContent).toContain('AI 个股分析')
  expect(JSON.parse(localStorage.getItem('last_stock:stock-analysis') ?? 'null')?.symbol ?? null)
    .toBe('NVDA')
})

it('0.16.2: 财务维已接入 SEC EDGAR 同步, 文案不再写「暂无」, 无数据时由后端退回暂无', async () => {
  await render()
  const text = container.textContent ?? ''
  expect(text).not.toContain('财务（暂无）')
  expect(text).toContain('财务（SEC EDGAR）')
  const { readFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const dlg = readFileSync(resolve('src/components/stock-analysis/StockAnalysisDialog.tsx'), 'utf8')
  const page = readFileSync(resolve('src/pages/StockAnalysis.tsx'), 'utf8')
  for (const src of [dlg, page]) {
    expect(src).not.toContain('财务暂无')
    expect(src).not.toContain('财务（暂无）')
    expect(src).not.toContain('美股暂无财务数据源')
  }
  expect(dlg).toContain('读取日 K / 技术指标 / 压力支撑 / 财务（SEC EDGAR）')
})
