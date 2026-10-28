// @vitest-environment jsdom
import { act, type ComponentProps } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, expectTypeOf, it, vi } from 'vitest'
import { EChartsIntraday } from './EChartsIntraday'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  on: vi.fn(), off: vi.fn(), setOption: vi.fn(), clear: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
}))
vi.mock('echarts', () => ({ init: () => chart }))
vi.mock('@/lib/theme', () => ({ useChartTheme: () => ({}) }))
const rows = [
  { datetime: '2026-09-09 09:30:00', open: 119.77, high: 119.77, low: 119, close: 119.1, volume: 100, amount: 11910 },
  { datetime: '2026-09-09 11:20:00', open: 118.25, high: 118.28, low: 118.24, close: 118.25, volume: 55, amount: 6503.75 },
]
const daily = { date: '2026-09-09', open: 119.77, high: 119.77, low: 118.16, close: 118.25 }
let cleanup = async () => {}
afterEach(async () => { await cleanup(); vi.unstubAllGlobals() })

it('shows daily OHLC by default, labels hovered minute, restores on exit and date switch', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.on.mockImplementation((name, callback) => { chart.handlers[name] = callback })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  const render = async (date = daily.date) => {
    await act(async () => root.render(<EChartsIntraday data={rows.map(row => ({ ...row, datetime: row.datetime.replace(daily.date, date) }))}
      date={date} dailySummary={{ ...daily, date }} />))
  }
  await render()
  expect(host.textContent).toContain('日K')
  expect(host.textContent).toContain('118.16')
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 110 }] }))
  expect(host.textContent).toContain('11:20')
  expect(host.textContent).toContain('118.28')
  await act(async () => chart.handlers.globalout())
  expect(host.textContent).toContain('118.16')
  expect(host.textContent).not.toContain('11:20')
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 110 }] }))
  await render('2026-09-10')
  expect(host.textContent).toContain('118.16')
  expect(host.textContent).not.toContain('11:20')
})

it('aggregates available minutes without inventing a missing opening price', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(<EChartsIntraday data={rows.map(row => ({ ...row, open: null }))} date={daily.date} />))
  expect(host.textContent).toContain('分时汇总')
  expect(host.textContent).toContain('119.77')
  expect(host.textContent).toContain('—')
  expect(host.textContent).toContain('155')
})

// ================================================================
// y 轴按真实数据自适应，保留大幅波动及无昨收场景。
// ================================================================
const wideRows = (day: string) => [
  { datetime: `${day} 09:30:00`, open: 15, high: 15, low: 15, close: 15, volume: 100, amount: 1500 },
  { datetime: `${day} 11:20:00`, open: 57.7, high: 57.8, low: 57.7, close: 57.77, volume: 55, amount: 3177.35 },
]

function lastYAxis(): any {
  const call = chart.setOption.mock.calls.at(-1)
  return call?.[0]?.yAxis?.[0]
}

it('adaptive y-axis covers large US stock moves without exchange-specific flags', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday
      data={wideRows('2026-09-18')}
      date="2026-09-18"
      prevClose={20.8}
    />,
  ))
  const axis = lastYAxis()
  expect(axis.min).toBeLessThanOrEqual(15)
  expect(axis.max).toBeGreaterThanOrEqual(57.77)
})

it('US stock: adaptive y-axis uses the observed range, not a price-limit band', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday
      data={[
        { datetime: '2026-09-18 09:30:00', open: 20, high: 20, low: 20, close: 20, volume: 100, amount: 2000 },
        { datetime: '2026-09-18 15:00:00', open: 22, high: 22, low: 22, close: 22, volume: 55, amount: 1210 },
      ]}
      date="2026-09-18"
      prevClose={20}
    />,
  ))
  const axis = lastYAxis()
  expect(axis.min).toBeCloseTo(17.8, 6)
  expect(axis.max).toBeCloseTo(22.2, 6)
})

it('listing day without prevClose anchors y-axis with scale, not zero', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday data={wideRows('2026-09-17')} date="2026-09-17" />,
  ))
  const axis = lastYAxis()
  expect(axis.min).toBeUndefined()
  expect(axis.max).toBeUndefined()
  expect(axis.scale).toBe(true)
})

it('cannot restore removed price-limit controls or bands with legacy props', async () => {
  expectTypeOf<ComponentProps<typeof EChartsIntraday>>().not.toHaveProperty('priceLimit')
  expectTypeOf<ComponentProps<typeof EChartsIntraday>>().not.toHaveProperty('showLimitLines')
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  // A stale caller spreading old settings must not resurrect the removed mode.
  const legacy = { showLimitLines: true, priceLimit: { rate: 0.1, limit_up: 132, limit_down: 108, source: 'rule' as const } }
  await act(async () => root.render(
    <EChartsIntraday data={rows} prevClose={120} priceLines={[{ value: 121, label: '目标' }]} {...legacy} />,
  ))
  expect(host.querySelectorAll('button')).toHaveLength(0)
  expect(host.textContent).not.toContain('涨跌停')
  const option = chart.setOption.mock.calls.at(-1)![0]
  expect(option.series[0].markLine.data.map((line: any) => line.yAxis)).toEqual([120, 121])
  expect(option.yAxis[0].min).toBeGreaterThan(108)
  expect(option.yAxis[0].max).toBeLessThan(132)
})

it('keeps 16:00 as an empty, non-hoverable boundary rather than a trading minute', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.on.mockImplementation((name, callback) => { chart.handlers[name] = callback })
  const host = document.createElement('div')
  const root = createRoot(host)
  const hover = vi.fn()
  cleanup = async () => { await act(async () => root.unmount()) }
  await act(async () => root.render(
    <EChartsIntraday data={[...rows, { ...rows[0], datetime: `${daily.date} 16:00:00` }]} onPriceHover={hover} />,
  ))
  const option = chart.setOption.mock.calls.at(-1)![0]
  expect(option.xAxis[0].data.at(-1)).toBe('16:00')
  for (const series of option.series) {
    expect(series.data).toHaveLength(391)
    expect(series.data[390]).toBeNull()
  }
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 390 }] }))
  expect(hover).toHaveBeenLastCalledWith(null)
})


// ================================================================
// 累计成交额单位: 亿 = 1e8。NVDA 实例: 8171 万股 × ~$230 ≈ $188 亿, 旧实现除以 1e9 显示成 18.83亿。
// ================================================================
it('cumulative dollar amount uses 1亿 = 1e8, not 1e9', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  const big = [
    { datetime: '2026-09-30 09:30:00', open: 229.27, high: 230, low: 229, close: 229.5, volume: 40_000_000, amount: 9_200_000_000 },
    { datetime: '2026-09-30 15:59:00', open: 228.4, high: 228.5, low: 228.2, close: 228.38, volume: 41_709_470, amount: 9_630_000_000 },
  ]
  await act(async () => root.render(<EChartsIntraday data={big} date="2026-09-30" />))
  expect(host.textContent).toContain('188.30亿')
  expect(host.textContent).not.toContain('18.83亿')
})
