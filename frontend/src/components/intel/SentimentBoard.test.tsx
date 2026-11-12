// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { IntelSentiment, SentimentRow } from '@/lib/api'
import { SentimentBoard, fmtEt } from './SentimentBoard'

let host: HTMLDivElement
let root: Root
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })
function render(node: React.ReactNode) { act(() => root.render(node)) }

const SOURCES = {
  gdelt: { status: 'ok' as const }, stocktwits: { status: 'ok' as const }, apewisdom: { status: 'missing' as const },
  reddit: { status: 'unconfigured' as const }, wiki: { status: 'ok' as const },
}
const rowA: SentimentRow = {
  symbol: 'NVDA', role: '池', grade: 'A', heat: 62, direction: 'bearish', event_type: 'management', hard: true,
  first_at: '2026-11-08T20:51:22Z', n_sources: 3, n_families: 2, families_ok: ['news', 'media', 'attention'],
  families: {
    news: { z: 9, heat: 89, anom: true, sources: { news_hi: { x: 9, med: 0, mad: 0, z: 9, n: 20, anom: true } } },
    attention: { z: 5.1, heat: 72, anom: true, sources: { wiki: { x: 50000, med: 9000, mad: 800, z: 5.1, n: 20, anom: true } } },
    media: { z: 0.4, heat: 10, anom: false, sources: { gdelt: { x: 300, med: 280, mad: 40, z: 0.4, n: 20, anom: false } } },
  },
  missing: ['social'],
  headlines: [{ at: '2026-11-08T21:20:36Z', zh: '董事会将展开调查', en: 'Board probe', importance: 'high', direction: 'bearish', event_type: 'management' }],
  line: [['2026-11-08T20:00:00Z', 5], ['2026-11-08T21:00:00Z', 20], ['2026-11-08T22:00:00Z', 62]],
}
const rowB: SentimentRow = {
  symbol: 'MU', role: '自选', grade: 'B', heat: 45, direction: 'mixed', event_type: 'earnings', hard: true,
  first_at: null, n_sources: 1, n_families: 1, families_ok: ['news'],
}
const base = (rows: SentimentRow[], counts = { A: 1, B: 1, C: 4 }): IntelSentiment => ({
  as_of: '2026-11-09T01:00:00Z', age_min: 3, rows, counts, symbols: 31, families: { news: 31 }, sources: SOURCES, heat_line: 40, window_h: 24,
})

describe('SentimentBoard', () => {
  it('A 级行: 评级、方向、热度、类型、首发、来源数; 点开看热度线、来源分布、中文标题', () => {
    render(<SentimentBoard data={base([rowA, rowB])} />)
    const row = document.querySelector('[data-sentiment-row="NVDA"]') as HTMLElement
    expect(row).toBeTruthy()
    expect(row.querySelector('[data-grade="A"]')?.textContent).toBe('A')
    expect(row.textContent).toContain('偏空')
    expect(row.textContent).toContain('62')
    expect(row.textContent).toContain('管理层')
    expect(row.textContent).toContain('11-08 15:51')            // 20:51Z = 美东 15:51 (11-01 起冬令时 UTC-5)
    expect(document.querySelector('[data-intel="sentiment"] h2')?.textContent).toBe('舆情 · A 1 · B 1 · C 4')
    expect(document.querySelector('[data-sentiment-detail]')).toBeNull()
    act(() => row.click())
    const det = document.querySelector('[data-sentiment-detail="NVDA"]') as HTMLElement
    expect(det.querySelector('[data-sentiment-line] polyline')?.getAttribute('points')?.split(' ').length).toBe(3)
    expect(det.textContent).toContain('财经新闻')
    expect(det.querySelector('[data-family-missing="social"]')?.textContent).toBe('社交未计入')
    expect(det.textContent).toContain('董事会将展开调查')
    expect(det.textContent).not.toMatch(/bearish|management|news_hi|attention/)   // 不露英文枚举
  })

  it('B 级行: 分歧方向, 没有首发时显示占位', () => {
    render(<SentimentBoard data={base([rowB], { A: 0, B: 1, C: 0 })} />)
    const row = document.querySelector('[data-sentiment-row="MU"]') as HTMLElement
    expect(row.querySelector('[data-grade="B"]')).toBeTruthy()
    expect(row.textContent).toContain('分歧')
    expect(row.textContent).toContain('财报')
    expect(row.textContent).toContain('—')
  })

  it('空: 无 A/B 级时只给 C 计数与各源状态', () => {
    render(<SentimentBoard data={base([], { A: 0, B: 0, C: 2 })} />)
    expect(document.querySelector('[data-sentiment-state="empty"]')?.textContent).toBe('无 A/B 级 · C 级 2 只')
    const src = document.querySelector('[data-sentiment-sources]')?.textContent ?? ''
    expect(src).toContain('Reddit 未配置')
    expect(src).toContain('ApeWisdom 缺源')
    expect(document.querySelector('[data-sentiment-row]')).toBeNull()
  })

  it('读取失败: 本段报错只空这一段', () => {
    render(<SentimentBoard data={null} failed />)
    expect(document.querySelector('[data-sentiment-state="failed"]')?.textContent).toBe('读取失败')
    expect(document.querySelector('[data-sentiment-row]')).toBeNull()
  })

  it('首轮前: 待首轮', () => {
    render(<SentimentBoard data={{ ...base([]), as_of: null }} />)
    expect(document.querySelector('[data-sentiment-state="pending"]')?.textContent).toBe('待首轮')
  })

  it('时间按美东显示', () => {
    expect(fmtEt('2026-07-01T13:30:00Z')).toBe('07-01 09:30')   // 夏令时 UTC-4
    expect(fmtEt(null)).toBe('—')
  })
})
