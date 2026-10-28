// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ systemStatus: vi.fn(), entryFunnel: vi.fn(() => new Promise(() => {})) }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
vi.mock('@/components/PageHeader', () => ({ PageHeader: ({ title }: { title: string }) => <h1>{title}</h1> }))

import { SystemStatus } from './SystemStatus'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.systemStatus.mockResolvedValue({
    at: '2026-10-02T10:00:00-04:00',
    components: [
      { id: 'alpaca_mirror', label: 'Alpaca 纸面镜像', state: 'error', summary: '最近错误 403', source: '持仓页' },
      { id: 'execution_quotes', label: '纸面撮合报价', state: 'ok', summary: '当前来源 tickflow', source: '/api/trading/status' },
    ],
    jobs: [{ id: 'zen_hot_quotes', next_run: '2026-10-02T10:00:05-04:00' }],
  })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear() })

it('shows each component with state, summary and source, plus scheduled jobs', async () => {
  await act(async () => { root.render(<QueryClientProvider client={qc}><SystemStatus /></QueryClientProvider>) })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const mirror = host.querySelector('[data-component="alpaca_mirror"]')!.textContent ?? ''
  expect(mirror).toContain('异常')
  expect(mirror).toContain('最近错误 403')
  expect(host.querySelector('[data-component="execution_quotes"]')!.textContent).toContain('出处：/api/trading/status')
  expect(host.querySelector('[data-job="zen_hot_quotes"]')!.textContent).toContain('热报价 (5 秒)')
})

// ── 0.7.1: 5 种场景 ──
type C = { id: string; label: string; state: 'ok' | 'warn' | 'error' | 'idle'; summary: string; idle_kind?: 'waiting' | 'closed' | 'disabled'; impact?: string; action?: string; since?: string | null }
const mk = (phase: 'premarket' | 'session' | 'postmarket' | 'closed', trading_day = true) =>
  ({ phase, trading_day, next_open: '2026-10-12T09:30-04:00', close: trading_day ? '2026-10-12T16:00-04:00' : null })

async function renderWith(payload: object) {
  mocks.systemStatus.mockResolvedValue(payload)
  await act(async () => { root.render(<QueryClientProvider client={qc}><SystemStatus /></QueryClientProvider>) })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
const verdict = () => host.querySelector('[data-verdict]')!

it('premarket: waiting is grey, not red', async () => {
  const comps: C[] = [
    { id: 'ops_watch', label: '盘中盯盘', state: 'idle', idle_kind: 'waiting', summary: '等待今天第一个定时任务运行' },
    { id: 'paper_loop', label: '纸面交易员', state: 'idle', idle_kind: 'waiting', summary: '最近运行 —' },
    { id: 'earnings', label: '财报保护', state: 'ok', summary: '已接入' },
  ]
  await renderWith({ at: '2026-10-12T07:30:00-04:00', components: comps, jobs: [], market: mk('premarket') })
  expect(verdict().getAttribute('data-verdict')).toBe('ok')
  expect(host.querySelector('[data-market]')!.textContent).toContain('盘前 · 今天是交易日')
  expect(host.querySelector('[data-group="problems"]')).toBeNull()
  expect(host.querySelector('[data-component="ops_watch"]')!.textContent).toContain('等待首次运行')
})

it('session all ok: verdict green, ok cards expanded, no problems line', async () => {
  await renderWith({ at: '2026-10-12T10:00:00-04:00', market: mk('session'),
    components: [{ id: 'paper_loop', label: '纸面交易员', state: 'ok', summary: '最近运行 10:00' }],
    jobs: [{ id: 'zen_market_mood', next_run: null, state: 'ok', last_at: '2026-10-12T08:36:00-04:00' },
      { id: 'zen_qveris_estimates', next_run: null, state: 'running' }] })
  expect(verdict().textContent).toContain('一切正常')
  expect(host.querySelector('[data-group="ok"] [data-component="paper_loop"]')).not.toBeNull()
  expect(host.querySelector('[data-group="ok"] details')).toBeNull()
  expect(host.querySelector('[data-no-problems]')!.textContent).toContain('没有需要处理的项')
  expect(host.querySelector('[data-job="zen_qveris_estimates"]')!.textContent).toContain('进行中')
  expect(host.querySelector('[data-market]')!.textContent).toContain('收盘')
})

it('session loop stalled: red card with reason / impact / next step / last ok', async () => {
  await renderWith({ at: '2026-10-12T10:42:00-04:00', market: mk('session'),
    components: [{ id: 'paper_loop', label: '纸面交易员', state: 'error', summary: '交易环已经 6 分钟没运行',
      impact: '盘中不会开仓、也不会按规则平仓', action: '看容器日志里的交易环报错；盘中先不要重启，收盘后再处理', since: '2026-10-12T10:36:12-04:00' }],
    jobs: [] })
  expect(verdict().getAttribute('data-verdict')).toBe('error')
  const card = host.querySelector('[data-group="problems"] [data-component="paper_loop"]')!.textContent ?? ''
  for (const s of ['原因', '6 分钟没运行', '影响', '下一步', '最后正常', '美东 10:36（新加坡 22:36）']) expect(card).toContain(s)
})

it('postmarket data quality error is red, missing job opens the jobs group', async () => {
  await renderWith({ at: '2026-10-12T18:00:00-04:00', market: mk('postmarket'),
    components: [{ id: 'data_quality', label: '日K数据质量', state: 'error', summary: '对价 1 只不一致', impact: 'x', action: 'y', since: '2026-10-12T17:10:00-04:00' }],
    jobs: [{ id: 'zen_earnings_postclose', next_run: null, state: 'missing' }] })
  expect(verdict().textContent).toContain('1 项异常')
  expect(host.querySelector('[data-group="jobs"]')).not.toBeNull()
  expect(host.querySelector('[data-no-problems]')).toBeNull()
  expect(host.querySelector('[data-job="zen_earnings_postclose"]')!.textContent).toContain('财报日历（盘后）')
})

it('closed day: says market closed, nothing red', async () => {
  await renderWith({ at: '2026-10-11T12:00:00-04:00', market: { ...mk('closed', false) },
    components: [{ id: 'paper_loop', label: '纸面交易员', state: 'idle', idle_kind: 'closed', summary: '—' }], jobs: [] })
  expect(verdict().textContent).toContain('休市')
  expect(host.querySelector('[data-market]')!.textContent).toContain('今天不是交易日')
  expect(host.querySelector('[data-component="paper_loop"]')!.textContent).toContain('休市')
})

it('fmtTime marks next day in Singapore', async () => {
  const { fmtTime } = await import('./SystemStatus')
  expect(fmtTime('2026-10-12T16:00:00-04:00')).toBe('美东 16:00（新加坡次日 04:00）')
  expect(fmtTime('2026-10-12T10:36:00-04:00')).toBe('美东 10:36（新加坡 22:36）')
})

// ── 0.13.0: 与数据页/持仓页统一版式 (白底卡、图标分区标题、计数格、任务卡按下次时间排序) ──
it('0.13.0 layout: max-w-6xl 页宽; 白底卡; 问题卡左色条; 总结论四格计数', async () => {
  await renderWith({ at: '2026-10-12T10:00:00-04:00', market: mk('session'),
    components: [
      { id: 'a', label: 'A', state: 'error', summary: 'x' },
      { id: 'b', label: 'B', state: 'warn', summary: 'y' },
      { id: 'c', label: 'C', state: 'ok', summary: 'z' },
      { id: 'd', label: 'D', state: 'idle', idle_kind: 'waiting', summary: 'w' }],
    jobs: [{ id: 'zen_hot_quotes', next_run: '2026-10-12T10:00:05-04:00', state: 'ok', last_at: '2026-10-12T09:59:00-04:00' }] })
  const wrap = host.querySelector('[data-status-wrap]')!.className
  expect(wrap).toContain('max-w-6xl'); expect(wrap).toContain('md:px-8')
  expect(host.querySelector('[data-status-cols]')!.className).toContain('lg:grid-cols-')
  expect(host.querySelector('[data-group="ok"] [data-card-grid]')!.className).toContain('grid')
  expect(host.querySelector('[data-group="ok"] [data-component="c"]')!.className).toContain('bg-surface')
  expect(host.querySelector('[data-group="idle"] [data-component="d"]')).not.toBeNull()
  expect(host.querySelector('[data-group="problems"] [data-component="a"]')!.className).toContain('border-l-rose-500')
  expect(host.querySelector('[data-group="problems"] [data-component="b"]')!.className).toContain('border-l-amber-400')
  expect(host.querySelector('[data-group="problems"] h2')!.textContent).toContain('需要处理 · 2')
  const stat = (k: string) => host.querySelector(`[data-verdict-stats] [data-stat="${k}"]`)!.textContent
  expect(stat('error')).toContain('1'); expect(stat('warn')).toContain('1'); expect(stat('ok')).toContain('1'); expect(stat('idle')).toContain('1')
  expect(host.querySelector('[data-group="jobs"] [data-jobs-list] [data-job="zen_hot_quotes"]')!.textContent).toContain('已完成')
})

it('0.13.0 定时任务: 按下次运行时间排序, 下次时间拆美东/新加坡两行, 空 next_run 排最后', async () => {
  await renderWith({ at: '2026-10-13T05:11:00-04:00', market: mk('premarket'), components: [],
    jobs: [
      { id: 'daily_pipeline', next_run: '2026-10-13T16:35:00-04:00' },
      { id: 'zen_weekly_review', next_run: null },
      { id: 'zen_hot_quotes', next_run: '2026-10-13T05:12:00-04:00' },
      { id: 'pre_market_instruments', next_run: '2026-10-13T09:10:00-04:00' }] })
  const ids = [...host.querySelectorAll('[data-jobs-list] [data-job]')].map((e) => e.getAttribute('data-job'))
  expect(ids).toEqual(['zen_hot_quotes', 'pre_market_instruments', 'daily_pipeline', 'zen_weekly_review'])
  const next = host.querySelector('[data-job="daily_pipeline"] [data-job-next]')!.textContent
  expect(next).toContain('10-13 16:35 ET'); expect(next).toContain('新加坡次日 04:35')
})

it('fmtNextRun splits ET / Singapore', async () => {
  const { fmtNextRun } = await import('./SystemStatus')
  expect(fmtNextRun('2026-10-13T09:10:00-04:00')).toEqual({ et: '10-13 09:10 ET', sg: '新加坡 21:10' })
  expect(fmtNextRun(null)).toBeNull()
})

it('0.9.1 读取失败: 红色带原因与重试按钮, 点重试重新请求', async () => {
  mocks.systemStatus.mockRejectedValueOnce(new Error('HTTP 502'))
  await act(async () => { root.render(<QueryClientProvider client={qc}><SystemStatus /></QueryClientProvider>) })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  const alert = host.querySelector('[data-empty="error"]')!
  expect(alert.getAttribute('role')).toBe('alert')
  expect(alert.textContent).toContain('读取失败'); expect(alert.textContent).toContain('HTTP 502')
  const n = mocks.systemStatus.mock.calls.length
  await act(async () => { (alert.querySelector('button') as HTMLButtonElement).click() })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  expect(mocks.systemStatus.mock.calls.length).toBe(n + 1)
})
