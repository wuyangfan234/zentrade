// @vitest-environment jsdom
// 策略池挪到服务器: 首次对账规则 + 对账前的增删不丢 + 之后以服务器为准 + 服务器读不到时退回本机。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { reconcilePool, useStrategyPool } from './useStrategyPool'
import { storage } from '@/lib/storage'

const toast = vi.fn()
vi.mock('@/components/Toast', () => ({ toast: (...a: unknown[]) => toast(...a) }))

let serverGet: () => Promise<{ pool: string[]; saved: boolean }>
const serverSave = vi.fn(async (pool: string[]) => ({ pool, saved: true }))
vi.mock('@/lib/api', () => ({
  api: {
    strategyPoolGet: () => serverGet(),
    strategyPoolSave: (pool: string[]) => serverSave(pool),
  },
}))

type Hook = ReturnType<typeof useStrategyPool>
let hook: Hook
let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

function Probe() {
  hook = useStrategyPool()
  return null
}

async function mount() {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>)
  })
}
const flush = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })

beforeEach(() => {
  localStorage.clear()
  toast.mockClear()
  serverSave.mockClear()
  serverSave.mockImplementation(async (pool: string[]) => ({ pool, saved: true }))
  serverGet = async () => ({ pool: [], saved: false })
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
})

it('对账规则 (纯函数)', () => {
  // 服务器从未保存: 本机有池 → 迁上去; 本机也空 → 用建议, 不保存
  expect(reconcilePool({ pool: ['x'], saved: false }, ['a', 'b'], false)).toEqual({ pool: ['a', 'b'], push: true })
  expect(reconcilePool({ pool: ['x', 'y'], saved: false }, [], false)).toEqual({ pool: ['x', 'y'], push: false })
  // 服务器已保存, 本机第一次对账: 本机独有的并到后面
  expect(reconcilePool({ pool: ['a', 'b'], saved: true }, ['b', 'c'], false)).toEqual({ pool: ['a', 'b', 'c'], push: true })
  expect(reconcilePool({ pool: ['a', 'b'], saved: true }, ['b', 'a'], false)).toEqual({ pool: ['a', 'b'], push: false })
  // 本机对过账之后: 以服务器为准, 别的设备删掉的不会被本机旧副本带回来
  expect(reconcilePool({ pool: ['a'], saved: true }, ['a', 'c'], true)).toEqual({ pool: ['a'], push: false })
  // 服务器清空过 (saved 且为空) ≠ 从未保存
  expect(reconcilePool({ pool: [], saved: true }, ['a'], true)).toEqual({ pool: [], push: false })
})

it('旧设备第一次打开: 本机的池迁到服务器, 并落下已对账标记', async () => {
  storage.strategyPool.set(['strong_open', 'boll_breakout'])
  await mount()
  await flush()
  expect(hook.pool).toEqual(['strong_open', 'boll_breakout'])
  expect(hook.ready).toBe(true)
  expect(serverSave).toHaveBeenCalledTimes(1)
  expect(serverSave).toHaveBeenCalledWith(['strong_open', 'boll_breakout'])
  expect(storage.strategyPoolSynced.get(false)).toBe(true)
})

it('新设备 (本机为空) 打开: 直接用服务器的池, 不回写', async () => {
  serverGet = async () => ({ pool: ['a', 'b'], saved: true })
  await mount()
  await flush()
  expect(hook.pool).toEqual(['a', 'b'])
  expect(storage.strategyPool.get([])).toEqual(['a', 'b'])
  expect(serverSave).not.toHaveBeenCalled()
})

it('服务器从未保存且本机为空: 显示建议池但不保存, 也不落已对账标记', async () => {
  serverGet = async () => ({ pool: ['strong_open'], saved: false })
  await mount()
  await flush()
  expect(hook.pool).toEqual(['strong_open'])
  expect(serverSave).not.toHaveBeenCalled()
  expect(storage.strategyPoolSynced.get(false)).toBe(false)
})

it('对过账的设备以服务器为准: 别的设备删掉的策略不会回来', async () => {
  storage.strategyPool.set(['a', 'gone'])
  storage.strategyPoolSynced.set(true)
  serverGet = async () => ({ pool: ['a'], saved: true })
  await mount()
  await flush()
  expect(hook.pool).toEqual(['a'])
  expect(serverSave).not.toHaveBeenCalled()
})

it('对账前的增删先作用在本机, 服务器到了之后在服务器的池上重放并保存', async () => {
  let release!: (v: { pool: string[]; saved: boolean }) => void
  serverGet = () => new Promise(r => { release = r })
  storage.strategyPoolSynced.set(true)
  storage.strategyPool.set(['a'])
  await mount()
  act(() => { hook.addToPool('new') })
  expect(hook.pool).toEqual(['a', 'new'])
  expect(serverSave).not.toHaveBeenCalled()                         // 还没对账, 不拿本机旧副本覆盖服务器
  await act(async () => { release({ pool: ['a', 'b'], saved: true }) })
  await flush()
  expect(hook.pool).toEqual(['a', 'b', 'new'])
  expect(serverSave).toHaveBeenCalledTimes(1)
  expect(serverSave).toHaveBeenCalledWith(['a', 'b', 'new'])
})

it('对账后的增删、排序、清理都保存到服务器; 没有变化不保存', async () => {
  serverGet = async () => ({ pool: ['a', 'b', 'c'], saved: true })
  await mount()
  await flush()
  act(() => { hook.addToPool('a') })                                 // 已在池里
  act(() => { hook.removeFromPool('zzz') })                          // 不在池里
  act(() => { hook.prune(new Set(['a', 'b', 'c', 'd'])) })           // 没有失效项
  expect(serverSave).not.toHaveBeenCalled()

  act(() => { hook.removeFromPool('b') })
  expect(serverSave).toHaveBeenLastCalledWith(['a', 'c'])
  act(() => { hook.reorderPool(['c', 'a']) })
  expect(serverSave).toHaveBeenLastCalledWith(['c', 'a'])
  act(() => { hook.prune(new Set(['a'])) })
  expect(serverSave).toHaveBeenLastCalledWith(['a'])
  await flush()
  expect(hook.pool).toEqual(['a'])
  expect(hook.isInPool('a')).toBe(true)
  expect(storage.strategyPool.get([])).toEqual(['a'])
  expect(serverSave).toHaveBeenCalledTimes(3)
})

it('保存失败: 提示原因, 本机的池保持不变', async () => {
  serverGet = async () => ({ pool: ['a'], saved: true })
  await mount()
  await flush()
  serverSave.mockImplementationOnce(async () => { throw new Error('网络错误') })
  act(() => { hook.addToPool('b') })
  await flush()
  expect(toast).toHaveBeenCalledWith('策略池保存失败 · 网络错误', 'error')
  expect(hook.pool).toEqual(['a', 'b'])
})

it('服务器读不到: 退回本机副本, ready 仍置真 (不卡住自动扫描), 不向服务器写', async () => {
  storage.strategyPool.set(['a'])
  serverGet = async () => { throw new Error('offline') }
  await mount()
  await flush()
  await flush()
  expect(hook.ready).toBe(true)
  expect(hook.pool).toEqual(['a'])
  expect(serverSave).not.toHaveBeenCalled()
})

it('旧分钟池仍并入统一池 (历史迁移不丢)', async () => {
  storage.strategyPool.set(['d1'])
  storage.strategyPoolMinute.set(['m1', 'd1'])
  await mount()
  expect(hook.pool).toEqual(['d1', 'm1'])
  expect(localStorage.getItem('strategy-pool-1m')).toBeNull()
})
