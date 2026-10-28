// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it } from 'vitest'
import { OptionsHeatmap, heatSentence } from './OptionsHeatmap'
import type { OptionsHeatmap as HM } from '@/lib/api'

const h: HM = {
  expirations: ['2026-10-23', '2026-10-30'], spot: 101, call_share: 0.7, stance: '偏多',
  resistance: 110, support: 90, call_focus: [110, 120],
  rows: [
    { strike: 110, cells: [{ call_oi: 3000, put_oi: 100, call_vol: 5, put_vol: 1, bias: 0.94, oi: 3100 }, null] },
    { strike: 90, cells: [{ call_oi: 100, put_oi: 900, call_vol: 1, put_vol: 2, bias: -0.8, oi: 1000 }, null] },
  ],
}

it('一句结论 + 现价线 + 点格子看明细', async () => {
  expect(heatSentence(h)).toBe('偏多：看涨期权集中在 $110–$120 · 上方 $110 压力 / 下方 $90 支撑')
  const host = document.createElement('div'); document.body.appendChild(host)
  await act(async () => { createRoot(host).render(<OptionsHeatmap h={h} />) })
  expect(host.querySelector('[data-heat-spot]')!.textContent).toContain('现价 $101')
  expect(host.querySelector('[data-heat-detail]')).toBeNull()
  await act(async () => { (host.querySelector('[data-heat-cell="110|2026-10-23"]') as HTMLButtonElement).click() })
  expect(host.querySelector('[data-heat-detail]')!.textContent).toContain('看涨 3,000 张')
})
