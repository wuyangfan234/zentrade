// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { DataSourcesResponse } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: { daily_data_provider: 'alpaca' } }) }))
import { SettingsDataSourcesPanel } from './DataSources'

let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })

const alpaca = (available: boolean) => ({
  name: 'alpaca', display_name: 'Alpaca (美股)', datasets: ['daily'], runtime: 'none', available,
  status: available ? 'ok' : '未配置 Alpaca Key (KEY_ID:SECRET)', description: '', install_hint: '',
  homepage: 'https://alpaca.markets', api_key_env: 'ZENTRADE_ALPACA_KEY', api_key_masked: available ? 'PKFA••••••0001' : '',
})

async function render(available: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, enabled: false } } })
  qc.setQueryData(QK.dataSources, { builtin: [], plugins: [alpaca(available)], custom: [], errors: [], config_dir: '' } as unknown as DataSourcesResponse)
  qc.setQueryData(QK.capabilityMatrix, { capabilities: [] })
  qc.setQueryData(QK.setupChecklist, { items: [], summary: { required_missing: [], recommended_missing: [], ready: true }, dismissed: false })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter><SettingsDataSourcesPanel /></MemoryRouter></QueryClientProvider>)
  })
}

it('Alpaca 详情可以在页面填写 Key (未配置)', async () => {
  await render(false)
  const box = host.querySelector('[data-testid="alpaca-key-config"]')!
  expect(box).not.toBeNull()
  const input = box.querySelector('input[type="password"]') as HTMLInputElement
  expect(input).not.toBeNull()
  expect(input.placeholder).toBe('KEY_ID:SECRET')
  expect(box.textContent).toContain('ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY')
  expect(box.textContent).toContain('未配置')
  expect(box.textContent).not.toMatch(/未配置\s*未配置/)                 // 状态不重复
  expect(host.querySelector('[data-testid="alpaca-routed-no-key"]')).not.toBeNull()  // 路由在用但无 Key: 不显示「服务中」
  expect(host.textContent).not.toContain('服务中 1 项能力')
})

it('已配置时显示脱敏 Key ID 和替换入口', async () => {
  await render(true)
  const box = host.querySelector('[data-testid="alpaca-key-config"]')!
  expect(box.textContent).toContain('已配置')
  expect(box.textContent).toContain('PKFA••••••0001')
  expect((box.querySelector('input') as HTMLInputElement).placeholder).toBe('粘贴新 Key 替换当前')
})
