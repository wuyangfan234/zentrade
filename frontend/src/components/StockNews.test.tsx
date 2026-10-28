// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { NewsItem } from '@/lib/api'

const mocks = vi.hoisted(() => ({ stockNews: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { NewsBadge, StockNewsPanel } from './StockNews'

const item = (over: Partial<NewsItem>): NewsItem => ({
  id: 'alpaca:1', symbol: 'LITE', published_at: '2026-10-02T14:00:00Z', age_hours: 3, headline: '华盛顿瞄准中国光模块，Lumentum 股价大涨',
  summary: '光模块股集体上涨。', original_headline: 'Lumentum Rips Higher As Washington Targets China', translated: true, tagged: true,
  tag_mode: 'llm', relevance: 'primary', event_type: 'regulatory', event_label: '监管/调查', direction: 'bullish', importance: 'high',
  rehash: false, mention: 'headline', scope: 'focused', sources: ['Benzinga', 'Finnhub'], url: 'https://example.com/a', other_symbols: [],
  ...over,
})

let qc: QueryClient
let root: Root
let host: HTMLDivElement

async function render(node: React.ReactNode) {
  await act(async () => { root.render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>) })
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear(); vi.resetAllMocks() })

it('shows only main news in Chinese by default, with direction colour and AI disclaimer', async () => {
  mocks.stockNews.mockResolvedValue({
    symbol: 'LITE', days: 7, disclaimer: '事件类型、方向与重要度为 AI 判读，未经验证',
    summary: { count_24h: 1, high_bearish_48h: false, high_bullish_48h: true, top: [] },
    items: [
      item({}),
      item({ id: 'alpaca:2', headline: '顺带一提的行业综述', relevance: 'passing' }),
      item({ id: 'alpaca:3', headline: 'Untranslated headline', translated: false, tagged: false, mention: 'headline',
             relevance: null, direction: null, importance: null, event_label: null, summary: '' }),
    ],
  })
  await render(<StockNewsPanel symbol="LITE" />)
  expect(mocks.stockNews).toHaveBeenCalledWith('LITE', 7)
  const text = host.textContent ?? ''
  expect(text).toContain('主角新闻 2 条')
  expect(text).toContain('华盛顿瞄准中国光模块')
  expect(text).not.toContain('顺带一提的行业综述')
  expect(text).toContain('（未翻译）')                 // 无译文时如实标注, 不冒充中文
  expect(text).toContain('未打标')
  expect(text).toContain('AI 判读')
  const dir = host.querySelector('[data-news-direction="bullish"]') as HTMLElement
  expect(dir.textContent).toBe('偏利好')
  expect(dir.className).toContain('text-bull')          // 红=利好 (红涨口径)

  // 展开全部
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('显示全部 3 条')) as HTMLButtonElement
  await act(async () => { btn.click() })
  expect(host.textContent).toContain('顺带一提的行业综述')

  // 点标题展开摘要与原文链接
  const title = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('华盛顿瞄准中国光模块')) as HTMLButtonElement
  await act(async () => { title.click() })
  expect(host.textContent).toContain('光模块股集体上涨')
  expect(host.textContent).toContain('原文：Lumentum Rips Higher')
  expect((host.querySelector('a[href="https://example.com/a"]') as HTMLAnchorElement).target).toBe('_blank')
})

it('explains an empty list without inventing news', async () => {
  mocks.stockNews.mockResolvedValue({ symbol: 'AAOI', days: 7, disclaimer: 'x', items: [],
    summary: { count_24h: 0, high_bearish_48h: false, high_bullish_48h: false, top: [] } })
  await render(<StockNewsPanel symbol="AAOI" />)
  expect(host.textContent).toContain('近 7 日没有点名该股的新闻')
})

it('news badge hides when empty and turns bearish-coloured on high bearish', async () => {
  await render(<div><NewsBadge count={0} /><NewsBadge count={3} /><NewsBadge count={1} highBearish /></div>)
  const badges = host.querySelectorAll('[data-news-badge]')
  expect(badges).toHaveLength(2)
  expect(badges[0].textContent).toBe('3')
  expect((badges[1] as HTMLElement).className).toContain('text-bear')
})
