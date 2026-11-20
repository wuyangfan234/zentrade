// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  prefs: {} as Record<string, unknown>,
  updateFullMarketDiscovery: vi.fn(),
  updateRealtimeMonitorConfig: vi.fn(),
}))

vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: mocks.prefs }),
  useVersion: () => ({ data: { version: '0.57.1', revision: 'abc1234' } }),
}))

vi.mock('@/lib/updateCheck', () => ({
  useUpdateCheck: () => ({
    status: 'idle',
    info: null,
    check: vi.fn(),
    enabled: false,
    error: null,
    repo: '',
  }),
}))

vi.mock('./PushSettings', () => ({
  PushChannelsSection: () => null,
  PushLogSection: () => null,
}))

vi.mock('@/lib/api', () => ({
  api: {
    updateFullMarketDiscovery: (...a: unknown[]) => mocks.updateFullMarketDiscovery(...a),
    updateRealtimeMonitorConfig: (...a: unknown[]) => mocks.updateRealtimeMonitorConfig(...a),
  },
}))

import { SettingsSystemPanel } from './System'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const flush = async () => {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

async function renderPanel() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SettingsSystemPanel />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await flush()
}

describe('SettingsSystemPanel 全市场扫描与看盘总开关', () => {
  beforeEach(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
    mocks.prefs = {
      full_market_discovery_enabled: true,
      screener_auto_run: true,
      realtime_quotes_enabled: true,
    }
    mocks.updateFullMarketDiscovery.mockReset().mockResolvedValue({ full_market_discovery_enabled: false })
    mocks.updateRealtimeMonitorConfig.mockReset().mockResolvedValue({})
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    qc.clear()
    vi.clearAllMocks()
  })

  it('默认开启，点击开关调用 PUT full-market-discovery', async () => {
    await renderPanel()
    expect(host.textContent).toContain('全市场盘中实时扫描与看盘遗留')
    const switches = Array.from(host.querySelectorAll('button[role="switch"]'))
    expect(switches[0].getAttribute('aria-checked')).toBe('true')
    await act(async () => {
      switches[0].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await flush()
    expect(mocks.updateFullMarketDiscovery).toHaveBeenCalledWith(false)
  })

  it('关闭时禁用策略页自动运行开关并显示关闭提示', async () => {
    mocks.prefs.full_market_discovery_enabled = false
    await renderPanel()
    const switches = Array.from(host.querySelectorAll('button[role="switch"]')) as HTMLButtonElement[]
    expect(switches[0].getAttribute('aria-checked')).toBe('false')
    expect(switches[1].getAttribute('aria-checked')).toBe('false')
    expect(switches[1].disabled).toBe(true)
    expect(host.textContent).toContain('全市场扫描已关闭')
  })
})
