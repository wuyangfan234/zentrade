/**
 * 创建策略弹窗「自定义编写」页签的三块: 左栏模板库、右栏分项校验、指南 / 语法标准抽屉。
 * 数据全部来自后端 (/api/strategies/code/kit 与 /code/check), 这里只做展示;
 * 模板的行数、参数个数与校验的行号都由后端从源码算出。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'
import { DOC_LINKS } from '@/lib/docsLinks'
import type { StrategyCheckResult, StrategyStandardDigest, StrategyTemplate } from '@/lib/api'

export type AutoCheck = { state: 'idle' | 'checking' | 'ok' | 'bad'; error?: string; result?: StrategyCheckResult }
export type DrawerTarget = { tab: 'standard' | 'guide'; section: number | null }

const HEAD = 'flex h-[38px] shrink-0 items-center gap-2 border-b border-border/40 px-3 text-[13px] font-semibold text-foreground'
const TAG = 'rounded-[4px] border px-1.5 text-[11px] font-medium leading-[18px] whitespace-nowrap'
export const TAG_OK = cn(TAG, 'border-emerald-600/40 text-emerald-700 dark:border-emerald-400/40 dark:text-emerald-400')
export const TAG_WARN = cn(TAG, 'border-warning/60 text-warning')
export const TAG_MUTED = cn(TAG, 'border-border text-muted')
const LINK = 'cursor-pointer text-accent hover:underline dark:text-blue-400'

// ───────────────────────── 左栏: 模板库 ─────────────────────────

export function TemplateColumn({ templates, code, dirty, onUse, className }: {
  templates: StrategyTemplate[]
  /** 当前编辑器里的代码: 与某个模板完全相同时该模板标为选中 */
  code: string
  /** 用户写过自己的代码: 换模板前要再点一次确认 */
  dirty: boolean
  onUse: (t: StrategyTemplate) => void
  className?: string
}) {
  const [kind, setKind] = useState<string | null>(null)
  const [armed, setArmed] = useState<string | null>(null)
  const kinds = useMemo(() => {
    const out: { kind: string; n: number }[] = []
    for (const t of templates) {
      const hit = out.find(k => k.kind === t.kind)
      if (hit) hit.n += 1
      else out.push({ kind: t.kind, n: 1 })
    }
    return out
  }, [templates])
  const shown = kind === null ? templates : templates.filter(t => t.kind === kind)
  useEffect(() => { setArmed(null) }, [code])

  const pick = (t: StrategyTemplate) => {
    if (t.code === code) return
    if (dirty && armed !== t.id) { setArmed(t.id); return }
    setArmed(null)
    onUse(t)
  }
  const chip = (on: boolean) => cn('cursor-pointer rounded-[5px] border px-1.5 text-[11px] leading-5 whitespace-nowrap',
    on ? 'border-accent/55 bg-accent/10 text-foreground' : 'border-border text-secondary hover:text-foreground')

  return (
    <aside data-col="tpl" className={cn('flex min-h-0 min-w-0 flex-col border-border/60 md:border-r max-md:border-b', className)}>
      <div className={HEAD}>模板<span className="text-[12px] font-normal text-secondary">{templates.length}</span></div>
      <div className="flex shrink-0 flex-wrap gap-[5px] px-3 py-2">
        <button type="button" data-kind="all" aria-pressed={kind === null} onClick={() => setKind(null)} className={chip(kind === null)}>全部 {templates.length}</button>
        {kinds.map(k => (
          <button type="button" key={k.kind} data-kind={k.kind} aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)} className={chip(kind === k.kind)}>{k.kind} {k.n}</button>
        ))}
      </div>
      <div data-tpl-list className="min-h-0 flex-1 overflow-y-auto px-2 pb-2 max-md:flex max-md:flex-none max-md:gap-1.5 max-md:overflow-x-auto max-md:px-3 max-md:pb-2.5">
        {templates.length === 0 && <div className="px-1 py-2 text-[12px] text-muted">加载中</div>}
        {shown.map(t => {
          const on = t.code === code
          const isArmed = armed === t.id
          return (
            <button type="button" key={t.id} data-tpl={t.id} data-on={on || undefined} data-armed={isArmed || undefined} aria-pressed={on} onClick={() => pick(t)}
              className={cn('mt-1.5 block w-full cursor-pointer rounded-[7px] border px-2.5 py-1.5 text-left max-md:mt-0 max-md:w-[150px] max-md:shrink-0',
                on ? 'border-accent bg-accent/10' : isArmed ? 'border-warning/60 bg-warning/10' : 'border-border hover:border-accent/50')}>
              <div className="flex items-center justify-between gap-1.5">
                <span className="truncate text-[13px] font-semibold text-foreground">{t.name}</span>
                <span className={t.level === '入门' ? TAG_MUTED : TAG_WARN}>{t.level}</span>
              </div>
              {isArmed
                ? <div className="mt-[3px] truncate text-[11px] text-warning">再点一次 · 替换当前代码</div>
                : (
                  <div className="mt-[3px] flex gap-2.5 whitespace-nowrap text-[11px] text-secondary">
                    <span>{t.kind}</span><span className="tabular-nums">{t.lines} 行</span><span className="tabular-nums">参数 {t.params}</span>
                  </div>
                )}
            </button>
          )
        })}
      </div>
    </aside>
  )
}

// ───────────────────────── 右栏: 分项校验 ─────────────────────────

const ROW_KEYS: { key: string; label: string }[] = [
  { key: 'safety', label: '安全' }, { key: 'meta', label: 'META' }, { key: 'entry', label: '入口' }, { key: 'fields', label: '字段' },
]

function pct(v: number | null | undefined): string {
  return typeof v === 'number' ? `${+(Math.abs(v) * 100).toFixed(1)}%` : '—'
}

export function CheckColumn({ check, onJump, onSection, className }: {
  check: AutoCheck
  onJump: (line: number) => void
  onSection: (section: number) => void
  className?: string
}) {
  const result = check.result
  const nBad = result ? result.items.filter(i => i.state === 'bad').length : 0
  const ov = result?.overview
  const stale = check.state === 'checking' && !!result
  const facts: [string, string][] = [
    ['行数', ov ? String(ov.lines) : '—'],
    ['参数', ov && ov.params !== null ? String(ov.params) : '—'],
    ['止损', pct(ov?.stop_loss)],
    ['最长持有', ov && ov.max_hold_days !== null ? `${ov.max_hold_days} 日` : '—'],
  ]
  return (
    <aside data-col="check" data-check-state={check.state} className={cn('flex min-h-0 min-w-0 flex-col border-border/60 md:border-l max-md:border-b', className)}>
      <div className={HEAD}>
        校验
        {check.state === 'checking' && <span data-check-badge className={TAG_MUTED}>校验中</span>}
        {check.state === 'ok' && <span data-check-badge className={TAG_OK}>全部通过</span>}
        {check.state === 'bad' && <span data-check-badge className={TAG_WARN}>{nBad > 0 ? `${nBad} 项未通过` : '未通过'}</span>}
        <span className="ml-auto text-[11px] font-normal text-muted">停笔自动</span>
      </div>
      {/* 校验中: 上一次的结果先留着 (不闪), 但压暗, 表示它说的是改动前的代码 */}
      <div data-check-rows data-stale={stale || undefined} className={cn('px-3 pb-2 pt-1 transition-opacity', stale && 'opacity-45')}>
        {ROW_KEYS.map((row, k) => {
          const item = result?.items.find(i => i.key === row.key)
          const state = item?.state ?? 'skip'
          return (
            <div key={row.key} data-check={row.key} data-state={state}
              className={cn('grid min-h-[38px] grid-cols-[10px_40px_minmax(0,1fr)_auto] items-center gap-x-2 text-[12px]', k > 0 && 'border-t border-border/40', state === 'bad' && 'gap-y-px py-[7px]')}>
              <span className={cn('h-2 w-2 rounded-full', state === 'ok' ? 'bg-emerald-500' : state === 'bad' ? 'bg-warning' : 'bg-border')} />
              <span className="whitespace-nowrap font-semibold text-foreground">{row.label}</span>
              {state === 'bad' && item ? (
                <>
                  <span />
                  {item.section !== null
                    ? <button type="button" data-check-ref={item.section} onClick={() => onSection(item.section!)} className={cn(LINK, 'whitespace-nowrap text-[12px]')}>标准 §{item.section}</button>
                    : <span />}
                  {item.line !== null ? (
                    <button type="button" data-check-line={item.line} title={item.detail || item.text} onClick={() => onJump(item.line!)}
                      className="col-[2/-1] cursor-pointer truncate text-left text-warning hover:underline">第 {item.line} 行 · {item.text}</button>
                  ) : (
                    <span title={item.detail || item.text} className="col-[2/-1] truncate text-warning">{item.text}</span>
                  )}
                </>
              ) : (
                <span className={cn('col-[3/-1] truncate', state === 'ok' ? 'text-secondary' : 'text-muted')}>{item ? item.text : '—'}</span>
              )}
            </div>
          )
        })}
      </div>
      <div className={cn(HEAD, 'border-t')}>概览</div>
      <div data-check-facts className={cn('grid shrink-0 grid-cols-2 gap-x-3 gap-y-2 px-3 pb-3 pt-2.5 transition-opacity', stale && 'opacity-45')}>
        {facts.map(([k, v]) => (
          <div key={k} className="flex flex-col">
            <span className="text-[11px] text-muted">{k}</span>
            <span className="text-[14px] font-semibold tabular-nums text-foreground">{v}</span>
          </div>
        ))}
      </div>
    </aside>
  )
}

// ───────────────────────── 抽屉: 语法标准 / 开发指南 ─────────────────────────

const BLOCK = 'border-b border-border/40 px-4 py-3'
const BLOCK_T = 'mb-2 text-[12px] font-semibold text-foreground'
const CHIP = 'rounded-[4px] border border-border px-1.5 font-mono text-[11px] leading-5 text-foreground whitespace-nowrap'
/** 表格里的代码片: 窄屏放不下时允许折行, 不撑出横向滚动 */
const CHIP_WRAP = 'inline-block rounded-[4px] border border-border px-1.5 font-mono text-[11px] leading-5 text-foreground [overflow-wrap:anywhere]'
/** 抽屉里有专门一块内容的章节; 其余章节只在目录里标出, 并给仓库原文链接 */
const SECTIONS_WITH_BLOCK = new Set([3, 5, 6])

export function StandardDrawer({ target, digest, onClose, onTab }: {
  target: DrawerTarget
  digest: StrategyStandardDigest | null
  onClose: () => void
  onTab: (tab: DrawerTarget['tab']) => void
}) {
  const bodyRef = useRef<HTMLDivElement>(null)
  const { tab, section } = target
  useEffect(() => {
    if (tab !== 'standard' || section === null) return
    const el = bodyRef.current?.querySelector<HTMLElement>(`[data-sec="${section}"]`)
    el?.scrollIntoView?.({ block: 'start' })
  }, [tab, section, digest])
  const seg = (on: boolean) => cn('cursor-pointer rounded-[4px] px-2.5 py-0.5 text-[12px] whitespace-nowrap', on ? 'bg-accent/15 text-foreground shadow-[inset_0_-2px_0_hsl(var(--accent))]' : 'text-secondary hover:text-foreground')
  const ring = (n: number) => (tab === 'standard' && section === n ? 'bg-accent/5 shadow-[inset_3px_0_0_hsl(var(--accent))]' : '')

  return (
    <Modal onClose={onClose} ariaLabel="策略语法标准与开发指南"
      overlayClassName="fixed inset-0 z-[60] flex justify-end bg-black/45"
      panelClassName="flex h-full w-[520px] max-w-full flex-col border-l border-border bg-surface shadow-2xl">
      <div data-drawer={tab} data-drawer-section={section ?? undefined} className="flex shrink-0 items-center gap-3 border-b border-border/50 px-4 py-2.5">
        <div className="inline-flex shrink-0 rounded-[6px] border border-border p-0.5">
          <button type="button" data-drawer-tab="standard" aria-pressed={tab === 'standard'} onClick={() => onTab('standard')} className={seg(tab === 'standard')}>语法标准</button>
          <button type="button" data-drawer-tab="guide" aria-pressed={tab === 'guide'} onClick={() => onTab('guide')} className={seg(tab === 'guide')}>开发指南</button>
        </div>
        <a data-drawer-source href={tab === 'standard' ? DOC_LINKS.strategyStandard : DOC_LINKS.strategyGuide} target="_blank" rel="noopener noreferrer"
          className={cn(LINK, 'ml-auto inline-flex items-center gap-1 whitespace-nowrap text-[12px]')}>仓库原文<ExternalLink className="h-3 w-3" /></a>
        <button type="button" onClick={onClose} className="cursor-pointer whitespace-nowrap text-[12px] text-secondary hover:text-foreground">关闭</button>
      </div>
      <div ref={bodyRef} className="min-h-0 flex-1 overflow-y-auto">
        {!digest && <div className="px-4 py-3 text-[12px] text-muted">加载中</div>}
        {digest && tab === 'standard' && (
          <>
            <div className={BLOCK}>
              <div className={BLOCK_T}>目录 · {digest.toc.length} 节</div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                {digest.toc.map(t => (
                  <div key={t.n} data-toc={t.n} data-on={section === t.n || undefined}
                    className={cn('h-6 truncate rounded-[4px] px-1.5 text-[12px] leading-6', section === t.n ? 'bg-accent/10 text-foreground' : 'text-secondary')}>
                    <span className="mr-1.5 text-muted tabular-nums">{t.n}</span>{t.title}
                  </div>
                ))}
              </div>
              {section !== null && !SECTIONS_WITH_BLOCK.has(section) && (
                <a data-drawer-section-link href={DOC_LINKS.strategyStandard} target="_blank" rel="noopener noreferrer"
                  className={cn(LINK, 'mt-2 inline-flex items-center gap-1 text-[12px]')}>§{section} 见仓库原文<ExternalLink className="h-3 w-3" /></a>
              )}
            </div>
            <div data-sec="3" className={cn(BLOCK, ring(3))}>
              <div className={BLOCK_T}>§3 执行方式 · {digest.backends.length} 选 1</div>
              <table className="w-full text-[12px]">
                <thead><tr className="text-left text-[11px] text-muted"><th className="h-7 font-normal">写法</th><th className="font-normal">必须</th><th className="font-normal">禁止</th></tr></thead>
                <tbody>
                  {digest.backends.map(b => (
                    <tr key={b.id} data-backend={b.id} className="border-t border-border/40">
                      <td className="h-8 whitespace-nowrap pr-2 text-foreground">{b.label}</td>
                      <td className="py-1 pr-2"><code className={CHIP_WRAP}>{b.must}</code></td>
                      <td className="py-1"><code className={cn(CHIP_WRAP, 'border-warning/55 text-warning')}>{b.forbid}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div data-sec="5" className={cn(BLOCK, ring(5))}>
              <div className={BLOCK_T}>§5 允许的 import · {digest.allowed_imports.length} 个</div>
              <div className="flex flex-wrap gap-[5px]">{digest.allowed_imports.map(x => <code key={x} className={CHIP}>{x}</code>)}</div>
            </div>
            <div data-sec="6" className={cn(BLOCK, ring(6))}>
              <div className={BLOCK_T}>§6 禁止的调用 · {digest.forbidden_calls.length} 个</div>
              <div className="flex flex-wrap gap-[5px]">{digest.forbidden_calls.map(x => <code key={x} className={cn(CHIP, 'border-warning/55 text-warning')}>{x}</code>)}</div>
              <div className={cn(BLOCK_T, 'mt-3')}>§6 禁止访问的属性 · {digest.forbidden_attrs.length} 个</div>
              <div className="flex flex-wrap gap-[5px]">{digest.forbidden_attrs.map(x => <code key={x} className={cn(CHIP, 'border-warning/55 text-warning')}>{x}</code>)}</div>
            </div>
          </>
        )}
        {digest && tab === 'guide' && (
          <div className={BLOCK}>
            <div className={BLOCK_T}>报错对照 · {digest.errors.length} 条</div>
            {digest.errors.map((e, k) => (
              <div key={e.sample} data-error-row className={cn('py-2 text-[12px]', k > 0 && 'border-t border-border/40')}>
                <div className="text-foreground">{e.meaning}</div>
                <div className="text-secondary"><span className="mr-1.5 text-muted">处理</span>{e.fix}</div>
                <code title={e.sample} className="mt-0.5 block truncate font-mono text-[11px] text-muted">{e.sample}</code>
              </div>
            ))}
          </div>
        )}
      </div>
    </Modal>
  )
}
