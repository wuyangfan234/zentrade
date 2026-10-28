// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { CapabilityRoute } from '@/lib/api'
import { CAP_LABELS } from '@/lib/capability-labels'
import { CapabilityCard } from './DataSources'

let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove() })

const cap = (over: Partial<CapabilityRoute>): CapabilityRoute => ({
  id: 'daily', label: '日K', desc: '历史日线', field: 'daily_data_provider', default: '', tf_tier: 'free',
  tf_available: true, usable: true, current: 'alpaca', current_display: 'Alpaca', effective: 'alpaca',
  effective_display: 'Alpaca', candidates: [], pending: [], ...over,
})

async function render(c: CapabilityRoute) {
  await act(async () => { root.render(<CapabilityCard cap={c} pendingKey={null} onSelect={() => {}} />) })
}

it('财务数据源可用时不再显示「美股暂无可用源」', async () => {
  await render(cap({ id: 'financial', label: '财务数据', desc: '财务指标与三大报表', field: 'financial_data_provider', usable: true, current: 'sec_edgar' }))
  expect(host.textContent).not.toContain('美股暂无可用源')
  expect(host.querySelector('[data-testid="cap-no-us-source"]')).toBeNull()
})

it('财务数据源不可用时标注「财务源不可用」', async () => {
  await render(cap({ id: 'financial', label: '财务数据', desc: '财务指标与三大报表', field: 'financial_data_provider', usable: false }))
  expect(host.querySelector('[data-testid="cap-no-us-source"]')?.textContent).toBe('财务源不可用')
})

it('其他能力不显示该标注', async () => {
  await render(cap({}))
  expect(host.querySelector('[data-testid="cap-no-us-source"]')).toBeNull()
})

it('五档盘口能力标签已删除', () => {
  expect(Object.keys(CAP_LABELS).filter(k => k.startsWith('depth5'))).toEqual([])
})
