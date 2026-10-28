// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ feishuAppChats: vi.fn(), updateFeishuApp: vi.fn(), sendTestWebhook: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { FeishuAppPanel } from './FeishuAppPanel'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

async function render(credentialsSet: boolean, chatId: string) {
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><FeishuAppPanel credentialsSet={credentialsSet} chatId={chatId} /></QueryClientProvider>)
  })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

const click = async (label: string) => {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent === label) as HTMLButtonElement
  await act(async () => { btn.click() })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.feishuAppChats.mockResolvedValue({ credentials_set: true, chats: [{ chat_id: 'oc_zen', name: 'ZenTrade' }] })
  mocks.updateFeishuApp.mockResolvedValue({ feishu_app_chat_id: 'oc_zen', feishu_app_configured: true })
  mocks.sendTestWebhook.mockResolvedValue({ ok: true, detail: '发送成功' })
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

it('without server credentials it says so and never asks the browser for a secret', async () => {
  await render(false, '')
  expect(host.textContent).toContain('服务端未配置飞书应用凭据')
  expect(host.querySelector('input[type="password"]')).toBeNull()
  expect(mocks.feishuAppChats).not.toHaveBeenCalled()
})

it('lists the groups the bot is in, saves the chosen chat id and sends a test through the app bot', async () => {
  await render(true, '')
  const select = host.querySelector('select[aria-label="推送目标群"]') as HTMLSelectElement
  expect([...select.options].map(o => o.textContent)).toContain('ZenTrade')
  await act(async () => {
    select.value = 'oc_zen'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await click('保存')
  expect(mocks.updateFeishuApp).toHaveBeenCalledWith('oc_zen')

  await render(true, 'oc_zen')
  expect(host.textContent).toContain('推送到 ZenTrade')
  await click('发送测试')
  expect(mocks.sendTestWebhook).toHaveBeenCalledWith('feishu_app')
  expect(host.textContent).toContain('发送成功')
})
