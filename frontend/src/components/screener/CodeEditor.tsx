/**
 * 策略代码编辑器: 语法高亮 + 行号, 不引第三方编辑器库 (Monaco / CodeMirror 压缩后几百 KB 到数 MB)。
 * 做法: 透明文字的 textarea 叠在同字体、同行高的高亮层上; 输入、选区、撤销都是 textarea 原生行为,
 * 高亮层与行号栏只跟着滚动。两层的字体、字号、行高、内边距、换行方式必须完全一致, 否则光标与文字错位。
 */
import { forwardRef, useImperativeHandle, useMemo, useRef, type KeyboardEvent, type ReactNode } from 'react'
import { cn } from '@/lib/cn'

export type PyTokenKind = 'comment' | 'string' | 'keyword' | 'const' | 'number' | 'decorator' | 'def' | 'text'
export interface PyToken { kind: PyTokenKind; text: string }

const KEYWORDS = new Set([
  'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
  'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass',
  'raise', 'return', 'try', 'while', 'with', 'yield',
])
const CONSTS = new Set(['True', 'False', 'None'])
const WORD = /[A-Za-z_][A-Za-z0-9_]*/y
const NUMBER = /(?:0[xX][0-9a-fA-F_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)/y
const STRING_PREFIX = /[rRbBfFuU]{0,2}(?="|')/y

/** 把 Python 源码切成带类别的片段。只为上色: 不求语法完备, 但片段拼回去必须等于原文 (逐字符不丢不多)。 */
export function tokenizePython(src: string): PyToken[] {
  const out: PyToken[] = []
  const push = (kind: PyTokenKind, text: string) => {
    if (!text) return
    const last = out[out.length - 1]
    if (last && last.kind === kind && kind === 'text') last.text += text
    else out.push({ kind, text })
  }
  let i = 0
  let afterDef = false
  while (i < src.length) {
    const ch = src[i]
    if (ch === '#') {
      const end = src.indexOf('\n', i)
      const stop = end === -1 ? src.length : end
      push('comment', src.slice(i, stop)); i = stop; continue
    }
    STRING_PREFIX.lastIndex = i
    const pm = STRING_PREFIX.exec(src)
    const isWordStart = /[A-Za-z_]/.test(ch)
    const prevIsWord = i > 0 && /[A-Za-z0-9_]/.test(src[i - 1])
    if (pm && !prevIsWord && (ch === '"' || ch === "'" || pm[0].length > 0)) {
      const qi = i + pm[0].length
      const q = src[qi]
      const triple = src.startsWith(q.repeat(3), qi)
      const close = triple ? q.repeat(3) : q
      let j = qi + close.length
      while (j < src.length) {
        if (src[j] === '\\') { j += 2; continue }
        if (!triple && src[j] === '\n') break
        if (src.startsWith(close, j)) { j += close.length; break }
        j += 1
      }
      j = Math.min(j, src.length)
      push('string', src.slice(i, j)); i = j; afterDef = false; continue
    }
    if (isWordStart) {
      WORD.lastIndex = i
      const w = WORD.exec(src)![0]
      if (afterDef) { push('def', w); afterDef = false }
      else if (KEYWORDS.has(w)) { push('keyword', w); afterDef = w === 'def' || w === 'class' }
      else if (CONSTS.has(w)) push('const', w)
      else push('text', w)
      i += w.length; continue
    }
    if (/[0-9]/.test(ch) || (ch === '.' && /[0-9]/.test(src[i + 1] ?? ''))) {
      NUMBER.lastIndex = i
      const n = NUMBER.exec(src)
      if (n && n[0]) { push('number', n[0]); i += n[0].length; continue }
    }
    if (ch === '@' && (i === 0 || /^[ \t]*$/.test(src.slice(src.lastIndexOf('\n', i - 1) + 1, i)))) {
      WORD.lastIndex = i + 1
      const w = WORD.exec(src)
      const text = '@' + (w ? w[0] : '')
      push('decorator', text); i += text.length; continue
    }
    if (ch !== ' ' && ch !== '\t') afterDef = false
    push('text', ch); i += 1
  }
  return out
}

const TOKEN_CLS: Record<PyTokenKind, string> = {
  comment: 'text-muted italic',
  string: 'text-emerald-700 dark:text-emerald-400',
  keyword: 'text-accent dark:text-blue-400',
  const: 'text-amber-700 dark:text-amber-400',
  number: 'text-amber-700 dark:text-amber-400',
  decorator: 'text-secondary',
  def: 'font-semibold text-foreground',
  text: '',
}

/** 两层共用的排版: 改这里必须两层同时生效 */
export const EDITOR_TEXT = 'm-0 whitespace-pre p-3 font-mono text-[12px] leading-5 [tab-size:4]'
/** 与 EDITOR_TEXT 的 p-3 / leading-5 对应的像素值: 错误行色带与跳行按它定位 */
export const EDITOR_PAD_PX = 12
export const EDITOR_LINE_PX = 20
const INDENT = '    '

export interface CodeEditorHandle {
  /** 选中第 n 行 (从 1 起) 并滚到可见处 */
  focusLine: (line: number) => void
}

export const CodeEditor = forwardRef<CodeEditorHandle, {
  value: string; onChange: (v: string) => void; className?: string; ariaLabel?: string; readOnly?: boolean
  /** 要标出的行号 (从 1 起): 行号加重 + 整行色带 */
  errorLines?: readonly number[]
}>(function CodeEditor({ value, onChange, className, ariaLabel = '策略代码', readOnly, errorLines }, ref) {
  const preRef = useRef<HTMLPreElement>(null)
  const gutterRef = useRef<HTMLDivElement>(null)
  const marksRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const tokens = useMemo(() => tokenizePython(value), [value])
  const lines = useMemo(() => value.split('\n').length, [value])
  const marked = useMemo(
    () => [...new Set(errorLines ?? [])].filter(n => Number.isInteger(n) && n >= 1 && n <= lines).sort((a, b) => a - b),
    [errorLines, lines],
  )
  const markedSet = useMemo(() => new Set(marked), [marked])

  const syncScroll = (t: HTMLTextAreaElement) => {
    if (preRef.current) { preRef.current.scrollTop = t.scrollTop; preRef.current.scrollLeft = t.scrollLeft }
    if (gutterRef.current) gutterRef.current.scrollTop = t.scrollTop
    if (marksRef.current) marksRef.current.style.transform = `translateY(${-t.scrollTop}px)`
  }

  useImperativeHandle(ref, () => ({
    focusLine(line: number) {
      const el = inputRef.current
      if (!el) return
      const rows = el.value.split('\n')
      const n = Math.min(Math.max(1, Math.trunc(line) || 1), rows.length)
      let start = 0
      for (let k = 0; k < n - 1; k++) start += rows[k].length + 1
      el.focus()
      el.setSelectionRange(start, start + rows[n - 1].length)
      el.scrollTop = Math.max(0, (n - 4) * EDITOR_LINE_PX)   // 目标行上方留三行
      syncScroll(el)
    },
  }), [])

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Tab' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || readOnly) return
    // Tab 插入 4 个空格 (Python 缩进); Shift+Tab 留给键盘用户移出编辑器
    e.preventDefault()
    const el = e.currentTarget
    const { selectionStart: a, selectionEnd: b } = el
    onChange(value.slice(0, a) + INDENT + value.slice(b))
    requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = a + INDENT.length })
  }

  const highlighted: ReactNode[] = tokens.map((t, k) => (
    t.kind === 'text' ? t.text : <span key={k} data-tok={t.kind} className={TOKEN_CLS[t.kind]}>{t.text}</span>
  ))

  return (
    <div data-code-editor className={cn('flex overflow-hidden rounded-xl border border-border/40 bg-base focus-within:ring-2 focus-within:ring-accent/30', className)}>
      <div ref={gutterRef} data-code-gutter aria-hidden
        className="shrink-0 select-none overflow-hidden border-r border-border/40 py-3 pl-2 pr-2 text-right font-mono text-[12px] leading-5 text-muted tabular-nums">
        {Array.from({ length: lines }, (_, k) => (
          <div key={k} data-error-line={markedSet.has(k + 1) ? k + 1 : undefined}
            className={markedSet.has(k + 1) ? '-mx-2 bg-warning/10 px-2 font-bold text-foreground dark:bg-warning/20' : undefined}>{k + 1}</div>
        ))}
      </div>
      <div className="relative min-w-0 flex-1">
        {/* 错误行色带: 垫在文字下面, 只跟纵向滚动走 (横向滚动时仍铺满可见宽度) */}
        <div data-code-marks aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
          <div ref={marksRef} className="relative">
            {marked.map(n => (
              <div key={n} data-mark-line={n} className="absolute inset-x-0 bg-warning/[0.07] shadow-[inset_3px_0_0_hsl(var(--warning))] dark:bg-warning/15"
                style={{ top: EDITOR_PAD_PX + (n - 1) * EDITOR_LINE_PX, height: EDITOR_LINE_PX }} />
            ))}
          </div>
        </div>
        <pre ref={preRef} data-code-highlight aria-hidden className={cn(EDITOR_TEXT, 'pointer-events-none absolute inset-0 overflow-hidden text-foreground')}>
          {highlighted}{'\n'}
        </pre>
        <textarea
          ref={inputRef}
          data-code-input aria-label={ariaLabel} value={value} readOnly={readOnly} spellCheck={false} wrap="off"
          autoCapitalize="off" autoCorrect="off" autoComplete="off"
          onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown}
          onScroll={(e) => syncScroll(e.currentTarget)}
          className={cn(EDITOR_TEXT, 'absolute inset-0 h-full w-full resize-none overflow-auto border-0 bg-transparent text-transparent caret-foreground outline-none selection:bg-accent/30')}
        />
      </div>
    </div>
  )
})
