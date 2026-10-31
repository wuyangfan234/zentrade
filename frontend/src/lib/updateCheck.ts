/**
 * 应用更新检查 — 单例 store (useSyncExternalStore)。
 *
 * 侧栏左下角版本号徽标与 设置→系统 的「检查更新」共用同一状态与网络请求。
 * 0.44.0 起改为问本服务的 GET /api/settings/update-check: 由服务端查上游仓库
 * (GitHub Release, 没有 Release 时读默认分支 VERSION), 服务端缓存 6 小时。
 * 好处: 浏览器不直连 GitHub (国内网络、CORS、按访客 IP 的限额都不再是问题);
 * 离线部署在 .env 设 UPDATE_CHECK_ENABLED=false 即全站不查、不显示入口。
 *
 * 前端再按天缓存一次 (localStorage), 避免每次打开页面都打后端; 失败不落缓存。
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { api, type UpdateCheckResult } from '@/lib/api'
import { useVersion } from '@/lib/useSharedQueries'

const CACHE_KEY = 'update_check_cache_v2'
const CACHE_TTL_MS = 24 * 60 * 60 * 1000

export type UpdateStatus = 'idle' | 'checking' | 'latest' | 'found' | 'error' | 'disabled'

export interface UpdateInfo {
  latest: string
  url: string
  notes?: string
  source?: 'release' | 'version_file'
}

interface UpdateState {
  status: UpdateStatus
  info: UpdateInfo | null
  checkedAt: number
  error?: string
  repo?: string
}

let state: UpdateState = { status: 'idle', info: null, checkedAt: 0 }
const listeners = new Set<() => void>()

function setState(next: Partial<UpdateState>): void {
  state = { ...state, ...next }
  listeners.forEach((fn) => fn())
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function getUpdateState(): UpdateState {
  return state
}

/** 测试用: 复位单例 */
export function resetUpdateState(): void {
  state = { status: 'idle', info: null, checkedAt: 0 }
  inFlight = false
  silentStarted = false
}

/** 语义版本比较: a<b → -1, 相等 → 0, a>b → 1; 无法解析按 0.0.0 处理 */
export function compareVersion(a: string, b: string): number {
  const parse = (v: string) =>
    (v.trim().replace(/^v/i, '').match(/\d+/g) ?? []).slice(0, 3).map(Number)
  const pa = parse(a)
  const pb = parse(b)
  for (let i = 0; i < 3; i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (d !== 0) return Math.sign(d)
  }
  return 0
}

interface CacheBlob {
  current: string
  result: UpdateCheckResult
  checkedAt: number
}

function readCache(): CacheBlob | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as Partial<CacheBlob>
    if (typeof v.current !== 'string' || typeof v.checkedAt !== 'number' || !v.result || typeof v.result.status !== 'string') {
      return null
    }
    return v as CacheBlob
  } catch {
    return null
  }
}

function writeCache(blob: CacheBlob): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(blob))
  } catch {
    // 隐私模式/存储满: 只影响下次多一次请求
  }
}

/** 服务端结果 → 前端状态。是否有新版由前端按本地版本再比一次 (防缓存跨版本)。 */
function applyResult(r: UpdateCheckResult, current: string, checkedAt: number): void {
  if (r.status === 'disabled') {
    setState({ status: 'disabled', info: null, checkedAt, repo: r.repo })
    return
  }
  if (r.status === 'error' || !r.latest) {
    setState({ status: 'error', info: null, checkedAt, error: r.error, repo: r.repo })
    return
  }
  const info: UpdateInfo = { latest: r.latest, url: r.url ?? '', notes: r.notes ?? '', source: r.source }
  const found = compareVersion(current || r.current, r.latest) < 0
  setState({ status: found ? 'found' : 'latest', info, checkedAt, error: undefined, repo: r.repo })
}

let inFlight = false

export async function checkForUpdate(current: string, opts: { force?: boolean } = {}): Promise<void> {
  const cur = current.trim().replace(/^v/i, '')
  if (!cur || inFlight) return
  if (!opts.force) {
    const c = readCache()
    if (c && c.current === cur && Date.now() - c.checkedAt < CACHE_TTL_MS && c.result.status !== 'error') {
      applyResult(c.result, cur, c.checkedAt)
      return
    }
  }
  inFlight = true
  setState({ status: 'checking' })
  const checkedAt = Date.now()
  try {
    const r = await api.updateCheck(!!opts.force)
    applyResult(r, cur, checkedAt)
    if (r.status !== 'error') writeCache({ current: cur, result: r, checkedAt })
  } catch {
    setState({ status: 'error', checkedAt })
  } finally {
    inFlight = false
  }
}

let silentStarted = false

/**
 * 侧栏与设置页共用的更新检查 hook。
 * 版本号就绪后自动做一次静默检查 (每次会话最多一次); `check` 为手动入口, 绕过缓存立即刷新。
 */
export function useUpdateCheck(): {
  status: UpdateStatus
  info: UpdateInfo | null
  checkedAt: number
  error?: string
  repo?: string
  check: () => void
  /** 服务端未关闭检查 (UPDATE_CHECK_ENABLED) 时才展示入口 */
  enabled: boolean
} {
  const s = useSyncExternalStore(subscribe, getUpdateState)
  const { data: versionData } = useVersion()
  const current = (versionData?.version ?? '').trim()
  useEffect(() => {
    if (!current || silentStarted) return
    silentStarted = true
    void checkForUpdate(current)
  }, [current])
  const check = useCallback(() => {
    void checkForUpdate(current, { force: true })
  }, [current])
  return { ...s, check, enabled: s.status !== 'disabled' }
}
