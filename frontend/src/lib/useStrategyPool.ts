import { useState, useCallback, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type StrategyPoolPayload } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { toast } from '@/components/Toast'

// 旧版按周期隔离的双池 ('strategy-pool' 日线 + 'strategy-pool-1m' 分钟) 已合并为
// 统一池 ('strategy-pool'): 策略自带周期声明, 执行时按各自 timeframes 路由,
// 池不再按周期隔离。此处做一次性迁移: 日线在前、分钟在后、按 ID 去重,
// 完成后移除旧分钟 key 保证幂等 (StrictMode 双调用 / HMR 重放均安全)。
function loadLocalPool(): string[] {
  const minute = storage.strategyPoolMinute.get([])
  const daily = storage.strategyPool.get([])
  if (minute.length === 0) {
    storage.strategyPoolMinute.remove()
    return daily
  }
  const merged = [...daily]
  for (const id of minute) {
    if (!merged.includes(id)) merged.push(id)
  }
  storage.strategyPool.set(merged)
  storage.strategyPoolMinute.remove()
  return merged
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

/**
 * 本机的池与服务器的池第一次对账 (纯函数)。push = 对账结果要不要写回服务器。
 * - 服务器从未保存过: 本机有池就迁上去; 本机也没有, 用服务器给的建议 (最近扫描过的策略), 先不保存。
 * - 服务器已保存、本机还没对过账: 本机独有的策略并到服务器的池后面 (换设备不丢旧池)。
 * - 本机对过账之后: 一律以服务器为准 (别的设备删掉的策略不会被本机旧副本带回来)。
 */
export function reconcilePool(server: StrategyPoolPayload, local: string[], synced: boolean): { pool: string[]; push: boolean } {
  if (!server.saved) {
    return local.length > 0 ? { pool: local, push: true } : { pool: server.pool, push: false }
  }
  if (synced) return { pool: server.pool, push: false }
  const extra = local.filter(id => !server.pool.includes(id))
  return extra.length > 0 ? { pool: [...server.pool, ...extra], push: true } : { pool: server.pool, push: false }
}

type PoolOp = (prev: string[]) => string[]

/**
 * 策略池 — 日线与分钟策略共用的统一池, 保存在服务器 (各设备一致)。
 * 首屏先用本机副本秒出, 服务器返回后对账 (见 reconcilePool); 对账前的增删先作用在本机并排队,
 * 对账后在服务器的池上重放, 不会被覆盖。服务器读不到时退回只用本机副本 (ready 仍会置真, 不卡住页面)。
 */
export function useStrategyPool() {
  const qc = useQueryClient()
  const [pool, setPoolState] = useState<string[]>(loadLocalPool)
  const [ready, setReady] = useState(false)
  const poolRef = useRef(pool)
  const initialRef = useRef(pool)
  const readyRef = useRef(false)
  const queueRef = useRef<PoolOp[]>([])
  const seqRef = useRef(0)
  const inflightRef = useRef(0)

  const server = useQuery({ queryKey: QK.strategyPool, queryFn: api.strategyPoolGet, retry: 1 })

  const commit = useCallback((next: string[]) => {
    poolRef.current = next
    setPoolState(next)
    storage.strategyPool.set(next)
  }, [])

  const push = useCallback((next: string[]) => {
    const seq = ++seqRef.current
    inflightRef.current += 1
    api.strategyPoolSave(next).then(
      res => { if (seq === seqRef.current) qc.setQueryData(QK.strategyPool, res) },
      e => toast(`策略池保存失败 · ${String((e as { message?: string })?.message ?? e)}`, 'error'),
    ).finally(() => { inflightRef.current -= 1 })
  }, [qc])

  // 服务器的池到了: 第一次做对账, 之后 (别的设备改过、重新取回) 直接采用
  useEffect(() => {
    const data = server.data
    if (!data) return
    if (!readyRef.current) {
      const r = reconcilePool(data, initialRef.current, storage.strategyPoolSynced.get(false))
      const next = queueRef.current.reduce((p, op) => op(p), r.pool)
      queueRef.current = []
      readyRef.current = true
      setReady(true)
      commit(next)
      const changed = !same(next, r.pool)
      // 建议池 (服务器未保存且本机为空) 不落标记: 下次进来本机已有副本, 走迁移分支存上去
      if (data.saved || r.push || changed) storage.strategyPoolSynced.set(true)
      if (r.push || changed) push(next)
      return
    }
    if (inflightRef.current === 0 && data.saved && !same(data.pool, poolRef.current)) commit(data.pool)
  }, [server.data, commit, push])

  // 服务器读不到: 只用本机副本, 不卡住依赖 ready 的自动扫描
  useEffect(() => {
    if (!server.isError || readyRef.current) return
    readyRef.current = true
    queueRef.current = []
    setReady(true)
  }, [server.isError])

  const apply = useCallback((op: PoolOp) => {
    const next = op(poolRef.current)
    if (next === poolRef.current || same(next, poolRef.current)) return
    commit(next)
    if (readyRef.current) push(next)
    else queueRef.current.push(op)
  }, [commit, push])

  const addToPool = useCallback((id: string) => {
    apply(prev => (prev.includes(id) ? prev : [...prev, id]))
  }, [apply])

  const removeFromPool = useCallback((id: string) => {
    apply(prev => (prev.includes(id) ? prev.filter(x => x !== id) : prev))
  }, [apply])

  const reorderPool = useCallback((newOrder: string[]) => {
    apply(() => newOrder)
  }, [apply])

  // 清除池中不存在于 validIds 的失效策略(如本地开发残留的自定义策略)。
  // 调用方传入"全周期合并的策略列表" ID, 池内日线/分钟策略一并校验。
  // 仅当确实有失效项时才更新,避免无谓重渲染与无谓保存。
  const prune = useCallback((validIds: Iterable<string>) => {
    const validSet = validIds instanceof Set ? validIds : new Set(validIds)
    apply(prev => {
      if (prev.length === 0) return prev
      const next = prev.filter(id => validSet.has(id))
      return next.length === prev.length ? prev : next
    })
  }, [apply])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, ready, addToPool, removeFromPool, reorderPool, prune, isInPool }
}
