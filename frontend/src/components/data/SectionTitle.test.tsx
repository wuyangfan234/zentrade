// @vitest-environment jsdom
// P7 (2026-10-11): 同步历史里任务编号降权 —— 主行写状态与时间, 编号小号灰字放在次行, 仍可悬停查看/排查。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { HistoryRow } from './SectionTitle'
import { ActiveJobCard } from './ActiveJobCard'

let root: Root | null = null
afterEach(() => { if (root) { act(() => { root!.unmount() }); root = null }; document.body.innerHTML = '' })

function mount(node: React.ReactNode) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  act(() => { root!.render(node) })
  return host
}

const job = { id: '27843f912e', status: 'succeeded', stage: 'done', progress: 100, started_at: '2026-10-10T18:19:27Z', duration_s: 583, result: { daily_days: 1 }, error: null }

it('同步历史行: 主行是状态, 任务编号降为小号灰字', () => {
  const host = mount(<HistoryRow job={job} onClick={() => {}} />)
  const main = host.querySelector('[data-history-main]')!
  expect(main.textContent).toContain('完成')
  expect(main.textContent).not.toContain('27843f912e')
  const id = host.querySelector('[data-job-id]')!
  expect(id.textContent).toContain('27843f912e')
  expect(id.className).toContain('text-muted')
  expect(id.className).toContain('text-[10px]')
  expect(id.getAttribute('title')).toContain('任务编号')
})

it('失败任务主行写「失败」', () => {
  const host = mount(<HistoryRow job={{ ...job, status: 'failed', error: '后端重启' }} onClick={() => {}} />)
  expect(host.querySelector('[data-history-main]')!.textContent).toContain('失败')
})

it('进行中卡片: 编号不再与时长同等醒目', () => {
  const host = mount(<ActiveJobCard job={{ ...job, status: 'running', stage: 'daily', progress: 40, duration_s: null, log: [] } as never} />)
  const id = host.querySelector('[data-job-id]')!
  expect(id.textContent).toContain('27843f912e')
  expect(id.className).toContain('text-[10px]')
})

it('同步历史时间按美东显示并标 ET (与页面其余时间一致, 与浏览器时区无关)', () => {
  // 2026-10-10T18:19:27Z = 美东 10-10 14:19 (EDT), 新加坡 10-11 02:19
  const host = mount(<HistoryRow job={job} onClick={() => {}} />)
  const main = host.querySelector('[data-history-main]')!
  expect(main.textContent).toContain('10-10 14:19 ET')
  expect(main.textContent).not.toContain('02:19')
})
