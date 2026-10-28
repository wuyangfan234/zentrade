// @vitest-environment jsdom
// bughunt-1015 B1: 截图 OCR 后端早已删除 (P1-5), 旧弹窗仍收截图并 POST /api/watchlist/import-image → 405 必失败。
// 现在: 截图不发请求、直接提示下线; CSV 仍走 import-csv。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  watchlistImportCsv: vi.fn(),
  toast: vi.fn(),
}))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, watchlistImportCsv: mocks.watchlistImportCsv } }
})
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))

import { api } from '@/lib/api'
import { WatchlistImportDialog } from './WatchlistImportDialog'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear(); vi.resetAllMocks() })

async function open() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <WatchlistImportDialog open onClose={() => {}} groups={[]} existingBySymbol={new Map()} />
      </QueryClientProvider>,
    )
  })
}

async function drop(files: File[]) {
  const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).toBeTruthy()
  Object.defineProperty(input, 'files', { value: files, configurable: true })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

it('no longer offers or calls the removed screenshot OCR endpoint', async () => {
  expect((api as Record<string, unknown>).watchlistImportImage).toBeUndefined()
  await open()
  const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
  expect(input.accept).not.toMatch(/image|png|jpe?g/)
  expect(document.body.textContent).not.toContain('截图')
  await drop([new File(['x'], 'shot.png', { type: 'image/png' })])
  expect(mocks.watchlistImportCsv).not.toHaveBeenCalled()
  expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('截图识别已下线'), 'error')
})

it('still imports a broker CSV through import-csv', async () => {
  mocks.watchlistImportCsv.mockResolvedValue({
    candidates: [{ code: 'NVDA', symbol: 'NVDA', name: 'NVIDIA', matched: true, already_in_watchlist: false }],
    matched_count: 1,
  })
  await open()
  await drop([new File(['NVDA'], 'list.csv', { type: 'text/csv' })])
  expect(mocks.watchlistImportCsv).toHaveBeenCalledTimes(1)
  expect(document.body.textContent).toContain('NVIDIA')
})
