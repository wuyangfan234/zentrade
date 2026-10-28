/**
 * 配置清单 (0.42.0): 数据源 / 新闻 / AI / 推送一张表说清「配了没有、不配会怎样、去哪配」。
 * - SetupChecklistCard: 设置页「数据源」顶部完整清单, 含 Finnhub / SEC 联系邮箱两项的直接填写框
 *   (这两项没有插件卡片, 之前只能改 .env)。
 * - SetupBanner: 首页横幅。必需项缺失时常驻不可关; 只缺建议项时可关闭 (状态存后端)。
 * 数据只读 GET /api/settings/setup-checklist, 不含任何密钥原文。
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, CircleDashed, ExternalLink, ListChecks, Loader2, Save, X } from 'lucide-react'
import { api, type SetupChecklist, type SetupItem } from '@/lib/api'
import { cardFlashCls, useCardFlash } from '@/lib/useCardFlash'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const LEVEL_LABEL: Record<SetupItem['level'], string> = { required: '必需', recommended: '建议', optional: '可选' }
const GROUP_LABEL: Record<SetupItem['group'], string> = { market: '行情数据', news: '新闻与基本面', ai: 'AI 模型', notify: '推送' }
const GROUP_ORDER: SetupItem['group'][] = ['market', 'news', 'ai', 'notify']
const INLINE_EDIT: Partial<Record<string, { name: 'finnhub' | 'sec_user_agent'; placeholder: string; secret: boolean }>> = {
  finnhub: { name: 'finnhub', placeholder: 'Finnhub API Key', secret: true },
  sec_ua: { name: 'sec_user_agent', placeholder: 'ZenTrade you@example.com', secret: false },
}

export function useSetupChecklist() {
  return useQuery({ queryKey: QK.setupChecklist, queryFn: api.setupChecklist, staleTime: 60_000 })
}

function whereHref(where: string): string {
  return where ? `/${where}` : ''
}

function StatusIcon({ item }: { item: SetupItem }) {
  if (item.configured) return <CheckCircle2 className="h-4 w-4 text-bear shrink-0" aria-label="已配置" />
  if (item.level === 'required') return <AlertTriangle className="h-4 w-4 text-bull shrink-0" aria-label="未配置" />
  return <CircleDashed className="h-4 w-4 text-muted shrink-0" aria-label="未配置" />
}

function InlineSecret({ itemId, autoFocus = false }: { itemId: string; autoFocus?: boolean }) {
  const cfg = INLINE_EDIT[itemId]
  const qc = useQueryClient()
  const [value, setValue] = useState('')
  const save = useMutation({
    mutationFn: () => api.saveServiceSecret(cfg!.name, value),
    onSuccess: () => {
      setValue('')
      toast('已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.setupChecklist })
    },
    onError: (e: Error) => toast(`保存失败: ${e.message}`, 'error'),
  })
  if (!cfg) return null
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={e => { e.preventDefault(); if (value.trim()) save.mutate() }}
    >
      <input
        type={cfg.secret ? 'password' : 'text'}
        value={value}
        onChange={e => setValue(e.target.value)}
        placeholder={cfg.placeholder}
        aria-label={cfg.placeholder}
        autoComplete="off"
        autoFocus={autoFocus}
        className="h-8 min-w-0 flex-1 rounded-btn border border-border bg-base px-2.5 text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent"
      />
      <button
        type="submit"
        disabled={!value.trim() || save.isPending}
        className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs text-white disabled:opacity-50"
      >
        {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
        保存
      </button>
    </form>
  )
}

function ItemRow({ item, highlight }: { item: SetupItem; highlight?: string }) {
  const href = whereHref(item.where)
  const anchor = `setup-${item.id}`
  const { ref, flash } = useCardFlash(highlight, anchor)
  const targeted = highlight === anchor
  // 指向清单本行的链接 (Finnhub / SEC 在本行内直接填写) 不再显示「去配置」: 点了也只是原地不动
  const selfLink = item.where.includes(`highlight=${anchor}`)
  return (
    <li className="border-t border-border first:border-t-0">
    <div ref={ref} id={anchor} className={`flex gap-3 py-3 ${cardFlashCls(flash)}`}>
      <div className="pt-0.5"><StatusIcon item={item} /></div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium text-foreground">{item.label}</span>
          <span className={`rounded px-1.5 py-px text-[10px] ${item.level === 'required' ? 'bg-bull/10 text-bull' : 'bg-elevated text-secondary'}`}>
            {LEVEL_LABEL[item.level]}
          </span>
          <span className="text-[11px] text-muted">{item.configured ? `已配置${item.source ? ` · ${item.source}` : ''}` : '未配置'}</span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-secondary">{item.purpose}</p>
        {!item.configured && <p className="mt-1 text-xs leading-relaxed text-foreground/80">{item.impact}</p>}
        {item.configured && item.id === 'ai_news' && <p className="mt-1 text-xs leading-relaxed text-secondary">{item.impact}</p>}
        {item.warn && (
          <p className="mt-1.5 flex gap-1.5 rounded-btn bg-warning/10 px-2 py-1.5 text-xs leading-relaxed text-warning">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            <span>{item.warn}</span>
          </p>
        )}
        {item.note && <p className="mt-1 text-[11px] text-muted">{item.note}</p>}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          {item.env.length > 0 && (
            <span className="text-muted">.env：<code className="font-mono text-secondary">{item.env.join(' / ')}</code></span>
          )}
          {href && !selfLink && <Link to={href} className="text-accent hover:underline">去配置</Link>}
          {item.link && (
            <a href={item.link} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent hover:underline">
              申请 <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
        {INLINE_EDIT[item.id] && (item.configured
          ? <details open={targeted || undefined} className="mt-1.5 text-[11px]"><summary className="cursor-pointer text-accent">修改</summary><InlineSecret itemId={item.id} autoFocus={targeted} /></details>
          : <InlineSecret itemId={item.id} autoFocus={targeted} />)}
      </div>
    </div>
    </li>
  )
}

export function SetupChecklistCard({ highlight }: { highlight?: string } = {}) {
  const q = useSetupChecklist()
  const data: SetupChecklist | undefined = q.data
  const { ref: cardRef, flash: cardFlash } = useCardFlash(highlight, 'setup-checklist')
  return (
    <section ref={cardRef} id="setup-checklist" className={`rounded-card border border-border bg-surface p-5 ${cardFlashCls(cardFlash)}`}>
      <div className="mb-1 flex items-center gap-2.5">
        <ListChecks className="h-4 w-4 text-secondary shrink-0" />
        <h2 className="text-sm font-medium text-foreground">配置清单</h2>
        {data && (
          <span className="text-[11px] text-muted">
            {data.items.filter(i => i.configured).length}/{data.items.length} 已配置
          </span>
        )}
      </div>
      <p className="mb-3 text-[11px] text-muted">
        所有 Key 可以在页面填写（存 data/user_data/secrets.json，优先于 .env），也可以写进 .env 后重启。
      </p>
      {q.isLoading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
      {q.isError && <p className="text-xs text-bull">读取失败：{(q.error as Error).message}</p>}
      {data && GROUP_ORDER.map(g => {
        const rows = data.items.filter(i => i.group === g)
        if (!rows.length) return null
        return (
          <div key={g} className="mt-3 first:mt-0">
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted">{GROUP_LABEL[g]}</h3>
            <ul>{rows.map(i => <ItemRow key={i.id} item={i} highlight={highlight} />)}</ul>
          </div>
        )
      })}
    </section>
  )
}

/** requiredOnly: 机会页 (首页) 按「零句子」约定只在必需项缺失时出现 (那时页面本来就是空的)。 */
export function SetupBanner({ requiredOnly = false }: { requiredOnly?: boolean }) {
  const q = useSetupChecklist()
  const qc = useQueryClient()
  const dismiss = useMutation({
    mutationFn: () => api.setSetupBanner(true),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.setupChecklist }),
  })
  const data = q.data
  if (!data) return null
  const byId = new Map(data.items.map(i => [i.id, i]))
  const req = data.summary.required_missing
  const rec = data.summary.recommended_missing
  if (!req.length && (requiredOnly || !rec.length || data.dismissed)) return null
  const names = (ids: string[]) => ids.map(id => byId.get(id)?.label ?? id).join('、')
  const blocking = req.length > 0
  return (
    <div
      role="status"
      className={`mb-3 flex items-start gap-2.5 rounded-card border px-3.5 py-2.5 text-xs ${
        blocking ? 'border-bull/30 bg-bull/[0.06]' : 'border-warning/30 bg-warning/[0.06]'
      }`}
    >
      <AlertTriangle className={`h-4 w-4 shrink-0 mt-px ${blocking ? 'text-bull' : 'text-warning'}`} />
      <div className="min-w-0 flex-1 leading-relaxed text-secondary">
        {blocking
          ? <><span className="font-medium text-foreground">未配置：{names(req)}</span>。系统没有行情数据，机会板与纸面交易无法运行。</>
          : <><span className="font-medium text-foreground">建议配置：{names(rec)}</span>。不影响交易环，相关功能缺失或降级。</>}
        {' '}
        <Link to="/settings?tab=data-sources&highlight=setup-checklist" className="text-accent hover:underline">查看配置清单</Link>
      </div>
      {!blocking && (
        <button
          type="button"
          onClick={() => dismiss.mutate()}
          aria-label="关闭提示"
          className="shrink-0 rounded p-0.5 text-muted hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}
