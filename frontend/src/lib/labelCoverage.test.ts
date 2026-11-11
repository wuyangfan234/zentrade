// @vitest-environment jsdom
/**
 * 守门: 后端的定时任务 id、台账原因码必须在前端有中文名, 否则页面会把英文标识直接显示出来
 * (系统状态页曾露出 zen_rank_lab, 持仓台账曾露出 entry_window_expired)。
 * 做法: 直接读后端源码取全集, 与前端映射表做差。
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { REASON_LABEL, corpActionLabel, reasonLabel } from './reasonLabels'
import { JOB_LABEL } from '@/pages/SystemStatus'

const BACKEND = resolve(process.cwd(), '../backend/app')
const hasBackend = existsSync(BACKEND)

function pyFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    if (name === '__pycache__') continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...pyFiles(p))
    else if (name.endsWith('.py')) out.push(p)
  }
  return out
}

function dictKeys(src: string, name: string): string[] {
  const at = src.indexOf(`${name} = {`)
  if (at < 0) return []
  const body = src.slice(at, src.indexOf('\n}', at))
  return [...body.matchAll(/["']([a-z][a-z0-9_]*)["']\s*:/g)].map((m) => m[1])
}

describe.skipIf(!hasBackend)('后端标识在前端都有中文名', () => {
  it('定时任务 id 全部有名称', () => {
    const ids = new Set<string>()
    for (const f of pyFiles(BACKEND)) {
      const src = readFileSync(f, 'utf8')
      if (!src.includes('add_job')) continue
      for (const m of src.matchAll(/\bid\s*=\s*["']([a-z][a-z0-9_]*)["']/g)) ids.add(m[1])
      // for job_id, hour, minute in (("zen_earnings_premarket", 8, 30), ...) 这种循环注册
      for (const m of src.matchAll(/\(\s*["'](zen_[a-z0-9_]+)["']\s*,\s*\d+\s*,\s*\d+\s*\)/g)) ids.add(m[1])
    }
    expect(ids.size).toBeGreaterThan(30)
    const missing = [...ids].filter((id) => !JOB_LABEL[id]).sort()
    expect(missing).toEqual([])
  })

  it('台账原因码全部有短词', () => {
    const codes = new Set<string>()
    const read = (rel: string) => readFileSync(join(BACKEND, rel), 'utf8')
    for (const k of dictKeys(read('api/portfolio.py'), 'EXIT_REASON_LABEL')) codes.add(k)
    for (const k of dictKeys(read('services/entry_funnel.py'), 'REASON_LABEL')) codes.add(k)
    for (const f of pyFiles(BACKEND)) {
      const src = readFileSync(f, 'utf8')
      for (const m of src.matchAll(/cancel_pending\([^)]*?["']([a-z][a-z0-9_]*)["']/g)) codes.add(m[1])
    }
    expect(codes.size).toBeGreaterThan(15)
    const missing = [...codes].filter((c) => !REASON_LABEL[c]).sort()
    expect(missing).toEqual([])
  })
})

describe('映射不到的码不直接上屏', () => {
  it('未知英文原因码显示「其他」, 中文自由文本原样显示', () => {
    expect(reasonLabel('entry_window_expired')).toBe('入场窗到期')
    expect(reasonLabel('some_new_code')).toBe('其他')
    expect(reasonLabel('some_new_code', '后端文案')).toBe('后端文案')
    expect(reasonLabel('5个交易日入场窗结束')).toBe('5个交易日入场窗结束')
    expect(reasonLabel('trailing_1.2R')).toBe('浮盈 1.2R')
    expect(reasonLabel(null)).toBe('')
  })

  it('公司行动类型不露英文', () => {
    expect(corpActionLabel('forward_split')).toBe('拆股')
    expect(corpActionLabel('unknown_kind')).toBe('公司行动')
    expect(corpActionLabel(undefined)).toBe('公司行动')
  })
})
