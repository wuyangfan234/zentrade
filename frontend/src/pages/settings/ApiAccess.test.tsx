// @vitest-environment jsdom
// 设置 → API 与 MCP (0.44.0): 生成密钥只显示一次、列表只显示前缀、吊销、MCP 配置片段自动填入新密钥
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ApiKeyInfo } from '@/lib/api'

const mocks = vi.hoisted(() => ({ apiKeys: vi.fn(), createApiKey: vi.fn(), revokeApiKey: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { SettingsApiAccessPanel, mcpSnippets } from './ApiAccess'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const key = (over: Partial<ApiKeyInfo> = {}): ApiKeyInfo => ({
  id: 'k1', name: 'Claude Desktop', scope: 'full', prefix: 'zt_AbCdEfG', created_at: 1_793_000_000,
  last_used_at: null, expires_at: null, ...over,
})

async function flush() {
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function render(keys: ApiKeyInfo[]) {
  mocks.apiKeys.mockResolvedValue({ keys })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter><SettingsApiAccessPanel /></MemoryRouter></QueryClientProvider>)
  })
  await flush()
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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

it('empty state, then generating a key shows the token once and fills the MCP snippets', async () => {
  await render([])
  expect(host.querySelector('[data-api-keys-empty]')).not.toBeNull()
  const token = 'zt_' + 'x'.repeat(43)
  mocks.createApiKey.mockResolvedValue({ key: key({ scope: 'read', name: 'bot' }), token, keys: [key({ scope: 'read', name: 'bot' })] })
  const input = host.querySelector('[data-api-key-name]') as HTMLInputElement
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(input, 'bot')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => { (host.querySelector('[data-api-key-create]') as HTMLButtonElement).click() })
  await flush()
  expect(mocks.createApiKey).toHaveBeenCalledWith({ name: 'bot', scope: 'read', expires_days: null })
  expect(host.querySelector('[data-api-key-token]')?.textContent).toBe(token)
  expect(host.querySelector('[data-mcp-snippet="cc"]')?.textContent).toContain(`Bearer ${token}`)
  // 列表行只显示前缀
  const row = host.querySelector('[data-api-key-row="k1"]')!
  expect(row.textContent).toContain('zt_AbCdEfG…')
  expect(row.textContent).toContain('只读')
  expect(row.textContent).not.toContain(token)
})

it('create button disabled without a name; revoke asks for confirmation', async () => {
  await render([key()])
  expect((host.querySelector('[data-api-key-create]') as HTMLButtonElement).disabled).toBe(true)
  expect(host.querySelector('[data-api-key-row="k1"]')?.textContent).toContain('完全控制')
  mocks.revokeApiKey.mockResolvedValue({ keys: [] })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
  const btn = host.querySelector('[aria-label="吊销 Claude Desktop"]') as HTMLButtonElement
  await act(async () => { btn.click() })
  expect(mocks.revokeApiKey).not.toHaveBeenCalled()
  await act(async () => { btn.click() })
  await flush()
  expect(confirm).toHaveBeenCalledTimes(2)
  expect(mocks.revokeApiKey).toHaveBeenCalledWith('k1')
  expect(host.querySelector('[data-api-keys-empty]')).not.toBeNull()
})

it('mcp snippets use the given endpoint and a placeholder before a key exists', () => {
  const s = mcpSnippets('https://zt.example.com/api/mcp', '')
  expect(s.claudeCode).toBe('claude mcp add --transport http zentrade https://zt.example.com/api/mcp --header "Authorization: Bearer <你的密钥>"')
  expect(JSON.parse(s.json).mcpServers.zentrade).toEqual({
    type: 'http', url: 'https://zt.example.com/api/mcp', headers: { Authorization: 'Bearer <你的密钥>' },
  })
  expect(s.curl).toContain('https://zt.example.com/api/watchlist')
})
