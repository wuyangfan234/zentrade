// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/Toast')>()),
  toast: vi.fn(),
}))

import { toast } from '@/components/Toast'
import { useQuoteStream } from './useQuoteStream'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  closeCount = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener() {}
  close() {
    this.closeCount += 1
  }
}

let visibility: DocumentVisibilityState = 'visible'
let qc: QueryClient
let root: Root
let container: HTMLDivElement

function setVisibility(state: DocumentVisibilityState) {
  visibility = state
  document.dispatchEvent(new Event('visibilitychange'))
}

function Probe({ enabled }: { enabled: boolean }) {
  useQuoteStream(enabled, undefined)
  return null
}

function mount(enabled = true) {
  act(() => {
    root.render(
      <QueryClientProvider client={qc}>
        <Probe enabled={enabled} />
      </QueryClientProvider>,
    )
  })
}

const instances = () => FakeEventSource.instances

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  vi.mocked(toast).mockClear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  visibility = 'visible'
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  qc.clear()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('releases the stream while hidden, reconnects and refreshes once when visible, and detaches on unmount', () => {
  mount()
  expect(instances()).toHaveLength(1)

  act(() => setVisibility('hidden'))
  expect(instances()[0].closeCount).toBe(1)
  act(() => {
    vi.advanceTimersByTime(120_000)
  })
  expect(instances()).toHaveLength(1)

  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  act(() => setVisibility('visible'))
  expect(instances()).toHaveLength(2)
  expect(invalidate).toHaveBeenCalledTimes(1)

  act(() => root.unmount())
  expect(instances()[1].closeCount).toBe(1)
  act(() => {
    setVisibility('hidden')
    setVisibility('visible')
    vi.advanceTimersByTime(120_000)
  })
  expect(instances()).toHaveLength(2)
})

it('hiding cancels a pending reconnect timer', () => {
  mount()
  act(() => instances()[0].onerror!())
  act(() => setVisibility('hidden'))
  act(() => {
    vi.advanceTimersByTime(120_000)
  })
  expect(instances()).toHaveLength(1)
})

it('errors from the connection closed on hide neither reconnect, count failures nor toast', () => {
  mount()
  const staleOnError = instances()[0].onerror!
  act(() => setVisibility('hidden'))
  act(() => {
    for (let i = 0; i < 5; i += 1) staleOnError()
    vi.advanceTimersByTime(120_000)
  })
  expect(instances()).toHaveLength(1)
  expect(toast).not.toHaveBeenCalled()
})

it('does not connect when mounted in a hidden tab', () => {
  visibility = 'hidden'
  mount()
  expect(instances()).toHaveLength(0)
  act(() => setVisibility('visible'))
  expect(instances()).toHaveLength(1)
})

it('does not refresh quote queries on resume when realtime is disabled', () => {
  mount(false)
  act(() => setVisibility('hidden'))
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  act(() => setVisibility('visible'))
  expect(instances()).toHaveLength(2)
  expect(invalidate).not.toHaveBeenCalled()
})
