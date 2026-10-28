// 首屏 (机会页) 不得静态打包 ECharts (~1MB): K 线 / 分时只在点开卡片后才需要, 必须懒加载。
// 回归背景 (2026-10-07 性能审查): 机会页静态 import StockDailyKChart → 首页冷启动多下 1MB echarts。
// 0.29.0: 卡片点开的是个股预览 (StockPreviewDialog → StockPanel → ECharts), 机会页及其直接子组件都不得静态 import 它。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

const page = readFileSync(resolve(__dirname, 'Opportunities.tsx'), 'utf-8')
const children = ['opportunities/Desk.tsx', 'opportunities/StatusRail.tsx', 'opportunities/RulesConsole.tsx']
  .map((f) => [f, readFileSync(resolve(__dirname, '../components', f), 'utf-8')] as const)
const STATIC = /^import[^\n]*from '@\/components\/(StockPreviewDialog|StockDailyKChart|StockPanel|EChartsCandlestick)'/m

it('opportunity page loads the stock preview (and its charts) lazily', () => {
  expect(page).not.toMatch(STATIC)
  expect(page).toMatch(/lazy\(\(\) => import\('@\/components\/StockPreviewDialog'\)/)
  for (const [f, src] of children) expect(src, f).not.toMatch(STATIC)
})
