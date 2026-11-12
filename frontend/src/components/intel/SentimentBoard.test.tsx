// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { IntelSentiment, SentimentRow } from '@/lib/api'
import { SentimentBoard, fmtCount, fmtEt, fmtTimes, reason } from './SentimentBoard'

let host: HTMLDivElement
let root: Root
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })
function render(node: React.ReactNode) { act(() => root.render(node)) }
const q = (sel: string) => document.querySelector(sel) as HTMLElement | null

const st = (x: number, med: number, anom: boolean, extra = {}) => ({ x, med, mad: 1, z: anom ? 5 : 0.3, n: 20, anom, ...extra })
const hi = (x: number, med: number) => ({ ...st(x, med, true), z: 9 })
const focus: SentimentRow = {
  symbol: 'NVDA', role: '持仓', grade: 'A', heat: 90, direction: 'bearish', event_type: 'management', hard: true,
  first_at: '2026-11-08T20:51:22Z', n_families: 3, families_ok: ['news', 'social', 'attention'],
  families: {
    news: { z: 9, heat: 89, anom: true, sources: { news: st(192, 26, true), news_hi: hi(51, 1) } },
    social: { z: 6, heat: 78, anom: true, sources: { reddit: st(31, 2, true) } },
    attention: { z: 14, heat: 97, anom: true, sources: { wiki: st(341625, 7528.5, true, { article: 'Jensen_Huang', day: '2026-11-08' }) } },
  },
  headlines: [{ at: '2026-11-08T21:20:36Z', zh: '董事会将展开调查', en: 'Board probe', importance: 'high', direction: 'bearish', event_type: 'management' }],
  posts: [{ at: '2026-11-08T21:01:00Z', title: 'Jensen’s official apology letter', sub: 'wallstreetbets', id: 'abc123' }],
}
const watch: SentimentRow = {
  symbol: 'MU', role: '池', grade: 'B', heat: 45, direction: 'mixed', event_type: 'earnings', hard: true,
  first_at: null, n_families: 1, families_ok: ['news', 'attention'],
  families: { news: { z: 4, heat: 63, anom: true, sources: { news: st(40, 12, true) } },
              attention: { z: 0.2, heat: 5, anom: false, sources: { wiki: st(9000, 8800, false) } } },
}
const base = (rows: SentimentRow[]): IntelSentiment => ({
  as_of: '2026-11-09T01:00:00Z', age_min: 3, rows, total: rows.length, symbols: 31, sources: { reddit: 'ok', wiki: 'ok' },
})

describe('SentimentBoard', () => {
  it('卡片只放代码、重点、利多利空、事件、变多的来源是平时几倍; 不出现字母评级与英文枚举', () => {
    render(<SentimentBoard data={base([focus, watch])} />)
    expect(q('[data-intel="sentiment"] h2')?.textContent).toBe('舆情异动 · 2 只')
    const c = q('[data-sentiment-card="NVDA"]')!
    expect(c.querySelector('[data-focus]')?.textContent).toBe('重点')
    expect(c.textContent).toContain('持仓')
    expect(c.textContent).toContain('利空')
    expect(c.textContent).toContain('管理层事件')
    expect(c.querySelector('[data-card-times]')?.textContent).toBe('重要新闻 51 倍Reddit 16 倍维基浏览 45 倍')
    const m = q('[data-sentiment-card="MU"]')!
    expect(m.querySelector('[data-focus]')).toBeNull()
    expect(m.textContent).toContain('多空都有')
    expect(m.textContent).not.toContain('池')                         // 「池」是内部说法, 不上卡
    expect(m.querySelector('[data-card-times]')?.textContent).toBe('新闻 3.3 倍')   // 维基 1.0 倍 没变多, 不列
    const text = q('[data-intel="sentiment"]')!.textContent ?? ''
    expect(text).not.toMatch(/\bA\b|\bB\b|bearish|management|σ|热度/)
  })

  it('点开卡片: 一句话原因 + 采集到的新闻、Reddit 帖子、维基浏览量与平时对比', () => {
    render(<SentimentBoard data={base([focus])} onSymbol={() => {}} />)
    act(() => q('[data-sentiment-card="NVDA"]')!.click())
    const s = q('[data-sentiment-sheet="NVDA"]')!
    expect(s.querySelector('[data-sentiment-reason]')?.textContent).toBe('新闻、Reddit 讨论、维基浏览量明显多于平时；新闻以利空为主。')
    expect(s.querySelector('[data-sentiment-block="news"]')?.textContent).toContain('近 24 小时 192 条，平时约 26 条')
    expect(s.querySelector('[data-news-important]')?.textContent).toBe('其中重要消息 51 条，平时约 1 条')
    expect(s.textContent).toContain('董事会将展开调查')
    const reddit = s.querySelector('[data-sentiment-block="social"]')!
    expect(reddit.textContent).toContain('近 24 小时 31 帖，平时约 2 帖')
    expect(reddit.querySelector('a')?.getAttribute('href')).toBe('https://www.reddit.com/comments/abc123')
    expect(s.querySelector('[data-sentiment-block="attention"]')?.textContent).toContain('11-08 全天 34万 次，平时约 7,529 次')
    expect(s.textContent).toContain('条目：Jensen Huang')
    expect(s.textContent).toContain('仅观察，不参与下单')
  })

  it('某类没有数据: 明细里写「暂无数据」; 没有方向时也给中性标签, 卡片排版一致', () => {
    render(<SentimentBoard data={base([{ ...watch, direction: 'none', event_type: null }])} />)
    const c = q('[data-sentiment-card="MU"]')!
    expect(c.textContent).toContain('方向不明')
    expect(c.textContent).toContain('无明确事件')
    act(() => q('[data-sentiment-card="MU"]')!.click())
    expect(q('[data-sentiment-block="social"]')?.textContent).toBe('Reddit 讨论暂无数据')
  })

  it('空 / 首次采集 / 读取失败', () => {
    render(<SentimentBoard data={base([])} />)
    expect(q('[data-sentiment-state="empty"]')?.textContent).toBe('近 24 小时没有明显异动')
    render(<SentimentBoard data={{ ...base([]), as_of: null }} />)
    expect(q('[data-sentiment-state="pending"]')?.textContent).toBe('首次采集中')
    render(<SentimentBoard data={null} failed />)
    expect(q('[data-sentiment-state="failed"]')?.textContent).toBe('读取失败')
    expect(q('[data-sentiment-card]')).toBeNull()
  })

  it('超过 8 只时折叠, 可展开', () => {
    const many = Array.from({ length: 10 }, (_, i) => ({ ...watch, symbol: `S${i}` }))
    render(<SentimentBoard data={base(many)} />)
    expect(document.querySelectorAll('[data-sentiment-card]').length).toBe(8)
    act(() => q('[data-sentiment-more]')!.click())
    expect(document.querySelectorAll('[data-sentiment-card]').length).toBe(10)
  })

  it('格式: 美东时间、万为单位、原因句', () => {
    expect(fmtEt('2026-07-01T13:30:00Z')).toBe('07-01 09:30')
    expect(fmtEt('2026-11-08T20:51:22Z')).toBe('11-08 15:51')
    expect(fmtCount(341625)).toBe('34万')
    expect(fmtCount(33238)).toBe('3.3万')
    expect(fmtCount(40043)).toBe('4万')
    expect(fmtTimes(51, 0)).toBe('51')
    expect(fmtTimes(40, 12)).toBe('3.3')
    expect(fmtTimes(20, 10)).toBe('2')
    expect(fmtCount(7528.5)).toBe('7,529')
    expect(reason({ ...watch, direction: 'none' })).toBe('新闻明显多于平时。')
  })
})
