import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { addCalendarDays, etToday } from '@/lib/etTime'
import { QK } from '@/lib/queryKeys'
import { MissingCapChip } from '@/lib/capability-labels'
import { DatePicker } from '@/components/DatePicker'

function todayStr() { return etToday() }
function daysAgo(n: number): string { return addCalendarDays(etToday(), -n) }

// hasCap: 日K批量能力当前是否可用 (路由矩阵判定, 生效源含插件/自定义源)
export function RepairDailyPanel({ hasCap, isRunning, latestDate, onStart }: {
  hasCap: boolean
  isRunning: boolean
  latestDate: string | null
  onStart: () => void
}) {
  const qc = useQueryClient()
  const hasBatchCap = hasCap

  // 默认起始日期: 最新数据往前推 30 天 (兼顾补缺口 + 复核近期数据, 成本不高)
  const [startDate, setStartDate] = useState(daysAgo(30))

  const repair = useMutation({
    mutationFn: () => api.repairDaily(startDate),
    onSuccess: () => {
      onStart()
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const today = todayStr()
  const canSubmit = hasBatchCap && !isRunning && !repair.isPending && startDate <= today
  const gapDays = latestDate
    ? Math.max(0, Math.round((Date.parse(today) - Date.parse(latestDate)) / 86400000))
    : null

  return (
    <div className="space-y-3">
      <div className="rounded-card bg-accent/8 border border-accent/20 p-3 space-y-1.5">
        <div className="text-xs text-foreground">
          数据有缺口时，重新同步所选日期至今的数据并重算指标
        </div>
        <div className="text-[11px] text-muted leading-relaxed">
          流程同盘后同步（日 K · 除权因子 · 指标重算）· 新数据覆盖旧值
        </div>
      </div>

      {/* 起始日期 — 用户主要操作,放上面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-secondary">起始日期</span>
        <DatePicker
          value={startDate}
          onChange={setStartDate}
          max={today}
          align="right"
          buttonClassName="tabular-nums text-xs"
        />
      </div>

      {/* 本地最新数据 — 参考信息,放下面 */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted">本地最新数据</span>
        <span className="tabular-nums text-xs text-secondary">
          {latestDate ?? '—'}
          {gapDays !== null && gapDays > 0 && (
            <span className="ml-2 text-warning">已落后 {gapDays} 天</span>
          )}
        </span>
      </div>

      <div className="text-[11px] text-muted -mt-1">
        将重拉 <span className="tabular-nums text-secondary">{startDate}</span>
        {' → '}<span className="tabular-nums text-secondary">{today}</span>(今天) 的美股日K · 除权并重算指标
      </div>

      <button
        onClick={() => repair.mutate()}
        disabled={!canSubmit}
        className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
      >
        {repair.isPending ? (
          <>
            <Loader2 className="h-3 w-3 animate-spin" />
            请求中…
          </>
        ) : (
          <>开始修正</>
        )}
      </button>

      {!hasBatchCap && (
        <span className="block text-center">
          <MissingCapChip capKey="kline.daily.batch" />
        </span>
      )}
    </div>
  )
}
