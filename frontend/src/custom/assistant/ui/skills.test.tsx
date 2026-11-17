// @vitest-environment jsdom
// 2026-11-17 AI 助手体检修复 + 技能管理 (B 档) 的前端守门。
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SkillSummary } from '../client'

const SKILLS: SkillSummary[] = [
  { name: 'posture', title: '持仓体检', group: '每日', triggers: ['/持仓', '持仓体检'], tools: ['get_portfolio', 'get_earnings'],
    output: 'chat', pages: ['持仓'], source: 'builtin', version: 1, enabled: true, based_on: null, last_run: null, runs_ok: 0, runs_n: 0 },
  { name: 'review', title: '盘后复盘', group: '每日', triggers: ['/复盘'], tools: ['get_indices'],
    output: 'report', pages: ['机会'], source: 'builtin', version: 1, enabled: true, based_on: null, last_run: null, runs_ok: 0, runs_n: 0 },
  { name: 'factor', title: '因子速查', group: '研究', triggers: ['/因子'], tools: ['get_factor_values'],
    output: 'chat', pages: ['因子'], source: 'builtin', version: 1, enabled: true, based_on: null, last_run: null, runs_ok: 0, runs_n: 0 },
]

const mocks = vi.hoisted(() => ({
  fetchSkill: vi.fn(),
  fetchSkills: vi.fn(),
  sendMessage: vi.fn(),
}))
vi.mock('../client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../client')>()
  return { ...actual, fetchSkill: mocks.fetchSkill, fetchSkills: mocks.fetchSkills }
})

import { SkillBar, SkillPalette, filterSkills, primaryTrigger, rankForPage, skillSteps } from './SkillPicker'
import { ASSISTANT_TOOL_LABELS } from './messages'

let root: Root | null = null
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  mocks.fetchSkill.mockImplementation(async (name: string) => ({
    ...SKILLS.find(s => s.name === name)!, text: '', body: '1. get_portfolio 读模拟持仓\n2. 按距止损排序', checks: [], versions: [], runs: [],
  }))
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

async function flush() {
  for (let i = 0; i < 3; i += 1) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

describe('工具中文名与后端注册表一致', () => {
  it('后端每个助手工具都有中文名 (不会把英文工具名露给用户)', () => {
    const backend = resolve(process.cwd(), '../backend/app')
    const names = new Set<string>()
    const src = readFileSync(resolve(backend, 'custom/assistant/tools.py'), 'utf8')
    for (const m of src.matchAll(/_schema\(\s*"([a-z_]+)"/g)) names.add(m[1])
    const cat = readFileSync(resolve(backend, 'services/tool_catalog.py'), 'utf8')
    for (const m of cat.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)) names.add(m[2])
    expect(names.size).toBe(19)
    expect([...names].filter(n => !ASSISTANT_TOOL_LABELS[n]).sort()).toEqual([])
  })

  it('内置技能引用的工具都有中文名', () => {
    const dir = resolve(process.cwd(), '../backend/app/custom/assistant/skills')
    for (const f of readdirSync(dir).filter(x => x.endsWith('.md'))) {
      const text = readFileSync(resolve(dir, f), 'utf8')
      const tools = /tools: \[([^\]]*)\]/.exec(text)![1].split(',').map(s => s.trim())
      expect(tools.filter(t => !ASSISTANT_TOOL_LABELS[t]), f).toEqual([])
    }
  })
})

describe('手机宽度面板不越出屏幕', () => {
  it('窄屏面板占满视口, 不再按 480px 下限把左侧推出屏幕', () => {
    const src = readFileSync(resolve('src/custom/assistant/ui/AssistantDrawer.tsx'), 'utf8')
    expect(src).toMatch(/if \(window\.innerWidth < MIN_WIDTH \+ 64\) return window\.innerWidth/)
    expect(src).not.toMatch(/Math\.max\(MIN_WIDTH, window\.innerWidth - 64\)/)
    expect(src).toContain('max-w-[100vw]')
  })
})

describe('技能条与指令卡', () => {
  it('当前页面相关的技能排最前, 点按钮发送触发词', () => {
    expect(rankForPage(SKILLS, '持仓')[0].name).toBe('posture')
    expect(rankForPage(SKILLS, '因子')[0].name).toBe('factor')
    expect(primaryTrigger(SKILLS[0])).toBe('/持仓')
    const onPick = vi.fn()
    root = createRoot(host)
    act(() => { root!.render(<SkillBar skills={SKILLS} page="因子" disabled={false} onPick={onPick} onMore={() => {}} />) })
    const buttons = [...host.querySelectorAll('[data-skill-bar] button')]
    expect(buttons[0].textContent).toBe('因子速查')
    expect(buttons.at(-1)!.textContent).toBe('全部 3')
    act(() => { (buttons[0] as HTMLButtonElement).click() })
    expect(onPick).toHaveBeenCalledWith(SKILLS[2])
  })

  it('输入 /持 过滤到持仓体检; 预览显示工具中文名与只读', async () => {
    expect(filterSkills(SKILLS, '/持').map(s => s.name)).toEqual(['posture'])
    expect(filterSkills(SKILLS, '/').length).toBe(3)
    root = createRoot(host)
    act(() => {
      root!.render(
        <MemoryRouter>
          <SkillPalette skills={SKILLS} query="/持" page="持仓" active={0} onActive={() => {}} onPick={() => {}} onClose={() => {}} />
        </MemoryRouter>,
      )
    })
    await flush()
    expect(host.querySelectorAll('[data-skill-item]').length).toBe(1)
    const preview = host.querySelector('[data-skill-preview="posture"]')!.textContent ?? ''
    expect(preview).toContain('查询模拟持仓')
    expect(preview).toContain('只读')
    expect(preview).toContain('按距止损排序')
    expect(preview).not.toMatch(/get_[a-z]/)
  })

  it('步骤里的工具英文名在界面上换成中文名', () => {
    expect(skillSteps('1. get_portfolio 读持仓\n2. get_news 看新闻')).toEqual(['「查询模拟持仓」读持仓', '「查询个股新闻」看新闻'])
  })

  it('没有匹配时明确写出, 不显示空白卡', () => {
    root = createRoot(host)
    act(() => {
      root!.render(
        <MemoryRouter>
          <SkillPalette skills={SKILLS} query="/不存在" page="" active={0} onActive={() => {}} onPick={() => {}} onClose={() => {}} />
        </MemoryRouter>,
      )
    })
    expect(host.textContent).toContain('无匹配技能')
  })
})

describe('设置页 AI 技能标签', () => {
  it('标签由扩展插槽提供: 核心设置页不直接 import 扩展模块', () => {
    const settings = readFileSync(resolve('src/pages/Settings.tsx'), 'utf8')
    expect(settings).toContain("key: 'skills'")
    expect(settings).toContain('settings.skills')
    expect(settings).not.toMatch(/from ['"]@\/custom\//)
    const ext = readFileSync(resolve('src/custom/assistant/extension.tsx'), 'utf8')
    expect(ext).toContain("name: 'settings.skills'")
  })
})
