// @vitest-environment jsdom
// AI 模型分工: 思考档位 (2026-10-19) —— 下拉可选 关/低/中/高/极高/模型默认, 「实际」显示真正发出去的档位
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AiTaskRow } from '@/lib/api'

const mocks = vi.hoisted(() => ({ aiTasks: vi.fn(), saveAiTask: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, ...mocks } }
})

import { AiTasksCard } from './AI'

let qc: QueryClient
let root: Root
let host: HTMLDivElement

const row = (task: string, label: string, effective: Partial<AiTaskRow['effective']>, thinking = '', def: 'default' | 'off' = 'default'): AiTaskRow => ({
  task, label, default_thinking: def,
  override: { model: '', base_url: '', thinking, has_api_key: false, api_key_masked: '' },
  effective: { model: 'grok-4.7', base_url: 'http://gw/v1', thinking: 'default', overridden: thinking ? ['thinking'] : [], ...effective },
})

async function flush() {
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function render(tasks: AiTaskRow[]) {
  mocks.aiTasks.mockResolvedValue({ tasks })
  await act(async () => {
    root.render(<QueryClientProvider client={qc}><MemoryRouter><AiTasksCard /></MemoryRouter></QueryClientProvider>)
  })
  await flush()
}

const effectiveText = (task: string) =>
  host.querySelector(`[data-ai-task="${task}"] [data-ai-task-effective]`)?.textContent ?? ''

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  qc.clear()
  vi.clearAllMocks()
})

it('offers graded thinking levels instead of only on/off', async () => {
  await render([row('stock_analysis', '个股分析', { reasoning_effort: '', route: 'api' })])
  const select = host.querySelector('[data-ai-task="stock_analysis"] select') as HTMLSelectElement
  expect([...select.options].map(o => o.value)).toEqual(['', 'off', 'low', 'medium', 'high', 'xhigh', 'default'])
  expect([...select.options].map(o => o.textContent)).toEqual([
    '思考：按任务默认（模型默认）', '思考：关', '思考：低', '思考：中', '思考：高', '思考：极高', '思考：模型默认（不指定档位）',
  ])
})

it('saves the chosen level for that task only', async () => {
  mocks.saveAiTask.mockResolvedValue({ tasks: [row('stock_analysis', '个股分析', { thinking: 'high', reasoning_effort: 'high', route: 'api' }, 'high')] })
  await render([row('stock_analysis', '个股分析', { reasoning_effort: '', route: 'api' })])
  const card = host.querySelector('[data-ai-task="stock_analysis"]') as HTMLElement
  const select = card.querySelector('select') as HTMLSelectElement
  await act(async () => {
    select.value = 'high'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const save = [...card.querySelectorAll('button')].find(b => b.textContent === '保存') as HTMLButtonElement
  await act(async () => { save.click() })
  await flush()
  expect(mocks.saveAiTask).toHaveBeenCalledWith('stock_analysis', { model: '', base_url: '', thinking: 'high', api_key: null })
  expect(effectiveText('stock_analysis')).toContain('思考 高')
  expect(effectiveText('stock_analysis')).toContain('已单独配置')
})

it('shows the level actually sent, not a generic "default" label', async () => {
  await render([
    row('news_translate', '新闻翻译与打标', { thinking: 'off', reasoning_effort: 'none', route: 'api' }, '', 'off'),
    row('stock_analysis', '个股分析', { thinking: 'xhigh', reasoning_effort: 'xhigh', route: 'api' }, 'xhigh'),
    row('assistant', '助手对话', { reasoning_effort: '', route: 'api' }),
    row('strategy_gen', '策略生成', { thinking: 'high', reasoning_effort: '', route: 'codex_cli', model: 'gpt-5.6-sol' }, 'high'),
  ])
  expect(effectiveText('news_translate')).toContain('思考 关')
  expect(effectiveText('stock_analysis')).toContain('思考 极高')
  expect(effectiveText('assistant')).toContain('思考 模型默认')
  expect(effectiveText('strategy_gen')).toContain('gpt-5.6-sol · Codex 推理档')
  // 下拉回显已保存的档位
  const sel = host.querySelector('[data-ai-task="stock_analysis"] select') as HTMLSelectElement
  expect(sel.value).toBe('xhigh')
})

it('falls back to the old on/off label when the backend has no reasoning_effort field', async () => {
  await render([
    { ...row('assistant', '助手对话', {}), effective: { model: 'm', base_url: '', thinking: 'default', overridden: [] } },
    { ...row('news_translate', '新闻翻译与打标', {}, '', 'off'), effective: { model: 'm', base_url: '', thinking: 'off', overridden: [] } },
  ])
  expect(effectiveText('assistant')).toContain('默认思考')
  expect(effectiveText('news_translate')).toContain('关思考')
})
