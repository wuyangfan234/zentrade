import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, FlaskConical, PenLine, Search, Sparkles, Trash2, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type FactorLibraryItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { GenerateFactorStrategyDialog } from './GenerateFactorStrategyDialog'

const INPUT_CLS = 'rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none'

const KIND_META: Record<FactorLibraryItem['kind'], { label: string; cls: string; tip: string }> = {
  base: { label: '基础', cls: 'bg-elevated text-secondary', tip: '基础因子：预计算指标' },
  virtual: { label: '虚拟', cls: 'bg-accent/10 text-accent', tip: '虚拟因子：按公式即时计算' },
  composite: { label: '复合', cls: 'bg-bull/10 text-bull', tip: '复合因子：多个因子加权合成' },
  custom: { label: '自定义', cls: 'bg-amber-400/10 text-amber-700 dark:text-amber-500', tip: '自定义因子：DSL 公式' },
}

// 0.47.0: 来源 = 内置 / 挖掘 / 采集 / 自写; 状态 = 自定义因子生命周期 (采集并入默认「观察」)
const ORIGIN_LABEL: Record<NonNullable<FactorLibraryItem['origin']>, string> = { builtin: '内置', mined: '挖掘', intake: '采集', custom: '自写' }
const STATUS_META: Record<NonNullable<FactorLibraryItem['status']>, { label: string; cls: string }> = {
  active: { label: '在用', cls: 'bg-elevated text-foreground' },
  watch: { label: '观察', cls: 'bg-accent/10 text-accent' },
  draft: { label: '草稿', cls: 'bg-elevated text-secondary' },
  retired: { label: '停用', cls: 'bg-elevated text-muted' },
}
const STATUS_ORDER = ['active', 'watch', 'retired', 'draft'] as const
const ASSET_LABEL: Record<string, string> = { stock: '股票', etf: 'ETF' }
const assetText = (types: string[]) => types.map(type => ASSET_LABEL[type] ?? type).join(' / ')
const statusLabel = (status: string | undefined) => STATUS_META[(status ?? 'active') as keyof typeof STATUS_META]?.label ?? status ?? ''

export function FactorLibrary({ onInspect, onEdit }: { onInspect: (factorId: string) => void; onEdit?: (factorId: string) => void }) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const [group, setGroup] = useState('all')
  const [origin, setOrigin] = useState('all')
  const [detail, setDetail] = useState<FactorLibraryItem | null>(null)

  const lib = useQuery({
    queryKey: QK.factorLibrary('all'),
    queryFn: () => api.factorLibrary(),
  })
  const factors = lib.data?.factors ?? []

  const groups = useMemo(
    () => Array.from(new Set(factors.map(item => item.group))),
    [factors],
  )
  // 选中的分组被改名/删空后回落「全部分组」(旧实现下拉显示全部、列表却仍按旧分组过滤成空)
  const groupValue = group === 'all' || groups.includes(group) ? group : 'all'

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return factors.filter(item => {
      if (kind !== 'all' && item.kind !== kind) return false
      if (groupValue !== 'all' && item.group !== groupValue) return false
      if (origin !== 'all' && (item.origin ?? 'builtin') !== origin) return false
      if (keyword && !`${item.id} ${item.label} ${item.formula}`.toLowerCase().includes(keyword)) return false
      return true
    })
  }, [factors, query, kind, groupValue, origin])

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface/80">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted" />
          <input
            type="text"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索因子 (id / 名称 / 公式)"
            className={`${INPUT_CLS} w-56 pl-7`}
          />
        </div>
        <select value={kind} onChange={event => setKind(event.target.value)} className={`${INPUT_CLS} w-24`} aria-label="因子类型">
          <option value="all">全部类型</option>
          <option value="base">基础</option>
          <option value="virtual">虚拟</option>
          <option value="composite">复合</option>
          <option value="custom">自定义</option>
        </select>
        <select value={origin} onChange={event => setOrigin(event.target.value)} className={`${INPUT_CLS} w-24`} aria-label="因子来源">
          <option value="all">全部来源</option>
          <option value="builtin">内置</option>
          <option value="mined">挖掘</option>
          <option value="intake">采集</option>
          <option value="custom">自写</option>
        </select>
        <select value={groupValue} onChange={event => setGroup(event.target.value)} className={`${INPUT_CLS} w-28`} aria-label="因子分组">
          <option value="all">全部分组</option>
          {groups.map(name => <option key={name} value={name}>{name}</option>)}
        </select>
        <span className="ml-auto text-[10px] text-muted">{lib.isLoading ? '加载中' : `${filtered.length} / ${factors.length} 个因子`}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {lib.isError && (
          <div className="m-3 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {String((lib.error as Error).message)}
          </div>
        )}
        {lib.isLoading && (
          <div className="p-6 text-center text-xs text-muted">加载中</div>
        )}
        {!lib.isLoading && !lib.isError && filtered.length === 0 && (
          <div className="p-6 text-center text-xs text-muted">无匹配因子</div>
        )}
        {!lib.isLoading && filtered.length > 0 && (
          <table className="w-full min-w-[760px] text-xs">
            <thead className="sticky top-0 bg-elevated text-left text-[11px] text-secondary">
              <tr>
                <th className="px-3 py-2.5 font-medium">因子</th>
                <th className="px-3 py-2.5 font-medium">来源</th>
                <th className="px-3 py-2.5 font-medium">状态</th>
                <th className="px-3 py-2.5 font-medium">分组</th>
                <th className="px-3 py-2.5 font-medium">类型</th>
                <th className="px-3 py-2.5 font-medium">公式</th>
                <th className="px-3 py-2.5 font-medium" title="最少历史交易日数">预热</th>
                <th className="px-3 py-2.5 font-medium">适用</th>
                <th className="w-24 px-3 py-2.5 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(item => (
                <tr
                  key={item.id}
                  onClick={() => setDetail(item)}
                  className="cursor-pointer border-t border-border/70 transition-colors hover:bg-elevated/40"
                >
                  <td className="px-3 py-2.5">
                    <div className="font-medium text-foreground">{item.label}</div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted">{item.id}</div>
                  </td>
                  <td className="px-3 py-2.5 text-secondary" data-origin={item.origin ?? 'builtin'}>{ORIGIN_LABEL[item.origin ?? 'builtin']}</td>
                  <td className="px-3 py-2.5">
                    <span className={`inline-flex rounded-btn px-1.5 py-0.5 text-[10px] font-medium ${STATUS_META[item.status ?? 'active'].cls}`}>{STATUS_META[item.status ?? 'active'].label}</span>
                  </td>
                  <td className="px-3 py-2.5 text-secondary">{item.group}</td>
                  <td className="px-3 py-2.5">
                    <span title={KIND_META[item.kind].tip} className={`inline-flex rounded-btn px-1.5 py-0.5 text-[10px] font-medium ${KIND_META[item.kind].cls}`}>
                      {KIND_META[item.kind].label}
                    </span>
                    {item.pit && <span className="ml-1 text-[10px] text-amber-700 dark:text-amber-500" title="点时数据: 仅使用公告日不晚于当日的财务数据">点时</span>}
                    {item.available === false && <span className="ml-1 text-[10px] text-muted" title="当前数据源未接入">缺数据</span>}
                  </td>
                  <td className="max-w-[22rem] px-3 py-2.5">
                    <span className="block truncate text-secondary" title={item.formula}>{item.formula}</span>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-secondary">{item.warmup_bars > 1 ? `${item.warmup_bars} 日` : <span title="无需滚动窗口">—</span>}</td>
                  <td className="px-3 py-2.5 text-muted">
                    {assetText(item.asset_types)}
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    {item.available !== false && (
                      <button
                        type="button"
                        onClick={event => { event.stopPropagation(); onInspect(item.id) }}
                        className="inline-flex items-center gap-1 rounded-btn px-2 py-1 text-[10px] text-accent transition-colors hover:bg-accent/10"
                      >
                        检验 <ArrowRight className="h-3 w-3" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {detail && <FactorDetailModal item={detail} onClose={() => setDetail(null)} onInspect={onInspect} onEdit={onEdit} />}
    </div>
  )
}

function FactorDetailModal({
  item,
  onClose,
  onInspect,
  onEdit,
}: {
  item: FactorLibraryItem
  onClose: () => void
  onInspect: (factorId: string) => void
  onEdit?: (factorId: string) => void
}) {
  const queryClient = useQueryClient()
  const setStatus = useMutation({
    mutationFn: (status: 'draft' | 'active' | 'watch' | 'retired') => api.factorSetStatus(item.id, status),
    onSuccess: data => {
      toast(`${item.label} · ${statusLabel(data.status)}`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['factors-library'] })
      void queryClient.invalidateQueries({ queryKey: QK.factorColumns })
      onClose()
    },
    onError: (error: Error) => toast(`状态更新失败 · ${error.message}`, 'error'),
  })
  const isDynamic = item.kind === 'custom' || item.kind === 'composite'
  const [groupDraft, setGroupDraft] = useState(item.group)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [blockedRefs, setBlockedRefs] = useState<string[] | null>(null)
  const [generateOpen, setGenerateOpen] = useState(false)
  const removeFactor = useMutation({
    mutationFn: (force: boolean) => api.factorDelete(item.id, force),
    onSuccess: data => {
      const refs = data.removed_references ?? []
      toast(
        refs.length > 0
          ? `${item.label} 已删除 · 引用 ${refs.length} 处失效`
          : `${item.label} 已删除`,
        'success',
      )
      void queryClient.invalidateQueries({ queryKey: ['factors-library'] })
      void queryClient.invalidateQueries({ queryKey: QK.factorColumns })
      onClose()
    },
    onError: (error: Error) => {
      // 409 时 detail 为 {message, references} → request() 已 stringify 成 JSON 文本
      try {
        const parsed = JSON.parse(error.message) as { references?: string[] }
        if (Array.isArray(parsed.references) && parsed.references.length > 0) {
          setBlockedRefs(parsed.references)
          return
        }
      } catch { /* 非 JSON 报错，走通用提示 */ }
      toast(`删除失败 · ${error.message}`, 'error')
    },
  })
  const setGroup = useMutation({
    mutationFn: () => api.factorSetGroup(item.id, groupDraft.trim()),
    onSuccess: data => {
      toast(`${item.label} · 分组 ${data.group}`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['factors-library'] })
      void queryClient.invalidateQueries({ queryKey: QK.factorColumns })
      onClose()
    },
    onError: (error: Error) => toast(`分组修改失败 · ${error.message}`, 'error'),
  })
  const rows: [string, string][] = [
    ['标识', item.id],
    ['名称', item.label],
    ['分组', item.group],
    ['类型', KIND_META[item.kind].label],
    ['公式', item.formula],
    ['版本', `v${item.version}`],
    ['预热', item.warmup_bars > 1 ? `${item.warmup_bars} 日` : '—'],
    ['点时数据', item.pit ? '是' : '否'],
    ['适用资产', assetText(item.asset_types)],
    ['跨标的可比', item.scale_free ? '是' : '否'],
    ['依赖列', item.dependencies.length > 0 ? item.dependencies.join(', ') : '—'],
    ['状态', statusLabel(item.status)],
    ...(item.available === false ? [['数据', '当前数据源未接入'] as [string, string]] : []),
    ...(item.description ? [['描述', item.description] as [string, string]] : []),
  ]
  const ROW_TITLE: Record<string, string> = {
    点时数据: '按公告日取数，不使用未公告数据',
    跨标的可比: '是 = 可直接截面排序；否 = 原值受价格尺度影响',
  }
  return (
    <Modal
      onClose={onClose}
      labelledBy="factor-detail-title"
      panelClassName="flex max-h-[86vh] w-[92vw] max-w-lg flex-col overflow-hidden border border-border bg-surface shadow-xl rounded-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FlaskConical className="h-4 w-4 shrink-0 text-accent" />
            <span id="factor-detail-title" className="truncate text-sm font-semibold text-foreground">{item.label}</span>
            <span title={KIND_META[item.kind].tip} className={`inline-flex shrink-0 rounded-btn px-1.5 py-0.5 text-[10px] font-medium ${KIND_META[item.kind].cls}`}>
              {KIND_META[item.kind].label}
            </span>
          </div>
          <div className="mt-1 font-mono text-[11px] text-muted">{item.id}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center text-muted transition-colors hover:text-foreground"
          aria-label="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <dl className="space-y-2">
          {rows.map(([key, value]) => (
            <div key={key} className="flex items-start gap-3 text-xs">
              <dt className="w-20 shrink-0 text-muted" title={ROW_TITLE[key]}>{key}</dt>
              <dd className="min-w-0 flex-1 break-words text-secondary">
                {key === '分组' && isDynamic ? (
                  <span className="flex items-center gap-1.5">
                    <input
                      type="text"
                      value={groupDraft}
                      maxLength={24}
                      onChange={event => setGroupDraft(event.target.value)}
                      className="h-6 w-32 rounded-input border border-border bg-base/60 px-1.5 text-xs text-foreground focus:border-accent focus:outline-none"
                      aria-label="修改分组"
                    />
                    <button
                      type="button"
                      onClick={() => setGroup.mutate()}
                      disabled={setGroup.isPending || !groupDraft.trim() || groupDraft.trim() === item.group}
                      className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40"
                    >
                      保存
                    </button>
                  </span>
                ) : value}
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3">
        {isDynamic && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted">状态</span>
            {STATUS_ORDER.map(status => {
              const current = (item.status ?? 'draft') === status
              return (
                <button
                  key={status}
                  type="button"
                  onClick={() => setStatus.mutate(status)}
                  disabled={setStatus.isPending || current}
                  aria-pressed={current}
                  className={`rounded-btn border px-2 py-0.5 text-[10px] transition-colors disabled:cursor-default ${current
                    ? 'border-accent/60 bg-accent/10 text-accent'
                    : 'border-border text-secondary hover:border-accent/40 hover:text-accent disabled:opacity-40'}`}
                >
                  {STATUS_META[status].label}
                </button>
              )
            })}
          </div>
        )}
        {item.kind === 'custom' && onEdit && (
          <button
            type="button"
            onClick={() => { onClose(); onEdit(item.id) }}
            className="inline-flex items-center gap-1 rounded-btn border border-accent/40 px-2.5 py-1.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent/10"
          >
            <PenLine className="h-3 w-3" />
            编辑公式
          </button>
        )}
        {item.available !== false && (
          <>
            <button
              type="button"
              onClick={() => setGenerateOpen(true)}
              className="inline-flex items-center gap-1 rounded-btn border border-accent/40 px-2.5 py-1.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent/10"
            >
              <Sparkles className="h-3 w-3" />
              生成策略
            </button>
            <button
              type="button"
              onClick={() => { onClose(); onInspect(item.id) }}
              className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-btn bg-accent px-2.5 py-1.5 text-[11px] font-medium text-white transition-colors hover:bg-accent/90"
            >
              检验 <ArrowRight className="h-3 w-3" />
            </button>
          </>
        )}
        {isDynamic && (
          confirmDelete ? (
            <>
              <button
                type="button"
                onClick={() => removeFactor.mutate(false)}
                disabled={removeFactor.isPending}
                className="inline-flex shrink-0 items-center gap-1 rounded-btn bg-danger px-2.5 py-1.5 text-[11px] font-medium text-white transition-colors hover:bg-danger/90 disabled:opacity-40"
              >
                {removeFactor.isPending ? '删除中' : '确认删除'}
              </button>
              <button
                type="button"
                onClick={() => { setConfirmDelete(false); setBlockedRefs(null) }}
                className="inline-flex shrink-0 items-center rounded-btn border border-border px-2.5 py-1.5 text-[11px] text-secondary transition-colors hover:text-foreground"
              >
                取消
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-danger/40 px-2.5 py-1.5 text-[11px] font-medium text-danger transition-colors hover:bg-danger/10"
            >
              <Trash2 className="h-3 w-3" />
              删除
            </button>
          )
        )}
        {blockedRefs && (
          <div className="w-full rounded-btn border border-danger/40 bg-danger/10 px-3 py-2 text-[11px] leading-relaxed text-danger">
            <div className="font-medium">引用 {blockedRefs.length} 处 · 删除后无法计算</div>
            <div className="mt-1 font-mono text-[10px] break-all text-danger">{blockedRefs.join('、')}</div>
            <div className="mt-2 flex items-center gap-2">
              <button
                type="button"
                onClick={() => removeFactor.mutate(true)}
                disabled={removeFactor.isPending}
                className="rounded-btn bg-danger px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-danger/90 disabled:opacity-40"
              >
                {removeFactor.isPending ? '删除中' : '仍要强制删除'}
              </button>
              <button
                type="button"
                onClick={() => setBlockedRefs(null)}
                className="text-[11px] text-secondary underline-offset-2 transition-colors hover:text-foreground hover:underline"
              >
                收起
              </button>
            </div>
          </div>
        )}
      </div>
      {generateOpen && (
        <GenerateFactorStrategyDialog item={item} onClose={() => setGenerateOpen(false)} />
      )}
    </Modal>
  )
}
