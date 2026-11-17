/**
 * AI 助手自包含 HTTP 客户端 — NDJSON 流式解析 + 状态/快捷指令查询。
 *
 * 解耦取舍: 项目约定核心页面统一走 `lib/api.ts`, 但扩展模块以"不修改核心
 * 文件"为更高优先级, 故在此内建同款约定的最小客户端(同源 fetch + 统一
 * 错误文案), 不复用也不修改 api.ts。协议与后端 `app/custom/assistant/__init__.py`
 * 的注释一一对应; 未知事件类型(如 ping)静默忽略。
 */

export interface AssistantStatus {
  configured: boolean
  provider: string
  model: string
  supports_tools: boolean
}

export interface QuickSuggest {
  id: string
  label: string
  prompt: string
}

/** 工具附带的可绘图数据 — 来自工具真实返回, 非模型生成(结果可核对)。 */
export type AssistantChart =
  | {
      kind: 'daily_kline'
      symbol: string
      name?: string
      /** [date, open, high, low, close, volume] 按日期升序, 后端封顶 120 根 */
      points: [string, number, number, number, number, number][]
    }
  | {
      kind: 'intraday'
      symbol: string
      name?: string
      /** 昨收基准价(反推值), 用于画基准虚线与涨跌着色 */
      prev_close?: number
      /** [HH:MM, price, volume] 按时间升序, 后端封顶 240 点 */
      points: [string, number, number][]
    }

export type AssistantEvent =
  | { type: 'notice'; message: string }
  | { type: 'skill'; name: string; title: string }
  | { type: 'tool_call'; call_id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; call_id: string; name: string; ok: boolean; summary: string; elapsed_ms: number; charts?: AssistantChart[] }
  | { type: 'delta'; content: string }
  | { type: 'error'; kind: string; message: string; hint?: string }
  | { type: 'done' }
  | { type: 'ping' }

export interface ChatHistoryMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface AssistantContext {
  page?: string
  symbol?: string
}

async function requestJson<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(path, init?.method
    ? { method: init.method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(init.body ?? {}) }
    : undefined)
  if (!res.ok) {
    let detail = ''
    try {
      const parsed = JSON.parse(await res.text()) as { detail?: unknown }
      detail = typeof parsed.detail === 'string' ? parsed.detail : ''
    } catch { /* 非 JSON 错误体走状态码文案 */ }
    throw new Error(detail || `AI 助手接口请求失败: ${res.status}`)
  }
  return res.json() as Promise<T>
}

// ===== 技能 (B 档技能管理) =====

export interface SkillSummary {
  name: string
  title: string
  group: string
  triggers: string[]
  tools: string[]
  output: 'chat' | 'report'
  pages: string[]
  source: 'builtin' | 'user'
  version: number
  enabled: boolean
  based_on: string | null
  last_run: SkillRun | null
  runs_ok: number
  runs_n: number
  errors?: number
  warnings?: number
}

export interface SkillRun { skill: string; at: string; ok: boolean; tools: number; ms: number }
export interface SkillCheck { level: 'error' | 'warn'; item: string; detail: string }
export interface SkillVersion { version: number; saved_at: string; note: string; warnings: number; current: boolean }
export interface SkillDetail extends SkillSummary {
  text: string
  body: string
  checks: SkillCheck[]
  versions: SkillVersion[]
  runs: SkillRun[]
}

const SKILL_BASE = '/api/custom/assistant/skills'

export function fetchSkills(): Promise<{ skills: SkillSummary[]; tools: string[] }> {
  return requestJson(SKILL_BASE)
}

export function fetchSkill(name: string): Promise<SkillDetail> {
  return requestJson(`${SKILL_BASE}/${encodeURIComponent(name)}`)
}

export function checkSkill(text: string): Promise<{ checks: SkillCheck[]; skill: SkillSummary | null }> {
  return requestJson(`${SKILL_BASE}/check`, { method: 'POST', body: { text } })
}

export function saveSkill(text: string, note = ''): Promise<{ saved: boolean; checks: SkillCheck[]; skill: SkillSummary }> {
  return requestJson(SKILL_BASE, { method: 'POST', body: { text, note } })
}

export function setSkillEnabled(name: string, enabled: boolean): Promise<{ name: string; enabled: boolean }> {
  return requestJson(`${SKILL_BASE}/${encodeURIComponent(name)}/enabled`, { method: 'POST', body: { enabled } })
}

export function rollbackSkill(name: string, version: number): Promise<{ name: string; version: number }> {
  return requestJson(`${SKILL_BASE}/${encodeURIComponent(name)}/rollback`, { method: 'POST', body: { version } })
}

export function deleteSkill(name: string): Promise<{ name: string; deleted: boolean }> {
  return requestJson(`${SKILL_BASE}/${encodeURIComponent(name)}/delete`, { method: 'POST', body: {} })
}

// ===== 体检 =====

export interface HealthTool { name: string; label: string; state: 'ok' | 'warn' | 'error' | 'skip'; detail: string; ms: number }
export interface HealthReport {
  at: string | null
  symbol?: string
  secs?: number
  tools: HealthTool[]
  counts?: { ok: number; warn: number; error: number; skip: number }
  summary: string | null
}

export function fetchHealth(): Promise<HealthReport> {
  return requestJson('/api/custom/assistant/health')
}

export function runHealth(): Promise<HealthReport> {
  return requestJson('/api/custom/assistant/health/run', { method: 'POST' })
}

export function fetchAssistantStatus(): Promise<AssistantStatus> {
  return requestJson<AssistantStatus>('/api/custom/assistant/status')
}

export function fetchAssistantSuggests(): Promise<QuickSuggest[]> {
  return requestJson<{ suggests: QuickSuggest[] }>('/api/custom/assistant/suggests')
    .then(body => body.suggests)
}

/** 对话主入口: POST NDJSON, 逐行 yield 事件; signal 支持中断。 */
export async function* assistantChatStream(
  body: { messages: ChatHistoryMessage[]; context?: AssistantContext },
  signal?: AbortSignal,
): AsyncGenerator<AssistantEvent> {
  const res = await fetch('/api/custom/assistant/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) {
    let detail = ''
    try {
      const parsed = JSON.parse(await res.text()) as { detail?: string; message?: string }
      detail = parsed.detail ?? parsed.message ?? ''
    } catch { /* 忽略解析失败, 走状态码文案 */ }
    throw new Error(detail || `AI 助手请求失败: ${res.status}`)
  }
  if (!res.body) throw new Error('响应无 body')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      const event = parseEventLine(line)
      if (event) yield event
    }
  }
  const tail = parseEventLine(buf)
  if (tail) yield tail
}

function parseEventLine(line: string): AssistantEvent | null {
  const text = line.trim()
  if (!text) return null
  try {
    return JSON.parse(text) as AssistantEvent
  } catch {
    return null
  }
}
