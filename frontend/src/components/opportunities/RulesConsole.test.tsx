// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { RadarPushEditor } from './RulesConsole'

let root: Root
let host: HTMLDivElement

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

it('describes global channel routing instead of Feishu-only delivery', async () => {
  await act(async () => {
    root.render(<RadarPushEditor
      cur={{ enabled: true, min_rvol: 3, levels: [], period: '', other: 0 }}
      enabled={true}
      minRvol={3}
      onEnabled={() => {}}
      onMinRvol={() => {}}
    />)
  })

  const description = host.querySelector('[data-radar-push]')?.textContent ?? ''
  expect(description).toContain('09:51 ET 发布后推送')
  expect(description).toContain('每渠道每日 1 条')
  expect(description).not.toContain('出榜后推送飞书')
})

// 0.52.0 排序因子: 右侧数字来自 ZenTrade 复核 (全窗), 悬停写挖掘段 / 留出段与公式; 没有候选池时写原因
it('ranker rows show replay numbers with segments, and say why when the pool is missing', async () => {
  const { historyCell } = await import('./RulesConsole')
  const seg = { n: 300, mean_r: 0.3, excess: 0.08, excess_ci: [0.01, 0.15], passed: true, window: ['2023-01-03', '2025-06-30'] }
  const cell = historyCell({
    id: 'rank_a1b2c3d4e5', name: '排序 · 二十日动量', history: null, kind: 'ranker',
    formula: 'ts_delta(close,20)/ts_delay(close,20)', direction: 'high',
    candidate: { passed: true, n: 620, mean_r: 0.31, excess: 0.07, excess_ci: [0.01, 0.13],
      segments: { mining: seg, holdout: { ...seg, n: 320, excess: 0.05, window: ['2025-07-01', '2026-11-06'] } } },
  })
  expect(cell.left).toBe('620 · +0.31R')
  expect(cell.title).toContain('挖掘段 2023-01 ~ 2025-06 300 笔')
  expect(cell.title).toContain('留出段 2025-07 起 320 笔')
  expect(cell.title).toContain('值大优先')
  const none = historyCell({ id: 'rank_x', name: 'x', history: null, kind: 'ranker', candidate: { passed: false, error: 'no_pool' } })
  expect(none.left).toContain('候选池未导出')
  const fresh = historyCell({ id: 'rank_y', name: 'y', history: null, kind: 'ranker', candidate: null })
  expect(fresh.left).toBe('未复核')
})

// 分段月份跟着复核结果走 (2026-11-10: 后端留出段改成 2025-07 起, 悬停还写死「2023–2024 / 2025 起」)
it('ranker segment labels follow the windows in the payload instead of hardcoded years', async () => {
  const { historyCell, segLabel, segSpan } = await import('./RulesConsole')
  const base = { n: 100, mean_r: 0.1, excess: 0.02, excess_ci: [-0.01, 0.05], passed: false }
  const row = (mining: string[] | undefined, holdout: string[] | undefined) => historyCell({
    id: 'rank_a1b2c3d4e5', name: 'x', history: null, kind: 'ranker', formula: 'close/prev_close', direction: 'high',
    candidate: { passed: false, n: 200, mean_r: 0.1, excess: 0.02, excess_ci: [-0.01, 0.05],
      segments: { mining: { ...base, window: mining }, holdout: { ...base, window: holdout } } },
  }).title
  const old = row(['2023-01-03', '2024-12-31'], ['2025-01-02', '2026-11-06'])
  expect(old).toContain('挖掘段 2023-01 ~ 2024-12 100 笔')
  expect(old).toContain('留出段 2025-01 起 100 笔')
  const cur = row(['2023-01-03', '2025-06-30'], ['2025-07-01', '2026-11-06'])
  expect(cur).toContain('挖掘段 2023-01 ~ 2025-06 100 笔')
  expect(cur).toContain('留出段 2025-07 起 100 笔')
  // 旧载荷没有 window: 只写段名, 不猜年份
  const bare = row(undefined, undefined)
  expect(bare).toContain('挖掘段 100 笔')
  expect(bare).toContain('留出段 100 笔')
  expect(bare).not.toMatch(/20\d\d[–-]20\d\d|2025 起/)
  expect(segLabel('holdout', null)).toBe('留出段')
  expect(segLabel('mining', { window: ['2023-01-03'] })).toBe('挖掘段')
  expect(segSpan('holdout', { window: ['2025-07-01', '2026-11-06'] })).toBe('2025-07 起')
  expect(segSpan('holdout', undefined)).toBe('')
})

it('candidate badge title names the holdout start from the payload', async () => {
  const { candidateBadgeTitle } = await import('./RulesConsole')
  const c = { passed: true, n: 620, mean_r: 0.31, excess: 0.07, excess_ci: [0.01, 0.13] }
  expect(candidateBadgeTitle('ranker', { ...c, segments: { mining: null, holdout: { n: 320, window: ['2025-07-01', '2026-11-06'] } } }))
    .toBe('排序因子 · 全窗与留出段（2025-07 起）的 95% 区间下限都 > 0: 每笔超额 +0.07R（区间 +0.01R ~ +0.13R），620 笔')
  // 旧载荷没有 window: 不写月份
  expect(candidateBadgeTitle('ranker', c)).toBe('排序因子 · 全窗与留出段的 95% 区间下限都 > 0: 每笔超额 +0.07R（区间 +0.01R ~ +0.13R），620 笔')
  // 挖掘 / AI 策略的文案不变
  expect(candidateBadgeTitle('custom', { ...c, excess_ci: null })).toBe('候选 · 回放超额为正（对比 4h 同向随机入场）: 每笔超额 +0.07R（区间 —），620 笔')
})
