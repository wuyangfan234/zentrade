import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'

type Props = {
  isRunning: boolean
  onStart: (jobId: string) => void
  purpose?: 'enriched' | 'turnover'
  historicalShareRows?: number
}

export function EnrichedRebuildPanel({
  isRunning,
  onStart,
  purpose = 'enriched',
  historicalShareRows = 0,
}: Props) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const batchSize = prefs.data?.enriched_batch_size ?? 1000
  const isTurnoverRebuild = purpose === 'turnover'
  const canRebuild = !isTurnoverRebuild || historicalShareRows > 0
  const [editing, setEditing] = useState(false)
  const [draftSize, setDraftSize] = useState(String(batchSize))
  const [hint, setHint] = useState<string | null>(null)

  function clampAndSave(raw: number) {
    if (isNaN(raw) || 1 > raw) { setHint('已自动设为最小值 1'); saveBatch.mutate(1); return }
    if (raw > 10000) { setHint('已自动设为上限 10000'); saveBatch.mutate(10000); return }
    setHint(null); saveBatch.mutate(raw)
  }

  const saveBatch = useMutation({
    mutationFn: (size: number) => api.updateEnrichedBatchSize(size),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      setEditing(false)
    },
  })
  const rebuild = useMutation({
    mutationFn: api.rebuildEnriched,
    onSuccess: ({ job_id }) => {
      onStart(job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-4">
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs font-medium text-foreground">批次大小</div>
            <div className="text-[11px] text-muted">每批计算的标的数量</div>
          </div>
          {editing ? (
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                value={draftSize}
                onChange={e => setDraftSize(e.target.value)}
                className="w-20 px-2 py-1 text-xs tabular-nums rounded-btn border border-border bg-surface text-foreground text-right focus:outline-none focus:border-accent"
                min={1}
                max={10000}
                autoFocus
                onKeyDown={e => {
                  if (e.key === 'Enter') clampAndSave(parseInt(draftSize))
                  if (e.key === 'Escape') { setEditing(false); setHint(null) }
                }}
              />
              <button
                onClick={() => clampAndSave(parseInt(draftSize))}
                disabled={saveBatch.isPending}
                className="px-2 py-1 text-[11px] rounded-btn bg-accent/15 text-accent dark:text-blue-400 hover:bg-accent/25 disabled:opacity-50 transition-colors"
              >
                {saveBatch.isPending ? '…' : '保存'}
              </button>
              <button
                onClick={() => setEditing(false)}
                className="px-2 py-1 text-[11px] rounded-btn bg-elevated text-muted hover:text-foreground transition-colors"
              >
                取消
              </button>
            </div>
          ) : (
            <button
              onClick={() => { setDraftSize(String(batchSize)); setEditing(true) }}
              className="px-2.5 py-1 rounded-btn border border-border bg-surface text-xs tabular-nums text-foreground hover:border-accent/50 transition-colors"
            >
              {batchSize} 只/批
            </button>
          )}
        </div>
        <div className="flex items-start gap-1.5 px-3 py-1.5 rounded-btn bg-warning/10 border border-warning/20">
          <span className="text-[11px] text-warning leading-relaxed">
            批次越大占用内存越多 · 内存不足时请调小
          </span>
        </div>
        {hint && (
          <div className="px-3 py-1 rounded-btn bg-accent/10 border border-accent/20 text-[11px] text-accent dark:text-blue-400">
            {hint}
          </div>
        )}
      </div>

      <div>
        {isTurnoverRebuild && (
          <div className={`mb-3 rounded-btn border px-3 py-2 text-[11px] leading-relaxed ${
            canRebuild
              ? 'border-accent/20 bg-accent/5 text-secondary'
              : 'border-warning/20 bg-warning/10 text-warning'
          }`}>
            {canRebuild
              ? `已检测到 ${historicalShareRows.toLocaleString()} 条历史股本记录 · 重算将按公告日匹配历史流通股本，覆盖全部技术指标`
              : '无历史股本数据 · 暂不能按历史股本重算'}
          </div>
        )}
        <div className="text-[11px] text-muted mb-2">
          {isTurnoverRebuild
            ? '基于现有日 K、除权因子与历史股本重新生成全部技术指标'
            : '基于已有日 K 与除权因子，全量重算前复权价格、技术指标和信号'}
        </div>
        <button
          onClick={() => rebuild.mutate()}
          disabled={!canRebuild || isRunning || rebuild.isPending}
          className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
        >
          {rebuild.isPending ? (
            <><Loader2 className="h-3 w-3 animate-spin" />计算中…</>
          ) : (
            <>{isTurnoverRebuild ? '重新计算并覆盖' : '全量计算'}</>
          )}
        </button>
        {rebuild.isError && (
          <div className="mt-2 text-[11px] text-danger">
            启动失败：{String((rebuild.error as Error)?.message ?? rebuild.error)}
          </div>
        )}
      </div>
    </div>
  )
}
