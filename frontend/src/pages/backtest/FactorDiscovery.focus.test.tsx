// @vitest-environment jsdom
// 审计复现 (先红): 编辑器保存后「去检验 uf_x」—— 因子列表缓存早于新因子时, 聚焦被吞掉、退化成全选
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'

const mocks = vi.hoisted(() => ({ factorColumns: vi.fn(), watchlistList: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})
import { FactorDiscovery } from './FactorDiscovery'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
let root: Root
let host: HTMLDivElement
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(() => { act(() => root.unmount()); host.remove(); vi.clearAllMocks() })
const tick = async () => { for (let i = 0; i < 10; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) }) }

const OLD = [
  { id: 'momentum_5d', label: '5日动量', group: '动量', desc: '' },
  { id: 'momentum_20d', label: '20日动量', group: '动量', desc: '' },
]

it('focus on a just-saved factor survives a stale cached column list', async () => {
  mocks.watchlistList.mockResolvedValue({ symbols: [] })
  mocks.factorColumns.mockResolvedValue({ columns: [...OLD, { id: 'uf_new', label: '新因子', group: '自定义', desc: '' }] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5_000 } } })
  // the editor tab populated this cache before the save; it is stale by the time 检验 mounts
  qc.setQueryData(QK.factorColumns, { columns: OLD }, { updatedAt: Date.now() - 60_000 })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><FactorDiscovery focusFactor="uf_new" /></QueryClientProvider>)
  })
  await tick()
  expect(host.textContent).toContain('已选 1 / 3')
})
