// @vitest-environment jsdom
// 策略代码编辑器: 上色不能改字 (片段拼回去 = 原文), 两层排版同一份, Tab 插 4 空格;
// 创建策略弹窗: 指南 / 标准跳开源仓库, 手写页签停笔后自动校验且不改代码。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CodeEditor, EDITOR_TEXT, tokenizePython } from './CodeEditor'
import { AUTO_VALIDATE_MS, StrategyBuilderDialog, buildCustomTemplate } from './StrategyBuilderDialog'
import { loadDefaultBasicFilter } from './DefaultStrategyParamsDialog'
import { DOC_LINKS, REPO_URL } from '@/lib/docsLinks'

const validate = vi.fn()
vi.mock('@/lib/api', () => ({
  api: {
    strategyValidateCode: (p: unknown, o: unknown) => validate(p, o),
    strategyAiStatus: vi.fn(async () => ({ configured: false, has_key: false, has_model: false })),
    strategySaveCodeV2: vi.fn(), strategyAiIterate: vi.fn(), strategyBuildStream: vi.fn(),
  },
  friendlyStreamError: (e: unknown) => String(e),
}))

let root: Root
let host: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  validate.mockReset()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers() })

const SAMPLE = `"""文档串
第二行"""
import polars as pl  # 注释里的 def 不算关键字

META = {"id": "custom_x", "limit": 100, "ok": True, "w": 0.5e-3}

@staticmethod
def filter(df, params):
    s = 'it\\'s' + f"{x}" + rb"raw"
    return (pl.col("close") > 1_000).fill_null(False) if not None else 0x1F
`

it('tokens join back to the exact source, including edge cases', () => {
  for (const src of [SAMPLE, '', '\n\n', 'x = "未闭合', "'''三引号未闭合\n\n", '# 只有注释', 'a=1#b', '@', '1.', '.5+5.', 'def', 'class  Foo:', 'r', 'foo"bar"', 'x\ty']) {
    expect(tokenizePython(src).map((t) => t.text).join('')).toBe(src)
  }
})

it('classifies comments, strings, keywords, constants, numbers, decorators and def names', () => {
  const toks = tokenizePython(SAMPLE)
  const of = (kind: string) => toks.filter((t) => t.kind === kind).map((t) => t.text)
  expect(of('comment')).toEqual(['# 注释里的 def 不算关键字'])
  expect(of('string')).toEqual(['"""文档串\n第二行"""', '"id"', '"custom_x"', '"limit"', '"ok"', '"w"', "'it\\'s'", 'f"{x}"', 'rb"raw"', '"close"'])
  expect(of('keyword')).toEqual(['import', 'as', 'def', 'return', 'if', 'not', 'else'])
  expect(of('const')).toEqual(['True', 'False', 'None'])
  expect(of('number')).toEqual(['100', '0.5e-3', '1_000', '0x1F'])
  expect(of('decorator')).toEqual(['@staticmethod'])
  expect(of('def')).toEqual(['filter'])
  expect(tokenizePython('x = a @ b').some((t) => t.kind === 'decorator')).toBe(false)   // 矩阵乘号不是装饰器
  expect(tokenizePython('define = 1').some((t) => t.kind === 'keyword')).toBe(false)    // 关键字只认整词
})

it('renders line numbers, a highlight layer and a textarea that share one typography', async () => {
  const onChange = vi.fn()
  await act(async () => { root.render(<CodeEditor value={SAMPLE} onChange={onChange} className="h-[420px]" />) })
  const input = host.querySelector<HTMLTextAreaElement>('[data-code-input]')!
  const pre = host.querySelector<HTMLElement>('[data-code-highlight]')!
  expect(host.querySelectorAll('[data-code-gutter] > div').length).toBe(SAMPLE.split('\n').length)
  expect(pre.textContent).toBe(SAMPLE + '\n')                       // 高亮层 = 原文 (末尾补一行, 保证最后一行可滚到)
  for (const cls of EDITOR_TEXT.split(' ')) {                         // 字体 / 行高 / 内边距 / 不换行: 两层同一份
    expect(input.className.split(' ')).toContain(cls)
    expect(pre.className.split(' ')).toContain(cls)
  }
  expect(EDITOR_TEXT).toMatch(/\bwhitespace-pre\b/)
  expect(input.getAttribute('wrap')).toBe('off')
  expect(input.className).toContain('text-transparent')
  expect(input.className).toContain('caret-foreground')
  expect(pre.className).toContain('pointer-events-none')
  expect(pre.getAttribute('aria-hidden')).toBe('true')
  expect(input.getAttribute('aria-label')).toBe('策略代码')
  expect(host.querySelector('[data-tok="keyword"]')?.textContent).toBe('import')
})

it('Tab inserts four spaces at the caret; Shift+Tab is left alone', async () => {
  const onChange = vi.fn()
  await act(async () => { root.render(<CodeEditor value={'ab'} onChange={onChange} />) })
  const input = host.querySelector<HTMLTextAreaElement>('[data-code-input]')!
  input.selectionStart = input.selectionEnd = 1
  const ev = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
  await act(async () => { input.dispatchEvent(ev) })
  expect(onChange).toHaveBeenCalledWith('a    b')
  expect(ev.defaultPrevented).toBe(true)
  const back = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })
  await act(async () => { input.dispatchEvent(back) })
  expect(back.defaultPrevented).toBe(false)
  expect(onChange).toHaveBeenCalledTimes(1)
})

// ── 创建策略弹窗 ──────────────────────────────────────────────────

async function openCustomTab() {
  await act(async () => { root.render(<StrategyBuilderDialog open onClose={() => {}} />) })
  const tab = [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('自定义编写'))!
  await act(async () => { tab.click() })
}

it('guide and standard links open the open-source repo docs in a new tab, on both tabs', async () => {
  expect(DOC_LINKS.strategyGuide).toBe(`${REPO_URL}/blob/main/docs/strategy-development.md`)
  expect(DOC_LINKS.strategyStandard).toBe(`${REPO_URL}/blob/main/docs/strategy-standard.md`)
  expect(REPO_URL).toMatch(/^https:\/\/github\.com\/[\w-]+\/zentrade$/)
  await act(async () => { root.render(<StrategyBuilderDialog open onClose={() => {}} />) })
  const check = () => {
    const links = [...document.querySelectorAll<HTMLAnchorElement>('[data-strategy-docs] a')]
    expect(links.map((a) => [a.dataset.docLink, a.getAttribute('href'), a.target, a.rel])).toEqual([
      ['guide', DOC_LINKS.strategyGuide, '_blank', 'noopener noreferrer'],
      ['standard', DOC_LINKS.strategyStandard, '_blank', 'noopener noreferrer'],
    ])
    expect(links.map((a) => a.textContent)).toEqual(['开发指南', '语法标准'])
  }
  check()                                                              // AI 生成页签
  expect(document.body.textContent).not.toContain('策略开发指南见项目文档')   // 原来点不开的灰字
  const tab = [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('自定义编写'))!
  await act(async () => { tab.click() })
  check()                                                              // 自定义编写页签
})

it('custom tab uses the code editor and validates after typing pauses, without rewriting the code', async () => {
  vi.useFakeTimers()
  validate.mockResolvedValue({ valid: true, error: null, code: 'REWRITTEN', meta: {} })
  await openCustomTab()
  const input = document.querySelector<HTMLTextAreaElement>('[data-code-input]')!
  const template = input.value
  expect(template).toContain('def filter(')                            // 进页签即带模板
  expect(template).toBe(buildCustomTemplate(loadDefaultBasicFilter()))
  expect(document.querySelector('[data-auto-check]')?.getAttribute('data-auto-check')).toBe('checking')
  expect(validate).not.toHaveBeenCalled()                              // 还没停够
  await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS - 1) })
  expect(validate).not.toHaveBeenCalled()
  await act(async () => { await vi.advanceTimersByTimeAsync(2) })
  expect(validate).toHaveBeenCalledTimes(1)
  expect(validate).toHaveBeenCalledWith({ code: template }, { quiet: true })   // 失败不弹全局提示
  expect(document.querySelector('[data-auto-check]')?.textContent).toBe('自动校验通过')
  expect(document.querySelector<HTMLTextAreaElement>('[data-code-input]')!.value).toBe(template)   // 自动校验不改代码
})

it('auto validation shows the reason on failure, restarts on every edit and ignores stale answers', async () => {
  vi.useFakeTimers()
  let release: (v: unknown) => void = () => {}
  validate.mockImplementationOnce(() => new Promise((r) => { release = r }))
  validate.mockResolvedValue({ valid: false, error: '禁止 import os (不在策略安全白名单)', code: '', meta: {} })
  await openCustomTab()
  await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS + 1) })
  expect(validate).toHaveBeenCalledTimes(1)                            // 第一次请求还挂着
  const input = document.querySelector<HTMLTextAreaElement>('[data-code-input]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
  await act(async () => { setter.call(input, input.value + '\nimport os\n'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(document.querySelector('[data-auto-check]')?.textContent).toBe('校验中')
  await act(async () => { release({ valid: true, error: null, code: '', meta: {} }) })   // 旧代码的回答迟到
  expect(document.querySelector('[data-auto-check]')?.textContent).toBe('校验中')          // 不采信
  await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS + 1) })
  expect(validate).toHaveBeenCalledTimes(2)
  expect(document.querySelector('[data-auto-check]')?.textContent).toBe('自动校验未通过')
  expect(document.querySelector('[data-auto-check-error]')?.textContent).toBe('禁止 import os (不在策略安全白名单)')
  validate.mockRejectedValue(new Error('network'))
  await act(async () => { setter.call(input, input.value + '#'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS + 1) })
  expect(document.querySelector('[data-auto-check]')).toBeNull()       // 断网不当成代码错误
  expect(document.querySelector('[data-auto-check-error]')).toBeNull()
})
