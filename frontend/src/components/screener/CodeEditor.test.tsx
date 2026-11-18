// @vitest-environment jsdom
// 策略代码编辑器: 上色不能改字 (片段拼回去 = 原文), 两层排版同一份, Tab 插 4 空格;
// 创建策略弹窗: AI 页签的指南 / 标准跳开源仓库; 手写页签是三栏 (模板 · 代码 · 分项校验),
// 停笔后自动校验且不改代码, 错误行标出并可跳转, 指南与标准在抽屉里看。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRef } from 'react'
import { CodeEditor, EDITOR_LINE_PX, EDITOR_PAD_PX, EDITOR_TEXT, tokenizePython, type CodeEditorHandle } from './CodeEditor'
import { AUTO_VALIDATE_MS, StrategyBuilderDialog, buildCustomTemplate, countLines } from './StrategyBuilderDialog'
import { loadDefaultBasicFilter } from './DefaultStrategyParamsDialog'
import { DOC_LINKS, REPO_URL } from '@/lib/docsLinks'

const validate = vi.fn()
const check = vi.fn()
const kit = vi.fn()
vi.mock('@/lib/api', () => ({
  api: {
    strategyValidateCode: (p: unknown, o: unknown) => validate(p, o),
    strategyCheckCode: (c: unknown, o: unknown) => check(c, o),
    strategyCodeKit: () => kit(),
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
  check.mockReset()
  kit.mockReset()
  kit.mockResolvedValue(KIT)
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

it('marks error lines in the gutter and with a band placed by the shared line metrics', async () => {
  await act(async () => { root.render(<CodeEditor value={SAMPLE} onChange={() => {}} errorLines={[3, 3, 999, 0, 5]} />) })
  expect([...host.querySelectorAll('[data-error-line]')].map((e) => e.textContent)).toEqual(['3', '5'])   // 去重, 越界的不标
  const bands = [...host.querySelectorAll<HTMLElement>('[data-mark-line]')]
  expect(bands.map((b) => [b.dataset.markLine, b.style.top, b.style.height])).toEqual([
    ['3', `${EDITOR_PAD_PX + 2 * EDITOR_LINE_PX}px`, `${EDITOR_LINE_PX}px`],
    ['5', `${EDITOR_PAD_PX + 4 * EDITOR_LINE_PX}px`, `${EDITOR_LINE_PX}px`],
  ])
  expect(EDITOR_TEXT.split(' ')).toEqual(expect.arrayContaining(['p-3', 'leading-5']))   // 12px / 20px 与这两个类对应
  expect([EDITOR_PAD_PX, EDITOR_LINE_PX]).toEqual([12, 20])
  await act(async () => { root.render(<CodeEditor value={SAMPLE} onChange={() => {}} />) })
  expect(host.querySelector('[data-error-line], [data-mark-line]')).toBeNull()
})

it('focusLine selects the whole line and scrolls it into view', async () => {
  const ref = createRef<CodeEditorHandle>()
  const value = Array.from({ length: 40 }, (_, k) => `line_${k + 1} = ${k}`).join('\n')
  await act(async () => { root.render(<CodeEditor ref={ref} value={value} onChange={() => {}} />) })
  const input = host.querySelector<HTMLTextAreaElement>('[data-code-input]')!
  await act(async () => { ref.current!.focusLine(21) })
  expect(input.value.slice(input.selectionStart, input.selectionEnd)).toBe('line_21 = 20')
  expect(document.activeElement).toBe(input)
  expect(input.scrollTop).toBe(17 * EDITOR_LINE_PX)                    // 目标行上方留三行
  await act(async () => { ref.current!.focusLine(999) })               // 越界取最后一行
  expect(input.value.slice(input.selectionStart, input.selectionEnd)).toBe('line_40 = 39')
  await act(async () => { ref.current!.focusLine(1) })
  expect(input.selectionStart).toBe(0)
  expect(input.scrollTop).toBe(0)
})

// ── 创建策略弹窗 ──────────────────────────────────────────────────

const tplCode = (id: string, name: string) => `"""${name}"""\nMETA = {\n    "id": "custom_${id}",\n    "name": "${name}",\n    "description": "${name}的说明",\n}\n`
const KIT = {
  templates: [
    { id: 'a', name: '回踩均线', level: '入门', order: 1, backend: 'polars_expr', kind: '单日过滤', lines: 49, params: 3, code: tplCode('a', '回踩均线') },
    { id: 'b', name: '站上均线', level: '入门', order: 2, backend: 'polars_expr', kind: '单日过滤', lines: 39, params: 1, code: tplCode('b', '站上均线') },
    { id: 'c', name: '放量突破', level: '进阶', order: 3, backend: 'matrix_native', kind: '矩阵', lines: 80, params: 2, code: tplCode('c', '放量突破') },
  ],
  standard: {
    toc: ['文件', 'META', '执行方式', '矩阵写法的契约', '允许的 import', '禁止的调用与访问'].map((title, k) => ({ n: k + 1, title })),
    backends: [{ id: 'polars_expr', label: '单日过滤', must: 'filter', forbid: 'filter_history' }, { id: 'matrix_native', label: '矩阵', must: 'MATRIX_STRATEGY', forbid: 'filter / filter_history' }],
    allowed_imports: ['datetime', 'numpy', 'polars'], forbidden_calls: ['eval', 'exec', 'open'], forbidden_attrs: ['__class__'],
    errors: [{ sample: 'polars_expr strategy must declare only filter', meaning: '单日过滤写法只能有 filter 一个入口', fix: '补上 filter' }],
  },
}
const item = (key: string, label: string, state: string, text: string, line: number | null = null, section: number | null = null, detail = '') =>
  ({ key, label, state, text, line, section, detail })
const OK = {
  valid: true,
  items: [item('safety', '安全', 'ok', 'import 1 个 · 均在清单内'), item('meta', 'META', 'ok', '参数 3 个 · 评分合计 1.0'), item('entry', '入口', 'ok', 'filter · 单日过滤'), item('fields', '字段', 'ok', '9 个 · 均可用')],
  overview: { lines: 49, params: 3, backend: 'polars_expr', backend_label: '单日过滤', stop_loss: -0.06, max_hold_days: 7 },
}
const BAD = {
  valid: false,
  items: [item('safety', '安全', 'bad', '禁止 import os', 3, 5, '禁止 import os (不在策略安全白名单)'), item('meta', 'META', 'bad', '评分合计 0.9 · 应为 1.0', 21, 2, 'META.scoring 权重总和必须为 1.0'),
    item('entry', '入口', 'ok', 'filter · 单日过滤'), item('fields', '字段', 'skip', '前一项修正后再查')],
  overview: { lines: 51, params: 3, backend: 'polars_expr', backend_label: '单日过滤', stop_loss: -0.065, max_hold_days: null },
}
const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector<T>(sel)
const qa = <T extends Element = HTMLElement>(sel: string) => [...document.querySelectorAll<T>(sel)]
const settle = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS + 1) }) }
const typeInto = async (input: HTMLTextAreaElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
  await act(async () => { setter.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })) })
}

async function openCustomTab() {
  await act(async () => { root.render(<StrategyBuilderDialog open onClose={() => {}} />) })
  const tab = [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('自定义编写'))!
  await act(async () => { tab.click() })
}

it('AI tab links to the repo docs in a new tab; custom tab opens them in a drawer instead', async () => {
  expect(DOC_LINKS.strategyGuide).toBe(`${REPO_URL}/blob/main/docs/strategy-development.md`)
  expect(DOC_LINKS.strategyStandard).toBe(`${REPO_URL}/blob/main/docs/strategy-standard.md`)
  expect(REPO_URL).toMatch(/^https:\/\/github\.com\/[\w-]+\/zentrade$/)
  await act(async () => { root.render(<StrategyBuilderDialog open onClose={() => {}} />) })
  const links = qa<HTMLAnchorElement>('[data-strategy-docs] a')
  expect(links.map((a) => [a.dataset.docLink, a.getAttribute('href'), a.target, a.rel, a.textContent])).toEqual([
    ['guide', DOC_LINKS.strategyGuide, '_blank', 'noopener noreferrer', '开发指南'],
    ['standard', DOC_LINKS.strategyStandard, '_blank', 'noopener noreferrer', '语法标准'],
  ])
  expect(document.body.textContent).not.toContain('策略开发指南见项目文档')   // 原来点不开的灰字
  await openCustomTab()
  expect(qa('[data-strategy-docs] a')).toEqual([])
  expect(qa('[data-doc-open]').map((b) => [b.dataset.docOpen, b.textContent])).toEqual([['guide', '开发指南'], ['standard', '语法标准']])
  expect(q('[data-drawer]')).toBeNull()
  await act(async () => { q('[data-doc-open="standard"]')!.click() })
  expect(q('[data-drawer]')!.dataset.drawer).toBe('standard')
  expect(q<HTMLAnchorElement>('[data-drawer-source]')!.getAttribute('href')).toBe(DOC_LINKS.strategyStandard)
  expect(qa('[data-toc]').length).toBe(KIT.standard.toc.length)
  expect(q('[data-sec="5"]')!.textContent).toContain(`§5 允许的 import · ${KIT.standard.allowed_imports.length} 个`)
  expect(q('[data-sec="6"]')!.textContent).toContain(`§6 禁止的调用 · ${KIT.standard.forbidden_calls.length} 个`)
  expect(qa('[data-sec="3"] [data-backend]').length).toBe(KIT.standard.backends.length)
  await act(async () => { q('[data-drawer-tab="guide"]')!.click() })
  expect(q<HTMLAnchorElement>('[data-drawer-source]')!.getAttribute('href')).toBe(DOC_LINKS.strategyGuide)
  expect(qa('[data-error-row]').map((r) => r.textContent)).toEqual(['单日过滤写法只能有 filter 一个入口处理补上 filterpolars_expr strategy must declare only filter'])
  await act(async () => { [...document.querySelectorAll('[data-drawer] button')].find((b) => b.textContent === '关闭')!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(q('[data-drawer]')).toBeNull()
  expect(q('[data-builder-custom]')).not.toBeNull()                    // 关抽屉不关弹窗
})

it('custom tab checks after typing pauses, shows four items and never rewrites the code', async () => {
  vi.useFakeTimers()
  check.mockResolvedValue(OK)
  await openCustomTab()
  const input = q<HTMLTextAreaElement>('[data-code-input]')!
  const template = input.value
  expect(template).toBe(buildCustomTemplate(loadDefaultBasicFilter()))  // 进页签即带模板
  expect(q('[data-auto-check]')?.dataset.autoCheck).toBe('checking')
  expect(q('[data-col="check"]')!.dataset.checkState).toBe('checking')
  expect(check).not.toHaveBeenCalled()                                 // 还没停够
  await act(async () => { await vi.advanceTimersByTimeAsync(AUTO_VALIDATE_MS - 1) })
  expect(check).not.toHaveBeenCalled()
  await act(async () => { await vi.advanceTimersByTimeAsync(2) })
  expect(check).toHaveBeenCalledTimes(1)
  expect(check).toHaveBeenCalledWith(template, { quiet: true })         // 失败不弹全局提示
  expect(validate).not.toHaveBeenCalled()                              // 自动校验不走会改写代码的那个接口
  expect(q('[data-auto-check]')?.textContent).toBe('已通过')
  expect(q('[data-check-badge]')?.textContent).toBe('全部通过')
  expect(qa('[data-check]').map((r) => [r.dataset.check, r.dataset.state])).toEqual([['safety', 'ok'], ['meta', 'ok'], ['entry', 'ok'], ['fields', 'ok']])
  expect(qa('[data-check-facts] > div').map((d) => d.textContent)).toEqual(['行数49', '参数3', '止损6%', '最长持有7 日'])
  expect(q('[data-error-line], [data-mark-line], [data-jump-line]')).toBeNull()
  expect(q<HTMLButtonElement>('[data-builder-save]')!.disabled).toBe(false)
  expect(q<HTMLTextAreaElement>('[data-code-input]')!.value).toBe(template)   // 自动校验不改代码
})

it('failed items show the line, mark it in the editor, jump to it and link to the standard section', async () => {
  vi.useFakeTimers()
  check.mockResolvedValue(BAD)
  await openCustomTab()
  await settle()
  expect(q('[data-check-badge]')?.textContent).toBe('2 项未通过')
  expect(q('[data-auto-check]')?.textContent).toBe('2 行有误')
  expect(qa('[data-check]').map((r) => r.dataset.state)).toEqual(['bad', 'bad', 'ok', 'skip'])
  expect(qa('[data-check-line]').map((b) => b.textContent)).toEqual(['第 3 行 · 禁止 import os', '第 21 行 · 评分合计 0.9 · 应为 1.0'])
  expect(qa('[data-check-ref]').map((b) => b.textContent)).toEqual(['标准 §5', '标准 §2'])
  expect(qa('[data-error-line]').map((e) => e.textContent)).toEqual(['3', '21'])
  expect(qa('[data-mark-line]').map((e) => e.dataset.markLine)).toEqual(['3', '21'])
  expect(qa('[data-jump-line]').map((e) => e.textContent)).toEqual(['第 3 行', '第 21 行'])
  expect(qa('[data-check-facts] > div').map((d) => d.textContent)).toEqual(['行数51', '参数3', '止损6.5%', '最长持有—'])
  expect(q<HTMLButtonElement>('[data-builder-save]')!.disabled).toBe(true)   // 没过不让存
  expect(q('[data-builder-blocked]')!.textContent).toBe('校验未通过 · 改好后可保存')   // 并说明为什么按钮是灰的
  const input = q<HTMLTextAreaElement>('[data-code-input]')!
  await act(async () => { q('[data-check-line="21"]')!.click() })
  expect(input.selectionStart).toBe(input.value.split('\n').slice(0, 20).join('\n').length + 1)
  expect(document.activeElement).toBe(input)
  await act(async () => { q('[data-check-ref="5"]')!.click() })       // 条目链接 → 抽屉对应章节
  expect([q('[data-drawer]')!.dataset.drawer, q('[data-drawer]')!.dataset.drawerSection]).toEqual(['standard', '5'])
  expect(qa('[data-toc][data-on]').map((e) => e.dataset.toc)).toEqual(['5'])
  expect(q('[data-drawer-section-link]')).toBeNull()                   // §5 抽屉里有内容
  await act(async () => { [...document.querySelectorAll('[data-drawer] button')].find((b) => b.textContent === '关闭')!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await act(async () => { q('[data-check-ref="2"]')!.click() })
  expect(q('[data-drawer-section-link]')!.textContent).toBe('§2 见仓库原文')   // §2 抽屉里没有专门一块, 给原文链接
})

it('auto check restarts on every edit, ignores stale answers, hides marks while checking and tolerates network errors', async () => {
  vi.useFakeTimers()
  let release: (v: unknown) => void = () => {}
  check.mockImplementationOnce(() => new Promise((r) => { release = r }))
  check.mockResolvedValue(BAD)
  await openCustomTab()
  await settle()
  expect(check).toHaveBeenCalledTimes(1)                               // 第一次请求还挂着
  const input = q<HTMLTextAreaElement>('[data-code-input]')!
  await typeInto(input, input.value + '\nimport os\n')
  expect(q('[data-auto-check]')?.textContent).toBe('校验中')
  await act(async () => { release(OK) })                               // 旧代码的回答迟到
  expect(q('[data-auto-check]')?.textContent).toBe('校验中')            // 不采信
  expect(q('[data-check-badge]')?.textContent).toBe('校验中')
  await settle()
  expect(check).toHaveBeenCalledTimes(2)
  expect(q('[data-auto-check]')?.textContent).toBe('2 行有误')
  await typeInto(input, input.value + '#')                             // 再改: 旧结果留着看, 但行标先撤 (行号会随输入移动)
  expect(qa('[data-check]').map((r) => r.dataset.state)).toEqual(['bad', 'bad', 'ok', 'skip'])
  expect(q('[data-error-line], [data-mark-line], [data-jump-line]')).toBeNull()
  expect(q<HTMLButtonElement>('[data-builder-save]')!.disabled).toBe(false)
  check.mockRejectedValue(new Error('network'))
  await settle()
  expect(q('[data-auto-check]')).toBeNull()                            // 断网不当成代码错误
  expect(q('[data-check-badge]')).toBeNull()
  expect(qa('[data-check]').map((r) => r.dataset.state)).toEqual(['skip', 'skip', 'skip', 'skip'])
  expect(q<HTMLButtonElement>('[data-builder-save]')!.disabled).toBe(false)
})

it('template library: counts per kind, filter, one click on a clean editor, two clicks once the code is edited', async () => {
  vi.useFakeTimers()
  check.mockResolvedValue(OK)
  await openCustomTab()
  await settle()
  expect(kit).toHaveBeenCalledTimes(1)
  expect(qa('[data-kind]').map((b) => b.textContent)).toEqual(['全部 3', '单日过滤 2', '矩阵 1'])
  expect(qa('[data-tpl]').map((b) => b.textContent)).toEqual(['回踩均线入门单日过滤49 行参数 3', '站上均线入门单日过滤39 行参数 1', '放量突破进阶矩阵80 行参数 2'])
  await act(async () => { q('[data-kind="矩阵"]')!.click() })
  expect(qa('[data-tpl]').map((b) => b.dataset.tpl)).toEqual(['c'])
  await act(async () => { q('[data-kind="all"]')!.click() })
  const input = () => q<HTMLTextAreaElement>('[data-code-input]')!
  const names = () => qa<HTMLInputElement>('[data-builder-custom] input').map((i) => i.value)
  await act(async () => { q('[data-tpl="a"]')!.click() })              // 编辑器里还是默认模板: 一次点击即换
  expect(input().value).toBe(KIT.templates[0].code)
  expect(q('[data-tpl="a"]')!.dataset.on).toBe('true')
  expect(names()).toEqual(['回踩均线', '回踩均线的说明'])                 // 名称 / 描述是空的, 跟着模板填
  expect(q('[data-code-file]')!.textContent).toBe('custom_a.py')
  await act(async () => { q('[data-tpl="b"]')!.click() })              // 模板原文没动过: 仍然一次点击
  expect(input().value).toBe(KIT.templates[1].code)
  expect(names()).toEqual(['站上均线', '站上均线的说明'])                 // 上一个模板带来的名称跟着换
  await typeInto(input(), input().value + '# 我自己写的\n')
  const mine = input().value
  await act(async () => { q('[data-tpl="c"]')!.click() })              // 写过自己的代码: 第一次点只是提示
  expect(input().value).toBe(mine)
  expect(q('[data-tpl="c"]')!.dataset.armed).toBe('true')
  expect(q('[data-tpl="c"]')!.textContent).toContain('再点一次 · 替换当前代码')
  await act(async () => { q('[data-tpl="c"]')!.click() })
  expect(input().value).toBe(KIT.templates[2].code)
  expect(q('[data-tpl][data-armed]')).toBeNull()
})

it('line count ignores the trailing newline, like the backend does', () => {
  expect([countLines(''), countLines('a'), countLines('a\n'), countLines('a\nb'), countLines('a\nb\n'), countLines('a\n\n'), countLines('a\r\n')]).toEqual([0, 1, 1, 2, 2, 2, 1])
})

it('the template library failing to load leaves the editor usable', async () => {
  vi.useFakeTimers()
  kit.mockRejectedValue(new Error('offline'))
  check.mockResolvedValue(OK)
  await openCustomTab()
  await settle()
  expect(qa('[data-tpl]')).toEqual([])
  expect(q<HTMLTextAreaElement>('[data-code-input]')!.value).toContain('def filter(')
  expect(q('[data-check-badge]')?.textContent).toBe('全部通过')
})
