import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Combine, Save, Search } from 'lucide-react'
import { toast } from '@/components/Toast'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none'
const MAX_MEMBERS = 8

type WeightMode = 'manual' | 'equal'

export function FactorComposite() {
  const [query, setQuery] = useState('')
  const [members, setMembers] = useState<Record<string, number>>({})
  const [mode, setMode] = useState<WeightMode>('equal')
  const [label, setLabel] = useState('')
  const [saveId, setSaveId] = useState('')
  // 手动权重输入草稿: 允许编辑中清空, 失焦时再钳到 ≥ 0.1 (旧实现清空即被钳回 0.1, 再键入 2 得 0.12)
  const [weightDrafts, setWeightDrafts] = useState<Record<string, string>>({})

  const queryClient = useQueryClient()
  const lib = useQuery({
    queryKey: QK.factorLibrary('all'),
    queryFn: () => api.factorLibrary(),
  })
  const save = useMutation({
    // 均等模式提交等权 (旧实现提交手动权重, 界面却显示等权)
    mutationFn: () => api.factorCompositeCreate({
      id: saveId.trim() || undefined,
      label: label.trim(),
      members: mode === 'equal' ? Object.fromEntries(Object.keys(members).map(id => [id, 1])) : members,
    }),
    onSuccess: data => {
      toast(`已注册 ${data.id} · v${data.version}`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['factors-library'] })
      void queryClient.invalidateQueries({ queryKey: QK.factorColumns })
      setMembers({})
      setWeightDrafts({})
      setLabel('')
      setSaveId('')
    },
    onError: (error: Error) => toast(`注册失败 · ${error.message}`, 'error'),
  })

  const candidates = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    // 只列当前数据源能算的因子 (与检验页同口径): 财务/股本类成员会让复合值整列为空
    return (lib.data?.factors ?? []).filter(item =>
      (item.kind === 'base' || item.kind === 'virtual') && item.available !== false
    ).filter(item =>
      !keyword || `${item.id} ${item.label}`.toLowerCase().includes(keyword)
    )
  }, [lib.data, query])

  const memberList = Object.entries(members)
  const effectiveWeights = mode === 'equal'
    ? Object.fromEntries(memberList.map(([id]) => [id, 1 / Math.max(memberList.length, 1)]))
    : members

  const toggleMember = (id: string) => {
    setMembers(current => {
      const next = { ...current }
      if (id in next) delete next[id]
      else if (Object.keys(next).length < MAX_MEMBERS) next[id] = 1
      return next
    })
  }
  const editWeight = (id: string, text: string) => {
    setWeightDrafts(current => ({ ...current, [id]: text }))
    const value = Number(text)
    if (text.trim() !== '' && Number.isFinite(value) && value >= 0.1) {
      setMembers(current => ({ ...current, [id]: value }))
    }
  }
  const commitWeight = (id: string) => {
    const value = Number(weightDrafts[id])
    const next = Number.isFinite(value) && value >= 0.1 ? value : Math.max(0.1, members[id] ?? 1)
    setMembers(current => ({ ...current, [id]: next }))
    setWeightDrafts(current => {
      const rest = { ...current }
      delete rest[id]
      return rest
    })
  }
  const weightSum = Object.values(effectiveWeights).reduce((left, right) => left + right, 0)

  const labelOf = (id: string) =>
    (lib.data?.factors ?? []).find(item => item.id === id)?.label ?? id

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 overflow-hidden xl:grid-cols-[20rem_minmax(0,1fr)]">
      <section className="flex min-h-0 flex-col gap-2 overflow-hidden rounded-card border border-border bg-surface/80 p-3">
        <div className="flex items-center gap-2">
          <Search className="h-3.5 w-3.5 text-muted" />
          <input
            type="text"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索因子加入组合"
            className={INPUT_CLS}
          />
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-0.5">
          {lib.isLoading && <div className="py-4 text-center text-[10px] text-muted">加载中</div>}
          {candidates.map(item => {
            const selected = item.id in members
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => toggleMember(item.id)}
                disabled={!selected && memberList.length >= MAX_MEMBERS}
                className={`flex w-full items-center justify-between gap-2 rounded-btn border px-2 py-1.5 text-left text-[11px] transition-colors disabled:opacity-40 ${selected
                  ? 'border-accent/60 bg-accent/10 text-accent'
                  : 'border-border bg-surface text-secondary hover:border-accent/40'
                }`}
              >
                <span className="min-w-0 flex-1 truncate">
                  {item.label}
                  <span className="ml-1 font-mono text-[9px] text-muted">{item.id}</span>
                </span>
                {selected && <span aria-hidden>✓</span>}
              </button>
            )
          })}
        </div>
        <div className="border-t border-border pt-1.5 text-[10px] text-muted">
          已选 {memberList.length} / {MAX_MEMBERS}
        </div>
      </section>

      <section className="flex min-h-0 flex-col gap-3 overflow-y-auto rounded-card border border-border bg-surface/80 p-3">
        <div className="flex items-center gap-2">
          <Combine className="h-4 w-4 text-accent" />
          <span className="text-sm font-medium text-foreground">复合因子</span>
          <span className="text-[10px] text-muted">Σ 权重 × 截面 z 分</span>
        </div>

        {memberList.length === 0 ? (
          <div className="rounded-btn border border-dashed border-border px-3 py-8 text-center text-xs text-muted">
            选 2–{MAX_MEMBERS} 个因子
          </div>
        ) : (
          <>
            <div className="flex items-center gap-1.5 text-[11px]">
              <span className="text-muted">权重方式</span>
              {(['equal', 'manual'] as WeightMode[]).map(value => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setMode(value)}
                  className={`rounded-btn border px-2 py-0.5 font-medium transition-colors ${mode === value
                    ? 'border-accent/60 bg-accent/10 text-accent'
                    : 'border-border text-secondary hover:text-accent'
                  }`}
                >
                  {value === 'equal' ? '均等' : '手动'}
                </button>
              ))}
              {mode === 'manual' && (
                <span className="text-muted">权重和 {weightSum.toFixed(2)} · 归一</span>
              )}
            </div>
            <div className="space-y-1.5">
              {memberList.map(([id, rawWeight]) => (
                <div key={id} className="flex items-center gap-2 rounded-btn border border-border bg-base/40 px-2.5 py-1.5">
                  <span className="min-w-0 flex-1 truncate text-xs text-foreground">
                    {labelOf(id)} <span className="font-mono text-[10px] text-muted">{id}</span>
                  </span>
                  {mode === 'manual' ? (
                    <>
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={weightDrafts[id] ?? String(rawWeight)}
                        onChange={event => editWeight(id, event.target.value)}
                        onBlur={() => commitWeight(id)}
                        className="w-20 rounded-input border border-border bg-surface px-2 py-1 text-right font-mono text-xs"
                        aria-label={`${labelOf(id)} 权重`}
                      />
                      <button type="button" onClick={() => toggleMember(id)} className="text-[10px] text-muted hover:text-danger">移除</button>
                    </>
                  ) : (
                    <span className="font-mono text-xs text-secondary">{(1 / memberList.length).toFixed(3)}</span>
                  )}
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <input type="text" value={label} onChange={event => setLabel(event.target.value)} maxLength={32} placeholder="名称" aria-label="名称" className={`${INPUT_CLS} h-8 w-40`} />
              <input type="text" value={saveId} onChange={event => setSaveId(event.target.value)} maxLength={43} placeholder="id（选填）" aria-label="id" className={`${INPUT_CLS} h-8 w-44 font-mono`} />
              <button
                type="button"
                onClick={() => save.mutate()}
                disabled={!label.trim() || memberList.length < 2 || save.isPending}
                className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Save className="h-3.5 w-3.5" />
                {save.isPending ? '保存中' : '注册复合因子'}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
