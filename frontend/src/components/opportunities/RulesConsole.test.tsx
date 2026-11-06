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
  expect(description).toContain('09:51 ET 出榜后按全局推送设置发送')
  expect(description).toContain('每日每渠道至多一条')
  expect(description).not.toContain('出榜后推送飞书')
})

// 0.52.0 排序因子: 右侧数字来自 ZenTrade 复核 (全窗), 悬停写挖掘段 / 留出段与公式; 没有候选池时写原因
it('ranker rows show replay numbers with segments, and say why when the pool is missing', async () => {
  const { historyCell } = await import('./RulesConsole')
  const seg = { n: 300, mean_r: 0.3, excess: 0.08, excess_ci: [0.01, 0.15], passed: true }
  const cell = historyCell({
    id: 'rank_a1b2c3d4e5', name: '排序 · 二十日动量', history: null, kind: 'ranker',
    formula: 'ts_delta(close,20)/ts_delay(close,20)', direction: 'high',
    candidate: { passed: true, n: 620, mean_r: 0.31, excess: 0.07, excess_ci: [0.01, 0.13],
      segments: { mining: seg, holdout: { ...seg, n: 320, excess: 0.05 } } },
  })
  expect(cell.left).toBe('620 · +0.31R')
  expect(cell.title).toContain('挖掘段 2023–2024 300 笔')
  expect(cell.title).toContain('留出段 2025 起 320 笔')
  expect(cell.title).toContain('值大优先')
  const none = historyCell({ id: 'rank_x', name: 'x', history: null, kind: 'ranker', candidate: { passed: false, error: 'no_pool' } })
  expect(none.left).toContain('候选池未导出')
  const fresh = historyCell({ id: 'rank_y', name: 'y', history: null, kind: 'ranker', candidate: null })
  expect(fresh.left).toBe('未复核')
})
