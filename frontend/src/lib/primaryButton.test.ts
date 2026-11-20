// 主按钮守门: 操作按钮一律用 index.css 的 .btn-primary (亮色墨黑底白字, 暗色反相为近白底深字)。
// 蓝色实底白字只留给选中标记与计数: 勾选框、选中日期、当前页签 / 子页签 / 分段、显示开关、「NEW」角标、筛选计数、动作徽章。
// 新增操作按钮写成 bg-accent + text-white 时这里会红: 改用 btn-primary; 确是选中标记再把文件登记进 KEEP_BLUE。
// 按行统计: 类名串基本写在一行里; 按字面量配对会被 JSX 文本、正则里的引号打乱, 不可靠。
import { readdirSync, readFileSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { expect, it } from 'vitest'

const SRC = resolve(__dirname, '..')

function productionSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(dir, entry.name)
    if (entry.isDirectory()) return productionSources(path)
    if (!/\.(?:ts|tsx)$/.test(entry.name) || /\.test\.[^.]+$/.test(entry.name) || entry.name.endsWith('.d.ts')) return []
    return [path]
  })
}

const SOLID_ACCENT = /(?<![\w:-])bg-accent(?:\/9\d)?(?![\w/-])/
const WHITE_TEXT = /(?<![\w:-])text-white(?![\w/-])/

const KEEP_BLUE: Record<string, number> = {
  'components/DatePicker.tsx': 2,
  'components/Layout.tsx': 1,
  'components/ListColumnCustomizer.tsx': 1,
  'components/monitor/RuleEditor.tsx': 1,
  'components/ui/badge.ts': 1,
  'pages/Backtest.tsx': 1,
  'pages/Factors.tsx': 1,
  'pages/Screener.tsx': 1,
  'pages/backtest/AutoMiningDialog.tsx': 1,
  'pages/backtest/FactorDiscovery.tsx': 1,
  'pages/backtest/MiningWorkbench.tsx': 1,
  'pages/backtest/RobustnessValidation.tsx': 1,
  'pages/backtest/StrategyBacktest.tsx': 1,
  'pages/settings/ExtPages.tsx': 1,
}

it('蓝色实底白字只用于选中标记与计数, 操作按钮走 btn-primary', () => {
  const found: Record<string, number> = {}
  for (const file of productionSources(SRC)) {
    for (const line of readFileSync(file, 'utf8').split('\n')) {
      if (SOLID_ACCENT.test(line) && WHITE_TEXT.test(line)) {
        const key = relative(SRC, file).split('\\').join('/')
        found[key] = (found[key] ?? 0) + 1
      }
    }
  }
  expect(found).toEqual(KEEP_BLUE)
})

it('index.css 定义主按钮样式 btn-primary (前景色实底 + 卡片色文字)', () => {
  const css = readFileSync(resolve(SRC, 'index.css'), 'utf8')
  expect(css).toMatch(/\.btn-primary\s*\{[^}]*bg-foreground[^}]*text-surface/)
})
