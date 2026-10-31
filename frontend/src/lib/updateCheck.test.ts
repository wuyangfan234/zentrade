// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// useVersion 仅被 useUpdateCheck hook 使用; 打断 react-query 导入链, 单测只测命令式 API
vi.mock('@/lib/useSharedQueries', () => ({
  useVersion: () => ({ data: undefined }),
}))
const mocks = vi.hoisted(() => ({ updateCheck: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: mocks }))

import { checkForUpdate, compareVersion, getUpdateState, resetUpdateState } from './updateCheck'

const CACHE_KEY = 'update_check_cache_v2'
const DAY = 24 * 60 * 60 * 1000

const res = (over: Record<string, unknown> = {}) => ({
  enabled: true, repo: 'owner/zentrade', current: '0.3.2', status: 'update_available',
  latest: 'v0.9.0', url: 'https://github.com/owner/zentrade/releases/tag/v0.9.0', notes: 'n', source: 'release',
  checked_at: 1, ...over,
})

function seedCache(result: Record<string, unknown>, current = '0.3.2', checkedAt = Date.now() - 1000) {
  localStorage.setItem(CACHE_KEY, JSON.stringify({ current, result, checkedAt }))
}

beforeEach(() => {
  localStorage.clear()
  resetUpdateState()
  mocks.updateCheck.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('compareVersion', () => {
  it('按语义版本比较大小', () => {
    expect(compareVersion('0.3.2', '0.3.3')).toBe(-1)
    expect(compareVersion('0.4.0', '0.3.9')).toBe(1)
    expect(compareVersion('1.0.0', '1.0')).toBe(0)
  })

  it('容忍 v 前缀与非法输入 (按 0.0.0 处理)', () => {
    expect(compareVersion('0.3.2', 'v0.3.3')).toBe(-1)
    expect(compareVersion('v1.2.3', '1.2.3')).toBe(0)
    expect(compareVersion('', '0.0.1')).toBe(-1)
  })
})

describe('checkForUpdate (经后端 /api/settings/update-check)', () => {
  it('发现新版本: 状态 found 并带链接与说明, 结果落缓存', async () => {
    mocks.updateCheck.mockResolvedValue(res())
    await checkForUpdate('v0.3.2')
    expect(mocks.updateCheck).toHaveBeenCalledWith(false)
    expect(getUpdateState().status).toBe('found')
    expect(getUpdateState().info).toMatchObject({ latest: 'v0.9.0', notes: 'n', source: 'release' })
    expect(JSON.parse(localStorage.getItem(CACHE_KEY) ?? '{}')).toMatchObject({ current: '0.3.2' })
  })

  it('同版本 24h 内命中缓存, 不请求后端', async () => {
    seedCache(res({ latest: 'v0.3.3' }))
    await checkForUpdate('0.3.2')
    expect(mocks.updateCheck).not.toHaveBeenCalled()
    expect(getUpdateState().status).toBe('found')
    expect(getUpdateState().info?.latest).toBe('v0.3.3')
  })

  it('缓存过期或本地版本已变 → 重新请求', async () => {
    seedCache(res(), '0.3.2', Date.now() - DAY - 1000)
    mocks.updateCheck.mockResolvedValue(res({ status: 'latest', latest: 'v0.3.2' }))
    await checkForUpdate('0.3.2')
    expect(mocks.updateCheck).toHaveBeenCalledTimes(1)
    expect(getUpdateState().status).toBe('latest')

    resetUpdateState()
    seedCache(res({ latest: 'v0.3.2' }), '0.3.1')
    await checkForUpdate('0.3.2')
    expect(mocks.updateCheck).toHaveBeenCalledTimes(2)
  })

  it('本地已升级到与缓存最新版相同: 不再显示 found (前端按本地版本复核)', async () => {
    mocks.updateCheck.mockResolvedValue(res({ current: '0.3.2', latest: 'v0.9.0' }))
    await checkForUpdate('0.9.0')
    expect(getUpdateState().status).toBe('latest')
  })

  it('force 绕过新鲜缓存, 并把 force 传给后端', async () => {
    seedCache(res())
    mocks.updateCheck.mockResolvedValue(res({ status: 'latest', latest: 'v0.3.2' }))
    await checkForUpdate('0.3.2', { force: true })
    expect(mocks.updateCheck).toHaveBeenCalledWith(true)
    expect(getUpdateState().status).toBe('latest')
  })

  it('后端 error 或请求失败: 状态 error 且不落缓存', async () => {
    mocks.updateCheck.mockResolvedValue(res({ status: 'error', latest: undefined, error: '仓库不可访问 (私有或不存在)' }))
    await checkForUpdate('0.3.2')
    expect(getUpdateState().status).toBe('error')
    expect(getUpdateState().error).toContain('私有')
    expect(localStorage.getItem(CACHE_KEY)).toBeNull()

    resetUpdateState()
    mocks.updateCheck.mockRejectedValue(new Error('offline'))
    await checkForUpdate('0.3.2')
    expect(getUpdateState().status).toBe('error')
  })

  it('服务端关闭检查: 状态 disabled (界面隐藏入口)', async () => {
    mocks.updateCheck.mockResolvedValue({ enabled: false, repo: '', current: '0.3.2', status: 'disabled', checked_at: null })
    await checkForUpdate('0.3.2')
    expect(getUpdateState().status).toBe('disabled')
  })

  it('版本未知时空操作', async () => {
    await checkForUpdate('')
    expect(mocks.updateCheck).not.toHaveBeenCalled()
    expect(getUpdateState().status).toBe('idle')
  })
})
