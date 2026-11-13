// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  updatePushChannels: vi.fn(),
  pushLog: vi.fn(),
  feishuAppChats: vi.fn(),
  prefs: {} as Record<string, unknown>,
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: { ...actual.api, updatePushChannels: mocks.updatePushChannels, pushLog: mocks.pushLog, feishuAppChats: mocks.feishuAppChats },
  }
})
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: mocks.prefs }) }))

import { PushChannelsSection, PushLogSection } from './PushSettings'
import { isServerChanUrl } from '@/lib/pushChannels'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const flush = async () => { for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

async function render(node: React.ReactNode) {
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
  })
  await flush()
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.prefs = {
    feishu_app_credentials_set: true, feishu_app_chat_id: 'oc_zen', feishu_app_configured: true,
    feishu_webhook_url: '', wecom_webhook_url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=••••abcd',
    push_channels: ['feishu'], push_channels_active: ['feishu'],
  }
  mocks.updatePushChannels.mockResolvedValue({ push_channels: ['feishu', 'wecom'], push_channels_active: ['feishu', 'wecom'] })
  mocks.feishuAppChats.mockResolvedValue({ credentials_set: true, chats: [] })
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

it('one tick per channel decides where every push goes, and says where pushes go now', async () => {
  await render(<PushChannelsSection />)
  expect(host.textContent).toContain('全部推送')
  expect(host.textContent).toContain('开盘放量、监控规则')
  expect(host.querySelector('[data-push-active]')?.textContent).toBe('当前发往：飞书')
  // a single feishu checkbox (app bot + group webhook are one channel)
  expect(host.querySelectorAll('input[aria-label="飞书"]').length).toBe(1)
  const boxes = [...host.querySelectorAll('[data-push-channels] input[type="checkbox"]')] as HTMLInputElement[]
  const wecom = boxes.find(b => b.closest('div')?.textContent?.includes('群推送 Webhook') && !b.checked)
  expect(wecom).toBeTruthy()
  await act(async () => { wecom!.click() })
  await flush()
  expect(mocks.updatePushChannels).toHaveBeenCalledWith(['feishu', 'wecom'])
  expect(host.textContent).toContain('只收消息，不参与推送')
})

it('labels a Server酱 address as Server酱 in the third-party row', async () => {
  mocks.prefs = { ...mocks.prefs, custom_webhook_url: 'https://sctapi.ftqq.com/••••abcd' }
  await render(<PushChannelsSection />)
  expect(host.querySelector('[data-custom-kind]')?.textContent).toBe('Server酱')
  expect(isServerChanUrl('https://123.push.ft07.com/send/x.send')).toBe(true)
  expect(isServerChanUrl('https://ftqq.com.evil.example/x')).toBe(false)
  expect(isServerChanUrl('https://example.com/hook')).toBe(false)
})

it('warns when no channel can send', async () => {
  mocks.prefs = { ...mocks.prefs, push_channels: [], push_channels_active: [] }
  await render(<PushChannelsSection />)
  expect(host.querySelector('[data-push-active]')?.textContent).toContain('当前没有可用渠道')
})

it('push log is collapsed by default; opened, it lists each message with per-channel results and the original text', async () => {
  mocks.pushLog.mockResolvedValue({
    total: 2, failed: 1, active_channels: ['feishu', 'wecom'],
    items: [
      { key: 'trade:7:c1:2026-11-12', kind: 'trade', kind_label: '成交', title: 'ZenTrade · 纸面买入', subtitle: '2026-11-12',
        body: '**AAPL** · 回踩', status: 'failed', label: '失败', attempts: 1, exhausted: false,
        created_at: 1794500000, sent_at: null, last_error: 'wecom:send_failed', delivered: ['feishu'],
        channels: [
          { channel: 'feishu', label: '飞书', ok: true, tries: 1, at: 1794500010, error: null, legacy: false },
          { channel: 'wecom', label: '企业微信', ok: false, tries: 1, at: 1794500010, error: 'send_failed', legacy: false },
        ] },
      { key: 'daily:2026-11-11', kind: 'daily', kind_label: '收盘', title: 'ZenTrade · 收盘', subtitle: '2026-11-11',
        body: '无新卡', status: 'sent', label: '成功', attempts: 1, exhausted: false,
        created_at: 1794400000, sent_at: 1794400030, last_error: null, delivered: [],
        channels: [{ channel: 'feishu', label: '飞书', ok: true, tries: 1, at: 1794400030, error: null, legacy: true }] },
    ],
  })
  await render(<PushLogSection />)
  // collapsed by default: header shows counts only, the list is neither rendered nor fetched
  const toggle = host.querySelector('[data-push-log-toggle]') as HTMLButtonElement
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(toggle.textContent).toContain('共 2 条 · 失败 1')
  expect(toggle.textContent).toContain('展开')
  expect(host.querySelector('[data-push-log-row]')).toBeNull()
  expect(host.textContent).not.toContain('只看未成功')
  expect(mocks.pushLog).toHaveBeenCalledWith({ limit: 1 })
  expect(mocks.pushLog).not.toHaveBeenCalledWith({ limit: 100, failed: false })

  await act(async () => { toggle.click() })
  await flush()
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  expect(toggle.textContent).toContain('收起')
  expect(mocks.pushLog).toHaveBeenCalledWith({ limit: 100, failed: false })
  expect(host.textContent).toContain('共 2 条 · 失败 1')
  const row = host.querySelector('[data-push-log-row="trade:7:c1:2026-11-12"]')!
  expect(row.textContent).toContain('成交')
  expect(row.textContent).toContain('纸面买入')
  expect(row.textContent).toContain('✓ 飞书')
  expect(row.textContent).toContain('✗ 企业微信')
  await act(async () => { (row.querySelector('button') as HTMLButtonElement).click() })
  expect(row.textContent).toContain('失败（对方未受理）')
  expect(row.querySelector('pre')?.textContent).toBe('**AAPL** · 回踩')
  const legacy = host.querySelector('[data-push-log-row="daily:2026-11-11"]')!
  await act(async () => { (legacy.querySelector('button') as HTMLButtonElement).click() })
  expect(legacy.textContent).toContain('推送日志上线前的记录')

  const onlyFailed = [...host.querySelectorAll('button')].find(b => b.textContent === '只看未成功') as HTMLButtonElement
  await act(async () => { onlyFailed.click() })
  await flush()
  expect(mocks.pushLog).toHaveBeenLastCalledWith({ limit: 100, failed: true })

  // collapsing again hides the list
  await act(async () => { toggle.click() })
  await flush()
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(host.querySelector('[data-push-log-row]')).toBeNull()
})
