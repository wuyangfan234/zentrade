// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { clearBacktest, stopBacktest, tryReconnect } from './backtestTask'

const RECONNECT_KEY = 'backtest_reconnect'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

class FakeEventSource {
  static instances: FakeEventSource[] = []
  readonly label: string
  closeCount = 0
  onopen: (() => void) | null = null

  constructor(readonly url: string) {
    this.label = new URL(url, 'http://test.local').searchParams.get('strategy_id') ?? 'unknown'
    FakeEventSource.instances.push(this)
  }

  addEventListener() {}
  close() { this.closeCount += 1 }
}

function success(): Response {
  return { ok: true, status: 200, body: { cancel: () => Promise.resolve() } } as unknown as Response
}

async function flush() {
  for (let i = 0; i < 10; i += 1) await Promise.resolve()
}

afterEach(() => {
  clearBacktest()
  localStorage.clear()
  FakeEventSource.instances = []
  vi.unstubAllGlobals()
})

it('stale preflight completion cannot replace the current task connection', async () => {
  const pending = { A: deferred<Response>(), B: deferred<Response>() }
  const sources = FakeEventSource.instances
  const fetcher = vi.fn((input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/backtest/strategy/stream?')) {
      const label = new URL(url, 'http://test.local').searchParams.get('strategy_id') as 'A' | 'B'
      return pending[label].promise
    }
    if (url === '/api/backtest/strategy/cancel') return Promise.resolve({ ok: true } as Response)
    return Promise.reject(new Error(`unexpected fetch: ${url}`))
  })
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('EventSource', FakeEventSource)

  localStorage.setItem(RECONNECT_KEY, 'strategy_id=A')
  expect(tryReconnect()).toBe(true)
  localStorage.setItem(RECONNECT_KEY, 'strategy_id=B')
  expect(tryReconnect()).toBe(true)

  pending.B.resolve(success())
  await flush()
  expect(sources.map((source) => source.label)).toEqual(['B'])

  pending.A.resolve(success())
  await flush()
  // The old A preflight must return without creating or replacing a connection.
  expect(sources.map((source) => source.label)).toEqual(['B'])

  await stopBacktest()
  expect(sources[0].label).toBe('B')
  expect(sources[0].closeCount).toBe(1)
})
