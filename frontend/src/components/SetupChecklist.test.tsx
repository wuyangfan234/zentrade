// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { SetupChecklist, SetupItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SetupBanner, SetupChecklistCard } from './SetupChecklist'

let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })

const item = (over: Partial<SetupItem>): SetupItem => ({
  id: 'x', group: 'market', label: 'X', level: 'optional', configured: false, source: '', purpose: '用途',
  impact: '不配的影响', where: 'settings?tab=data-sources', env: [], link: '', note: '', warn: '', ...over,
})

function data(items: SetupItem[], dismissed = false): SetupChecklist {
  const req = items.filter(i => i.level === 'required' && !i.configured).map(i => i.id)
  const rec = items.filter(i => i.level === 'recommended' && !i.configured).map(i => i.id)
  return { items, summary: { required_missing: req, recommended_missing: rec, ready: !req.length }, dismissed }
}

async function render(node: React.ReactNode, d: SetupChecklist) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(QK.setupChecklist, d)
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
  })
}

const alpaca = item({ id: 'alpaca', label: 'Alpaca（美股行情）', level: 'required' })
const ai = item({ id: 'ai', group: 'ai', label: 'AI 模型（全局）', level: 'recommended' })
const news = item({ id: 'ai_news', group: 'ai', label: '新闻翻译模型', level: 'recommended', warn: '用量大：建议低价、关思考的模型' })

it('必需项缺失: 首页横幅常驻且不可关闭', async () => {
  await render(<SetupBanner requiredOnly />, data([alpaca, ai]))
  expect(host.textContent).toContain('未配置：Alpaca（美股行情）')
  expect(host.querySelector('button[aria-label="关闭提示"]')).toBeNull()
})

it('只缺建议项: 首页 (requiredOnly) 不打扰, 其他页可关闭', async () => {
  await render(<SetupBanner requiredOnly />, data([{ ...alpaca, configured: true }, ai]))
  expect(host.textContent).toBe('')
  await render(<SetupBanner />, data([{ ...alpaca, configured: true }, ai]))
  expect(host.textContent).toContain('建议配置：AI 模型（全局）')
  expect(host.querySelector('button[aria-label="关闭提示"]')).not.toBeNull()
})

it('建议项已关闭后不再出现', async () => {
  await render(<SetupBanner />, data([{ ...alpaca, configured: true }, ai], true))
  expect(host.textContent).toBe('')
})

it('清单卡: 分组、影响说明、新闻翻译用量提示、Finnhub / SEC 可直接填写', async () => {
  const fh = item({ id: 'finnhub', group: 'news', label: 'Finnhub', level: 'recommended', env: ['FINNHUB_API_KEY'] })
  const sec = item({ id: 'sec_ua', group: 'news', label: 'SEC EDGAR 联系邮箱', level: 'recommended' })
  await render(<SetupChecklistCard />, data([alpaca, fh, sec, ai, news]))
  const text = host.textContent ?? ''
  for (const s of ['行情数据', '新闻与基本面', 'AI 模型', '不配的影响', '低价、关思考', 'FINNHUB_API_KEY']) expect(text).toContain(s)
  expect(host.querySelector('input[aria-label="Finnhub API Key"]')?.getAttribute('type')).toBe('password')
  expect(host.querySelector('input[aria-label="ZenTrade you@example.com"]')).not.toBeNull()
})
