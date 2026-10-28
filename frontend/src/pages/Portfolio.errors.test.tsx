// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ portfolioOverview: vi.fn(), tradingStatus: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { Portfolio } from './Portfolio'

const overview = (nav: number) => ({ cash: nav, nav, open_risk: 0, positions: [], events: [], nav_daily: [] })
const status = (protections?: Record<string, unknown>) => ({
  booted: true, scheduled: true, in_session: false, notifications: { label: '未配置' }, protections,
})

let qc: QueryClient
let root: Root
let container: HTMLDivElement

async function flush() {
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })
  }
}

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <Portfolio />
      </QueryClientProvider>,
    )
  })
  await flush()
}

const text = () => container.textContent ?? ''
const divWithText = (t: string) => [...container.querySelectorAll('div')].find((d) => d.textContent === t)

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.portfolioOverview.mockReset()
  mocks.tradingStatus.mockReset()
  mocks.tradingStatus.mockResolvedValue(status())
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  qc.clear()
})

it('overview failure without data shows an error card, not an empty ledger', async () => {
  mocks.portfolioOverview.mockRejectedValue(new Error('HTTP 502 Bad Gateway'))
  await render()
  expect(text()).toContain('持仓数据加载失败')
  expect(text()).toContain('HTTP 502 Bad Gateway')
  expect(text()).not.toContain('暂无台账事件')
  expect(text()).not.toContain('组合风险')
  expect(text()).not.toContain('$—')
})

it('retry button refetches the overview', async () => {
  mocks.portfolioOverview.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(overview(100000))
  await render()
  const retry = [...container.querySelectorAll('button')].find((b) => b.textContent === '重试')
  expect(retry).toBeTruthy()
  await act(async () => {
    retry!.click()
  })
  await flush()
  expect(mocks.portfolioOverview).toHaveBeenCalledTimes(2)
  expect(text()).toContain('$100,000')
  expect(text()).not.toContain('持仓数据加载失败')
})

it('refresh failure with cached data keeps the old numbers and says when they are from', async () => {
  const updatedAt = new Date(2020, 0, 2, 9, 5, 7).getTime()
  qc.setQueryData(['portfolio', 'overview'], overview(123456), { updatedAt })
  mocks.portfolioOverview.mockRejectedValue(new Error('timeout'))
  await render()
  expect(mocks.portfolioOverview).toHaveBeenCalledTimes(1)
  expect(text()).toContain('刷新失败，当前显示的是 09:05:07 的数据')
  expect(text()).toContain('$123,456')
  expect(text()).not.toContain('持仓数据加载失败')
})

it('trader status failure is reported as a read failure, not as scheduler not ready', async () => {
  mocks.portfolioOverview.mockResolvedValue(overview(100000))
  mocks.tradingStatus.mockRejectedValue(new Error('HTTP 500'))
  await render()
  expect(text()).toContain('状态读取失败')
  expect(text()).not.toContain('调度未就绪')
  // 读不到状态时不能凭空宣称“已暂停新开仓”
  expect(text()).not.toContain('已暂停新开仓')
})

it('connected protections show source and freshness', async () => {
  mocks.portfolioOverview.mockResolvedValue(overview(100000))
  mocks.tradingStatus.mockResolvedValue(status({
    earnings_auto: 'connected',
    earnings_calendar: { status: 'connected', source: 'nasdaq', fetched_at: '2026-10-01T12:00:00Z' },
    corporate_actions_auto: 'connected',
  }))
  await render()
  expect(text()).toContain('财报保护：已接入（来源 nasdaq，更新于 10-01 08:00 ET）')
  expect(text()).toContain('公司行动保护：已接入')
  expect(text()).not.toContain('已暂停新开仓')
  expect(text()).not.toContain('尚未接入')
  expect(text()).toContain('当前结果仅作纸面验证，不等同可实盘策略。')
})

it('unavailable protections are shown in red as pausing new entries', async () => {
  mocks.portfolioOverview.mockResolvedValue(overview(100000))
  mocks.tradingStatus.mockResolvedValue(status({
    earnings_auto: 'unavailable',
    earnings_calendar: { status: 'unavailable', source: null, fetched_at: null },
    corporate_actions_auto: 'not_connected',
  }))
  await render()
  expect(divWithText('财报日历不可用，已暂停新开仓')?.className).toContain('text-danger')
  expect(divWithText('公司行动数据不可用，已暂停新开仓')?.className).toContain('text-danger')
  expect(text()).not.toContain('已接入')
  expect(text()).toContain('当前结果仅作纸面验证，不等同可实盘策略。')
})

// 10-10 全量 review: 后端公司行动状态有 connected / stale / unavailable 三种。缓存超过 26 小时
// (每个周末、节假日) 是 stale, 页面却一律写「数据不可用」, 与真实状态不符。stale 要写明是过期 + 上次更新时刻;
// 仍按后端口径暂停新开仓 (只改文案, 许可不变)。
it('stale corporate actions say expired with last update time, not unavailable', async () => {
  mocks.portfolioOverview.mockResolvedValue(overview(100000))
  mocks.tradingStatus.mockResolvedValue(status({
    earnings_auto: 'connected',
    earnings_calendar: { status: 'connected', source: 'nasdaq', fetched_at: '2026-10-09T21:00:00Z' },
    corporate_actions_auto: 'stale',
    corporate_actions: { status: 'stale', fetched_at: '2026-10-09T08:00:00.004596-04:00', due_at: '2026-10-12T08:00:00-04:00' },
  }))
  await render()
  // 新鲜度按交易日算 (10-11 用户批准): 过期 = 最近一个交易日 08:00 ET 的定时刷新没成功
  const line = divWithText('公司行动数据已过期（更新于 10-09 08:00 ET，10-12 08:00 ET 的定时刷新未成功），已暂停新开仓')
  expect(line?.className).toContain('text-danger')
  expect(text()).not.toContain('公司行动数据不可用')
})
