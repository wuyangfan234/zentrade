// @vitest-environment jsdom
// 审计 2026-10-26 H-3: 盘中实时拼接的首根分钟 volume=null, 分时组件 .toFixed 崩溃; 路由无错误兜底整页白屏。
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { EChartsIntraday } from '@/components/EChartsIntraday'
import { EChartsMultiDayIntraday } from '@/components/EChartsMultiDayIntraday'
import { RouteError } from '@/components/RouteError'
import { computeIntradayAverage, summarizeMinutes } from '@/lib/intraday-chart'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  on: vi.fn(), off: vi.fn(), setOption: vi.fn(), clear: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
}))
vi.mock('echarts', () => ({ init: () => chart }))
vi.mock('@/lib/theme', () => ({ useChartTheme: () => ({}) }))

const stitched = [
  { datetime: '2026-10-26T09:30:00', open: 180.0, high: 180.1, low: 180.0, close: 180.1, volume: null, amount: null },
  { datetime: '2026-10-26T09:31:00', open: 180.11, high: 180.22, low: 180.11, close: 180.22, volume: 12000, amount: null },
]

let cleanup = async () => {}
afterEach(async () => { await cleanup(); vi.unstubAllGlobals() })

function mount() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.on.mockImplementation((name: string, cb: any) => { chart.handlers[name] = cb })
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  return { host, root }
}

it('EChartsIntraday hover on null-volume minute shows a dash instead of crashing', async () => {
  const { host, root } = mount()
  await act(async () => root.render(<EChartsIntraday data={stitched} date="2026-10-26" prevClose={179} />))
  expect(host.textContent).toContain('12000')                      // 累计量只算已知分钟
  await act(async () => chart.handlers.updateAxisPointer({ axesInfo: [{ axisDim: 'x', value: 0 }] }))
  expect(host.textContent).toContain('09:30')
  expect(host.textContent).toMatch(/量—/)
})

it('EChartsMultiDayIntraday renders a session whose latest row has null volume', async () => {
  const { host, root } = mount()
  const sessions = [
    { date: '2026-10-23', prev_close: 175, rows: [{ datetime: '2026-10-23T15:59:00', open: 179, high: 179.2, low: 178.9, close: 179, volume: 50000, amount: 8950000 }] },
    { date: '2026-10-26', prev_close: 179, rows: [stitched[0]] },
  ]
  await act(async () => root.render(<EChartsMultiDayIntraday sessions={sessions} />))
  expect(host.textContent).toMatch(/量—/)
})

it('summary helpers do not invent volume', () => {
  expect(summarizeMinutes([stitched[0]])?.volume).toBeNull()
  expect(summarizeMinutes(stitched)?.volume).toBe(12000)
  expect(computeIntradayAverage(stitched)).toEqual([null, null])
})

it('a crashing page keeps the layout and shows the in-app error panel', async () => {
  const { host, root } = mount()
  const origErr = console.error; console.error = () => {}
  function FakeLayout() { return <div><nav data-nav>导航栏</nav><Outlet /></div> }
  function Crashy(): never { throw new TypeError("Cannot read properties of null (reading 'toFixed')") }
  const router = createMemoryRouter([{
    path: '/', element: <FakeLayout />, errorElement: <RouteError fullPage />,
    children: [{ errorElement: <RouteError />, children: [{ index: true, element: <Crashy /> }] }],
  }])
  await act(async () => root.render(<RouterProvider router={router} />))
  console.error = origErr
  expect(host.querySelector('[data-nav]')).not.toBeNull()
  expect(host.textContent).toContain('这个页面出错了')
  expect(host.textContent).not.toContain('Unexpected Application Error')
})

it('production router wires page-level and app-level error elements', async () => {
  const { router } = await import('@/router')
  const root: any = router.routes.find(r => r.path === '/')!
  expect(root.errorElement).toBeTruthy()
  expect(root.children?.length).toBe(1)
  const pages: any = root.children![0]
  expect(pages.path).toBeUndefined()
  expect(pages.errorElement).toBeTruthy()
  expect(pages.children?.some((c: any) => c.index)).toBe(true)
})
