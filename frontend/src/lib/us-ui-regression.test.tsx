// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { BUILTIN_COLUMNS } from './watchlist-columns'
import { SCREENER_BUILTIN_COLUMNS, SCREENER_COLUMN_GROUPS } from './screener-columns'
import { mergeColumns, createExtColumn, buildExtColumnsParam } from './list-columns'
import { getSortValue } from './stock-table'
import { renderBuiltinDataCell } from '@/components/stock-table/primitives'
import { BUILTIN_SIGNAL_DEFINITIONS } from './signals'
import { fmtVolume, fmtBigNum, formatScheduleDatePart, formatScheduleTimePart, priceColorClass } from './format'
import { StatCard } from '@/components/data/StatCard'
import { ScheduleEditor } from '@/components/data/ScheduleEditor'
import { Portfolio } from '@/pages/Portfolio'
import { PageHeader } from '@/components/PageHeader'

const source = (path: string) => readFileSync(resolve('src', path), 'utf8')

it('60D momentum survives legacy column preferences and renders/sorts the backend field', () => {
  const legacy = { ...BUILTIN_COLUMNS.find(c => c.id === 'builtin:momentum_60d')!, source: { type: 'builtin' as const, key: 'momentum' } }
  const col = mergeColumns([legacy], BUILTIN_COLUMNS).find(c => c.id === legacy.id)!
  expect(col.source).toEqual({ type: 'builtin', key: 'momentum_60d' })
  expect(getSortValue({ momentum_60d: 0.178 }, col)).toBe(0.178)
  expect(renderToStaticMarkup(<table><tbody><tr>{renderBuiltinDataCell({ momentum_60d: 0.178 }, col)}</tr></tbody></table>)).toContain('+17.80%')
})

it('no empty turnover column is enabled by default', () => {
  for (const cols of [BUILTIN_COLUMNS, SCREENER_BUILTIN_COLUMNS]) {
    expect(cols.filter(c => c.visible).map(c => c.id)).not.toContain('builtin:turnover')
  }
})

it('removes retired limit/deviation columns from persisted preferences without touching ext data', () => {
  const ext = { ...createExtColumn('my_metrics', 'Custom metrics', 'limit_ups', 'Custom score'), visible: true }
  const obsolete = ['limit_ups', 'limit_downs', 'deviate_3d', 'deviate_10d', 'deviate_30d'].map(key => ({ id: `builtin:${key}`, label: key, source: { type: 'builtin' as const, key }, visible: true }))
  const merged = mergeColumns([...obsolete, ext], SCREENER_BUILTIN_COLUMNS)
  for (const c of obsolete) expect(merged.map(x => x.id)).not.toContain(c.id)
  expect(merged).toContainEqual(ext)
  expect(buildExtColumnsParam(merged)).toBe('my_metrics.limit_ups')
  expect(SCREENER_COLUMN_GROUPS.map(g => g.id)).not.toContain('limit')
})

it('offers no daily price-limit signals', () => {
  expect(BUILTIN_SIGNAL_DEFINITIONS.filter(s => /limit_up|limit_down/.test(s.id))).toEqual([])
})

it('sidebar identifies ETF prices, not index points', () => {
  const text = source('components/Layout.tsx')
  for (const symbol of ['SPY', 'QQQ', 'DIA', 'IWM']) expect(text).toContain(`name: '${symbol} ETF'`)
  expect(text).toContain('ETF 价格 (USD)')
  expect(text).toContain('whitespace-nowrap')
})

describe('data schedules use explicit ET and distinguish local next-run time', () => {
  it.each([
    ['2026-09-30T13:10:00Z', '09-30', '09:10 ET'],
    ['2026-12-01T14:10:00Z', '12-01', '09:10 ET'],
    ['2026-10-01T00:35:00Z', '09-30', '20:35 ET'],
  ])('%s follows America/New_York including DST', (iso, day, time) => {
    expect(formatScheduleDatePart(iso)).toBe(day)
    expect(formatScheduleTimePart(iso)).toBe(time)
  })
  it('schedule editor labels ET and the page shows last/next run both in ET (ui-audit-1011: no local GMT+8 mix)', () => {
    expect(renderToStaticMarkup(<ScheduleEditor value={{ hour: 9, minute: 10 }} onSave={() => {}} loading={false} />)).toContain('ET')
    expect(source('pages/Data.tsx')).not.toContain('下次（本地）')
    expect(source('pages/Data.tsx')).not.toContain("timeZoneName: 'short'")
    expect(source('pages/Data.tsx')).toContain('下次 {formatScheduleDatePart(s.next_pipeline_run)}')
    expect(source('components/data/QuoteConfigCard.tsx')).toContain('formatScheduleTimePart(iso)')
    expect(source('pages/Data.tsx')).not.toContain('hint="不早于 15:00"')
  })
})

it('adjustment factor date count is not presented as daily coverage', () => {
  const html = renderToStaticMarkup(<StatCard title="除权因子" hint="增量同步" tierKey="adj_factor" capLimits={{ adj_factor: true }} stats={{ rows: 8000, symbols_covered: 8000, trading_days: 1, earliest_date: '2025-08-05', latest_date: '2026-09-29' }} />)
  expect(html).toContain('因子记录')
  expect(html).not.toContain('· 1 日')
  expect(html).toContain('日K覆盖起')
})

it('empty ledger has an explicit empty state, not a blank events section', () => {
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  qc.setQueryData(['portfolio', 'overview'], { cash: 100000, nav: 100000, open_risk: 0, positions: [], events: [], nav_daily: [] })
  const html = renderToStaticMarkup(<QueryClientProvider client={qc}><Portfolio /></QueryClientProvider>)
  expect(html).toContain('暂无台账事件')
  qc.clear()
})

it('strategy toolbar wraps by control and keeps title and labels intact', () => {
  const html = renderToStaticMarkup(<PageHeader title="策略" right={<button>策略池</button>} />)
  expect(html).toContain('whitespace-nowrap')
  const text = source('pages/Screener.tsx')
  expect(text).toContain('aria-label="策略工具栏"')
  expect(text).toMatch(/flex-wrap[^"\n]*whitespace-nowrap/)
})

it.each(['pages/settings/AI.tsx', 'custom/assistant/ui/AssistantDrawer.tsx'])('no sponsor promotion or sponsor API call in %s', path => {
  expect(source(path)).not.toMatch(/runninghub|sponsorModels|RhPromoBanner/i)
})

it.each(['pages/Onboarding.tsx', 'pages/settings/DataSourceEditor.tsx', 'components/screener/DefaultStrategyParamsDialog.tsx', 'components/screener/ScreenerFilter.tsx', 'components/screener/StrategySettingsDialog.tsx'])('US units and onboarding copy in %s', path => {
  const text = source(path)
  expect(text).not.toMatch(/本地化的 A 股|全 A 股|title: '连板梯队'|成交量 \(手\)|成交额 \(元\)|unit[:=][ ]*['"]元['"]/)
})

it('debug page has no retired ladder entry or calls', () => {
  expect(source('pages/Dev.tsx')).not.toMatch(/LadderTestPanel|monitorRuleTestLadder|monitorRuleTriggerLadder|封单监控/)
})

it('keeps the existing user-selected red-up / green-down classes', () => {
  expect(priceColorClass(0.02)).toBe('text-bull')
  expect(priceColorClass(-0.02)).toBe('text-bear')
})

it('量额格式化只作数量级缩写，不换算手数或币种，缺失值不伪装为零', () => {
  expect(fmtVolume(123)).toBe('123')
  expect(fmtVolume(12345)).toBe('1.23万')
  expect(fmtBigNum(125001)).toBe('13万')
  for (const value of [null, undefined, NaN]) {
    expect(fmtVolume(value)).toBe('—')
    expect(fmtBigNum(value)).toBe('—')
  }
  const volume = BUILTIN_COLUMNS.find(c => c.id === 'builtin:volume')!
  const amount = SCREENER_BUILTIN_COLUMNS.find(c => c.id === 'builtin:amount')!
  const html = renderToStaticMarkup(<table><tbody><tr>{renderBuiltinDataCell({ volume: 123 }, volume)}{renderBuiltinDataCell({ amount: 125001 }, amount)}</tr></tbody></table>)
  expect(html).toContain('>123<')
  expect(html).toContain('>13万<')
})

it('自选和策略列名明确股数与美元，不对紧凑数字添加错误单位', () => {
  expect(BUILTIN_COLUMNS.find(c => c.id === 'builtin:volume')?.label).toBe('成交量 (股)')
  for (const cols of [BUILTIN_COLUMNS, SCREENER_BUILTIN_COLUMNS]) {
    expect(cols.find(c => c.id === 'builtin:amount')?.label).toBe('成交额 (USD)')
    expect(cols.find(c => c.id === 'builtin:float_val')).toBeUndefined()
  }
})

it('首次向导不承诺虚构的全市场数量、拉取耗时或无数据源也可用', () => {
  const text = source('pages/Onboarding.tsx')
  expect(text.match(/5500|1-3 分钟|无需 Key|即可使用全部功能|IEX 15 分钟延迟/)).toBeNull()
  expect(text).toContain('美股')
  expect(text).toContain('纸面交易')
})

it('助手上下文不再引用连板梯队入口', () => {
  expect(source('custom/assistant/AssistantLauncher.tsx').match(/limit-ladder|连板梯队/)).toBeNull()
})
