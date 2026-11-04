// @vitest-environment jsdom
// 11-04 因子页审计: 自动挖掘不能断开正在跑的任务, 超时与真实结果
import { expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ miningRun: vi.fn(), miningAutoStart: vi.fn() }))
vi.mock('./api', () => ({ api: mocks }))

class FakeEventSource {
  static instances: FakeEventSource[] = []
  closed = false
  onopen = null
  onerror = null
  constructor(public url: string) { FakeEventSource.instances.push(this) }
  close() { this.closed = true }
  addEventListener() {}
}
vi.stubGlobal('EventSource', FakeEventSource)

import { attachMiningRun, startAutoMining } from './miningTask'

it('auto mining while a run is active returns busy and keeps the run attached', async () => {
  const run = { run_id: 'active-A', status: 'running', request: {}, progress: null }
  mocks.miningRun.mockResolvedValue(run)
  mocks.miningAutoStart.mockResolvedValue({ started: false, screening: { n_total: 2, reason_counts: {} } })
  expect(await attachMiningRun('active-A')).toBe(true)
  const source = FakeEventSource.instances.at(-1)!
  expect(await startAutoMining({ asset_type: 'stock' } as never)).toBe('busy')
  expect(mocks.miningAutoStart).not.toHaveBeenCalled()
  expect(source.closed).toBe(false)
  expect(localStorage.getItem('mining_active_run_id')).toBe('active-A')
})

it('the auto-mining request uses the compute timeout', async () => {
  const { readFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const src = readFileSync(resolve('src/lib/api.ts'), 'utf8')
  const block = src.slice(src.indexOf('miningAutoStart:'), src.indexOf('miningStart:'))
  expect(block).toContain('timeoutMs: COMPUTE_REQUEST_TIMEOUT_MS')
})
