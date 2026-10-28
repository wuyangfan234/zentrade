// ui-audit-1011 (2026-10-11): 显示层逐页审计的回归断言 —— 文案/术语/标点/时区/残留/死链。只断言展示, 不涉及计算。
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { expect, it } from 'vitest'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { SCREENER_BUILTIN_COLUMNS } from '@/lib/screener-columns'
import { boardTag } from '@/components/stock-table/primitives'
import { sourceName } from '@/pages/backtest/EvidenceDesk'

const src = (p: string) => readFileSync(resolve('src', p), 'utf8')

it('术语: 页面不再直接露出 enriched / worker / outer folds / matrix-native / Beam / Finalists / BETA 等内部词', () => {
  const cases: [string, string[]][] = [
    ['pages/Screener.tsx', ['毫秒级 SQL', 'enriched 表', '右上角「策略池」']],
    ['pages/backtest/MiningWorkbench.tsx', ['outer folds', 'outer 折', 'matrix-native', '>Beam<', '>Finalists<', 'run ID', 'worker。', '独立 worker', 'T-1 环境', 'enriched 交易日', '<span>Sharpe</span>']],
    ['pages/backtest/StrategyOptimizer.tsx', ['独立 worker']],
    ['pages/backtest/StrategyBacktest.tsx', ['BETA', 'Matrix 策略', 'ETF enriched', '（推荐）']],
    ['pages/Data.tsx', ['title="Enriched"', "label: 'Enriched'", 'Enriched 指标数据', '下次（本地）']],
    ['components/data/SectionTitle.tsx', ['`enriched ']],
    ['components/data/SchemaModal.tsx', ["'Enriched'", "'ETF Enriched'"]],
    ['pages/Portfolio.tsx', ['台账即真相']],
    ['components/stock-analysis/StockSearchBox.tsx', ['如 NVDA / Apple']],
  ]
  for (const [file, banned] of cases) for (const b of banned) expect(src(file), `${file}: ${b}`).not.toContain(b)
  expect(src('pages/Backtest.tsx')).toContain('影子账本（只记录不下单）')
})

it('列名: 60日动量 (不中英混写 60D)', () => {
  for (const cols of [BUILTIN_COLUMNS, SCREENER_BUILTIN_COLUMNS]) {
    expect(cols.find((c) => c.id === 'builtin:momentum_60d')!.label).toBe('60日动量')
  }
})

it('A 股残留: 无创/科/北板块角标, 引导页不再宣传看板/涨跌分布/情绪雷达/财务分析, 菜单设置首项叫「机会」并含持仓/系统状态', () => {
  expect(boardTag('300750')).toBeNull()
  expect(boardTag('688981')).toBeNull()
  const onboarding = src('pages/Onboarding.tsx')
  for (const b of ['情绪雷达', '涨跌分布', "title: '财务分析'", '「选股」页']) expect(onboarding).not.toContain(b)
  const menu = src('pages/settings/MenuSettings.tsx')
  expect(menu).toContain("{ id: '/', label: '机会'")
  expect(menu).toContain("{ id: '/paper', label: '持仓'")
  expect(menu).toContain("{ id: '/status', label: '系统状态'")
  expect(src('components/data/EnrichedRebuildPanel.tsx')).not.toContain('财务分析页面')
})

it('证据页来源名: 事件来源有中文兜底, 不露原始 id', () => {
  expect(sourceName({}, 'earnings_beat_drift')).toBe('财报超预期后漂移')
  expect(sourceName({ x: '甲' }, 'x')).toBe('甲')
  expect(sourceName({}, 'unknown_src')).toBe('unknown_src')
})

it('时区: 数据页「最后获取」与下次运行按美东显示; 系统状态页下次运行带 ET; 持仓页最近检查不露裸 ISO', () => {
  expect(src('components/data/QuoteConfigCard.tsx')).toContain('formatScheduleTimePart(iso)')
  // 0.7.1: 下次运行改由 fmtTime 显示「美东 HH:MM（新加坡 HH:MM）」
  expect(src('pages/SystemStatus.tsx')).toContain("`美东 ${f('America/New_York')}（新加坡${next} ${f('Asia/Singapore')}）`")
  // 0.13.0: 定时任务下次运行由 fmtNextRun 拆两行「MM-DD HH:MM ET」+「新加坡 HH:MM」(渲染断言见 SystemStatus.test)
  expect(src('pages/SystemStatus.tsx')).toContain("et: `${day('America/New_York').slice(5)} ${f('America/New_York')} ET`")
  expect(src('pages/SystemStatus.tsx')).toContain('fmtNextRun(j.next_run)')
  expect(src('pages/Portfolio.tsx')).toContain('scheduleStamp(trader.data.last_run_at)')
})

it('版式: 持仓/系统状态页头与其它页一致通栏; 手机净值卡可收缩; 侧栏「AI 配置 未配置」不换行不压点', () => {
  for (const f of ['pages/Portfolio.tsx', 'pages/SystemStatus.tsx']) {
    expect(src(f)).not.toMatch(/<div className="mx-auto max-w-3xl p-6">\s*<PageHeader/)
  }
  expect(src('pages/Portfolio.tsx')).toContain('data-nav-cards className="grid grid-cols-3 gap-2 sm:gap-3"')
  expect(src('components/Layout.tsx')).toContain('ml-auto shrink-0 whitespace-nowrap text-[11px] leading-none text-muted">未配置')
})

// 中文句内半角逗号/分号: 只扫字符串与 JSX 文本 (跳过注释行与 CSV 导出行)
function walk(dir: string, out: string[] = []): string[] {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (p.endsWith('.tsx') && !p.includes('.test.')) out.push(p)
  }
  return out
}

it('标点: 中文句内不用半角逗号/分号', () => {
  const bad: string[] = []
  const re = /[\u4e00-\u9fff）」]\s?[,;]\s?(?=[\u4e00-\u9fff「（A-Za-z0-9$])/
  for (const f of walk(resolve('src'))) {
    readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
      const t = line.trim()
      if (/^(\/\/|\*|\/\*|\{\/\*)/.test(t) || line.includes('lines.push')) return
      const code = line.replace(/(?<![:"'`])\/\/(?!\/).*$/, '')
      if (re.test(code)) bad.push(`${f.split('/src/')[1]}:${i + 1}`)
    })
  }
  expect(bad).toEqual([])
})

it('证据页持仓浮动 R 按全站涨跌色 (bull/bear), 不再正绿负红反着来', () => {
  const s = src('pages/backtest/EvidenceDesk.tsx')
  expect(s).toContain("r.r_now > 0 ? 'text-bull' : 'text-bear'")
  expect(s).not.toContain("r.r_now < 0 ? 'text-rose-700 dark:text-rose-300' : 'text-emerald-700")
  expect(s).toContain('forward_min_n ?? 300} 笔')
})

it('设置: 说明文字可换行不截断; 无「连续竞价」A 股用语; rpm 写成中文', () => {
  for (const f of ['pages/settings/Monitoring.tsx', 'pages/settings/System.tsx']) expect(src(f)).not.toContain('text-[11px] text-muted truncate')
  const m = src('pages/settings/Monitoring.tsx')
  expect(m).not.toContain('连续竞价')
  expect(m).not.toContain('配额 (rpm)')
})

it('持仓: 当前 R / 风险有悬停说明, 镜像偏差用「基点」', () => {
  const s = src('pages/Portfolio.tsx')
  expect(s).toContain('title="浮动盈亏，R = 以止损距离为 1 份风险"')
  expect(s).not.toContain('} bps`')
})

// 色板 token `base` 与 Tailwind 字号 token 撞名: text-base 会连带生成
// `color: hsl(var(--base))`, 暗色下等于页面背景色 -> 文字隐身 (净值/现金实证)。
// 16px 字号一律写 text-[16px] leading-6, 见 tailwind.config.ts 注释。
it('字号: 全站不用 text-base (会把文字染成背景色)', () => {
  const bad: string[] = []
  for (const f of walk(resolve('src'))) {
    readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
      if (/\btext-base\b/.test(line)) bad.push(`${f.split('/src/')[1]}:${i + 1}`)
    })
  }
  expect(bad).toEqual([])
})

it('持仓: 净值/现金/组合风险三卡都显式给文字色, 不靠 body 继承', () => {
  const s = src('pages/Portfolio.tsx')
  const cards = [...s.matchAll(/className="mt-1 truncate ([^"]+)"/g)].map((m) => m[1])
  expect(cards.length).toBe(3)
  for (const cls of cards) {
    expect(cls, cls).toMatch(/\btext-\[16px\]/)
    expect(cls, cls).toMatch(/\btext-(foreground|danger)\b/)
  }
})
