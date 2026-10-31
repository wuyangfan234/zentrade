// @vitest-environment jsdom
// AI 模型分工: 思考档位 (2026-10-19) —— 下拉可选 关/低/中/高/极高/模型默认, 「实际」显示真正发出去的档位
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AiModelEntry, AiTaskRow } from '@/lib/api'

const mocks = vi.hoisted(() => ({ aiTasks: vi.fn(), saveAiTask: vi.fn(), aiModels: vi.fn() }))
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

const thinkingSelect = (task: string) =>
  host.querySelector(`[data-ai-task="${task}"] select[aria-label$="思考档位"]`) as HTMLSelectElement
const refSelect = (task: string) =>
  host.querySelector(`[data-ai-task="${task}"] [data-ai-task-ref]`) as HTMLSelectElement

async function render(tasks: AiTaskRow[], models: AiModelEntry[] = []) {
  mocks.aiTasks.mockResolvedValue({ tasks })
  mocks.aiModels.mockResolvedValue({ models })
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
  const select = thinkingSelect('stock_analysis')
  expect([...select.options].map(o => o.value)).toEqual(['', 'off', 'low', 'medium', 'high', 'xhigh', 'default'])
  expect([...select.options].map(o => o.textContent)).toEqual([
    '思考：按任务默认（模型默认）', '思考：关', '思考：低', '思考：中', '思考：高', '思考：极高', '思考：模型默认（不指定档位）',
  ])
})

it('saves the chosen level for that task only', async () => {
  mocks.saveAiTask.mockResolvedValue({ tasks: [row('stock_analysis', '个股分析', { thinking: 'high', reasoning_effort: 'high', route: 'api' }, 'high')] })
  await render([row('stock_analysis', '个股分析', { reasoning_effort: '', route: 'api' })])
  const card = host.querySelector('[data-ai-task="stock_analysis"]') as HTMLElement
  const select = thinkingSelect('stock_analysis')
  await act(async () => {
    select.value = 'high'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const save = [...card.querySelectorAll('button')].find(b => b.textContent === '保存') as HTMLButtonElement
  await act(async () => { save.click() })
  await flush()
  expect(mocks.saveAiTask).toHaveBeenCalledWith('stock_analysis', { model: '', base_url: '', thinking: 'high', api_key: null, model_ref: '' })
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
  expect(thinkingSelect('stock_analysis').value).toBe('xhigh')
})

it('falls back to the old on/off label when the backend has no reasoning_effort field', async () => {
  await render([
    { ...row('assistant', '助手对话', {}), effective: { model: 'm', base_url: '', thinking: 'default', overridden: [] } },
    { ...row('news_translate', '新闻翻译与打标', {}, '', 'off'), effective: { model: 'm', base_url: '', thinking: 'off', overridden: [] } },
  ])
  expect(effectiveText('assistant')).toContain('默认思考')
  expect(effectiveText('news_translate')).toContain('关思考')
})

const lib = (id: string, name: string, model: string): AiModelEntry => ({
  id, name, model, base_url: 'https://x/v1', has_api_key: true, api_key_masked: 'sk-1••••abcd', used_by: [],
})

it('lets each task pick a model from the library (DeepSeek for translate, Claude for assistant)', async () => {
  mocks.saveAiTask.mockResolvedValue({ tasks: [] })
  await render([row('news_translate', '新闻翻译与打标', { route: 'api' }, '', 'off')],
    [lib('deepseek', 'DeepSeek', 'deepseek-chat'), lib('claude', 'Claude', 'claude-x')])
  const sel = refSelect('news_translate')
  expect([...sel.options].map(o => o.value)).toEqual(['', 'deepseek', 'claude', '__custom__'])
  // 未选单独填写时不显示地址/Key 输入框
  expect(host.querySelector('[data-ai-task="news_translate"] input[type="password"]')).toBeNull()
  await act(async () => {
    sel.value = 'deepseek'
    sel.dispatchEvent(new Event('change', { bubbles: true }))
  })
  const save = [...host.querySelectorAll('[data-ai-task="news_translate"] button')].find(b => b.textContent === '保存') as HTMLButtonElement
  await act(async () => { save.click() })
  await flush()
  expect(mocks.saveAiTask).toHaveBeenCalledWith('news_translate', { model: '', base_url: '', thinking: '', api_key: null, model_ref: 'deepseek' })
})

it('shows the library model name in the effective line and preselects it', async () => {
  const r = row('assistant', '助手对话', { model: 'claude-x', route: 'api', model_ref: 'claude', model_ref_name: 'Claude' })
  r.override.model_ref = 'claude'
  await render([r], [lib('claude', 'Claude', 'claude-x')])
  expect(effectiveText('assistant')).toContain('模型库「Claude」')
  expect(refSelect('assistant').value).toBe('claude')
})

it('legacy per-task fields open the custom editor; dangling refs are flagged', async () => {
  const legacy = row('stock_analysis', '个股分析', { route: 'api' })
  legacy.override.model = 'old-model'
  const dangling = row('assistant', '助手对话', { route: 'api', model_ref_missing: true })
  await render([legacy, dangling])
  expect(refSelect('stock_analysis').value).toBe('__custom__')
  expect(host.querySelector('[data-ai-task="stock_analysis"] input[type="password"]')).not.toBeNull()
  expect(host.querySelector('[data-ai-task="assistant"] [data-ai-task-ref-missing]')).not.toBeNull()
})
