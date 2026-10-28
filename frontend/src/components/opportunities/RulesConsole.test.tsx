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
