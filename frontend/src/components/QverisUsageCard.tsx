import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Coins, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const KIND_LABEL: Record<string, string> = { options_chain: '期权', earnings_estimates: '财报预期', earnings_transcript: '电话会' }
const TRIGGER_LABEL: Record<string, string> = { click: '点击', auto: '自动' }

/** QVeris 数据源卡片内的用量区: 余额 (免费查询, 60 秒缓存) / 今日用量 / 每日上限 (可改) / 最近调用。 */
export function QverisUsageCard() {
  const qc = useQueryClient()
  const usage = useQuery({ queryKey: QK.qverisUsage, queryFn: () => api.qverisUsage(), refetchInterval: 60_000 })
  const refresh = useMutation({
    mutationFn: () => api.qverisUsage(true),
    onSuccess: d => qc.setQueryData(QK.qverisUsage, d),
  })
  const [limitInput, setLimitInput] = useState('')
  const setLimit = useMutation({
    mutationFn: (v: number) => api.qverisSetLimit(v),
    onSuccess: d => { qc.setQueryData(QK.qverisUsage, d); setLimitInput('') },
  })
  const u = refresh.data ?? usage.data
  if (usage.isLoading) return <div className="text-xs text-muted">用量读取中…</div>
  if (!u) return <div className="text-xs text-danger">用量读取失败</div>
  const spendKnown = !u.error && u.spent_today != null && Number.isFinite(u.spent_today)
  const pct = spendKnown && u.daily_limit > 0 ? Math.min(1, u.spent_today! / u.daily_limit) : null
  const limitVal = Number(limitInput)
  return (
    <div data-qveris-usage className="space-y-3">
      <div className="flex items-center gap-2">
        <Coins className="h-3.5 w-3.5 text-secondary" />
        <h3 className="text-xs font-medium text-foreground">积分与用量</h3>
        <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}
          className="ml-auto inline-flex items-center gap-1 text-[11px] text-accent hover:underline disabled:opacity-50">
          {refresh.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}刷新余额
        </button>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded border border-border/60 px-2.5 py-2">
          <div className="text-[10px] text-muted">账户余额</div>
          <div data-balance className="text-[16px] font-semibold leading-6 tabular-nums text-foreground">
            {u.balance.remaining != null ? u.balance.remaining.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—'}
            <span className="ml-1 text-[11px] font-normal text-muted">积分</span>
          </div>
          {u.balance.error && <div className="text-[10px] text-danger">{u.balance.error}</div>}
        </div>
        <div className="rounded border border-border/60 px-2.5 py-2">
          <div className="text-[10px] text-muted">{`今日已用（美东 ${u.day}）`}</div>
          <div data-spent className="text-[16px] font-semibold leading-6 tabular-nums text-foreground">
            {spendKnown ? u.spent_today!.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '未知'}
            <span className="ml-1 text-[11px] font-normal text-muted">/ {u.daily_limit} 上限</span>
          </div>
          {pct != null && <div className="mt-1 h-1 rounded bg-elevated">
            <div className={`h-1 rounded ${pct >= 0.9 ? 'bg-danger' : 'bg-accent'}`} style={{ width: `${pct * 100}%` }} />
          </div>}
        </div>
      </div>
      {!spendKnown && <div role="alert" className="text-xs text-danger">
        额度未知，暂停收费查询。{u.error || '无法确定今日已用积分，请先核查账本。'}
      </div>}
      <form
        className="flex items-center gap-2 text-[11px]"
        onSubmit={e => { e.preventDefault(); if (limitInput && limitVal >= 0) setLimit.mutate(limitVal) }}
      >
        <span className="text-secondary">每日上限</span>
        <input value={limitInput} onChange={e => setLimitInput(e.target.value.replace(/[^0-9.]/g, ''))} placeholder={String(u.daily_limit)}
          className="h-7 w-20 rounded border border-border bg-transparent px-2 font-mono text-[11px]" />
        <button type="submit" disabled={!limitInput || setLimit.isPending}
          className="h-7 rounded bg-accent px-2.5 font-medium text-white disabled:opacity-40">保存</button>
        <span className="text-muted">超出后当天拒绝新的收费调用；缓存命中不计</span>
      </form>
      {setLimit.error && <div className="text-[11px] text-danger">{(setLimit.error as Error).message}</div>}
      {spendKnown && <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-secondary">
        {Object.keys(KIND_LABEL).map(k => (
          <span key={k}>{`${KIND_LABEL[k]} ${u.by_kind[k]?.calls ?? 0} 次 / ${u.by_kind[k]?.credits ?? 0} 积分`}</span>
        ))}
      </div>}
      {u.recent.length > 0 && (
        <div className="space-y-0.5 text-[10px] tabular-nums text-muted">
          {u.recent.slice(0, 6).map((r, i) => (
            <div key={i} className="flex justify-between gap-2">
              <span>{`${r.at.slice(5, 16).replace('T', ' ')} UTC · ${KIND_LABEL[r.kind] ?? r.kind} ${r.symbol} · ${TRIGGER_LABEL[r.trigger] ?? r.trigger}`}</span>
              <span className={r.ok ? '' : 'text-danger'}>{r.ok ? `${r.charged} 积分` : `失败 · ${r.charged} 积分`}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
