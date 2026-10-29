import { useState } from 'react'
import { OptionsHeatmap } from '@/components/OptionsHeatmap'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Coins, Loader2, RefreshCw } from 'lucide-react'
import { api, type EstimateRow, type QverisOptions, type QverisPaidMeta } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/**
 * QVeris 付费数据面板 (2026-10-02 产品决策: 期权 / 财报预期 / 电话会; 每日上限 100 积分)。
 * 打开面板只读缓存 (免费); 只有点击「查询」才可能花约 1 积分 (缓存仍新鲜时后端直接返回缓存, 不扣费)。
 */

const pct = (x: number | null | undefined, d = 1) => (x == null ? '—' : `${(x * 100).toFixed(d)}%`)
const num = (x: number | null | undefined, d = 2) => (x == null ? '—' : x.toLocaleString('en-US', { maximumFractionDigits: d }))

function etTime(iso?: string) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d) + ' 美东'
}

function useUsage() {
  return useQuery({ queryKey: QK.qverisUsage, queryFn: () => api.qverisUsage(), staleTime: 30_000 })
}

/** 按钮 + 费用说明: 显示「约 1 积分」与今日用量; 未配置 Key 时禁用并提示。 */
export function PaidFetchBar({ meta, pending, onFetch, label, error }: {
  meta?: QverisPaidMeta
  pending: boolean
  onFetch: () => void
  label: string
  error?: string | null
}) {
  const usage = useUsage()
  const u = usage.data
  const configured = u?.configured ?? true
  const ledgerBlocked = !!u && (!!u.error || u.spent_today == null)
  const freshCache = meta?.available && meta.fresh
  return (
    <div data-paid-bar className="flex flex-wrap items-center gap-2 rounded border border-border/60 bg-elevated/40 px-2.5 py-1.5 text-[11px]">
      <button
        type="button"
        onClick={onFetch}
        disabled={pending || !configured || ledgerBlocked}
        className="inline-flex h-6 items-center gap-1 rounded bg-accent px-2.5 font-medium text-white hover:bg-accent/90 disabled:opacity-50"
      >
        {pending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
        {label}
      </button>
      <span className="inline-flex items-center gap-1 text-secondary">
        <Coins className="h-3 w-3" />
        {freshCache ? '缓存仍新鲜 · 再点免费' : `约 ${meta?.cost ?? 1} 积分`}
      </span>
      {meta?.available && meta.fetched_at && (
        <span className="text-muted">
          {`取数 ${etTime(meta.fetched_at)}`}{meta.cached ? ' · 缓存' : meta.charged ? ` · 本次扣 ${meta.charged} 积分` : ''}
        </span>
      )}
      {u && (
        <span className="ml-auto text-muted tabular-nums">
          {u.configured
            ? `今日 ${num(u.spent_today, 2)}/${num(u.daily_limit, 0)} · 余额 ${u.balance.remaining != null ? num(u.balance.remaining, 2) : '—'}`
            : '未配置 QVeris Key（设置 → 数据源）'}
        </span>
      )}
      {ledgerBlocked && <span role="alert" className="basis-full text-danger">额度未知，暂停收费查询。{u?.error}</span>}
      {error && <span className="basis-full text-danger">{error}</span>}
    </div>
  )
}

function usePaid<T extends QverisPaidMeta>(key: readonly unknown[], read: () => Promise<T>, fetch: () => Promise<T>) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: key, queryFn: read, staleTime: 60_000 })
  const m = useMutation({
    mutationFn: fetch,
    onSuccess: (data) => {
      qc.setQueryData(key, data)
      qc.invalidateQueries({ queryKey: QK.qverisUsage })
    },
  })
  return { q, m, data: (m.data ?? q.data) as T | undefined }
}

// ── 期权 ───────────────────────────────────────────────────────

export function OptionsPanel({ symbol }: { symbol: string }) {
  const { q, m, data } = usePaid<QverisOptions>(QK.qverisOptions(symbol),
    () => api.qverisOptions(symbol, false), () => api.qverisOptions(symbol, true))
  const a = data?.analysis
  return (
    <div data-options-panel className="space-y-3 text-xs">
      <PaidFetchBar meta={data} pending={m.isPending} onFetch={() => m.mutate()} label="查询期权"
        error={m.error ? (m.error as Error).message : null} />
      {q.isLoading && <div className="py-4 text-muted">读取缓存…</div>}
      {data && !data.available && <div className="py-4 text-muted">尚未查询该股期权。点「查询期权」获取全链（含希腊值与隐含波动率）。</div>}
      {data?.available && a && !a.ok && <div className="py-4 text-warning">{a.reason ?? '期权数据不可用'}</div>}
      {data?.available && a?.ok && (
        <>
          <div className="text-[11px] text-muted">
            {`数据日 ${a.as_of ?? '—'} · ${a.contracts} 个合约 / ${a.expirations} 个到期日 · 现价 $${num(data.spot)}（${data.spot_source}）`}
          </div>
          {data.source === 'alpaca' && <div className="text-[10px] text-muted" data-options-source="alpaca">免费源 · 持仓量为上一交易日</div>}
          {a.heatmap && <OptionsHeatmap h={a.heatmap} />}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="认沽/认购（成交量）" value={num(a.pcr?.all.volume, 2)} hint={`持仓量 ${num(a.pcr?.all.open_interest, 2)}`} />
            <Stat label="近月认沽/认购" value={num(a.pcr?.front.volume, 2)} hint={a.pcr?.front_expiration ?? ''} />
            <Stat label="25Δ 偏斜" value={a.skew ? `${(a.skew.risk_reversal * 100).toFixed(1)} pt` : '—'}
              hint={a.skew ? `看跌 ${pct(a.skew.put25_iv)} / 看涨 ${pct(a.skew.call25_iv)}` : ''} />
            <Stat label="财报预期波动" value={a.earnings_move ? `±${pct(a.earnings_move.expected_move_pct)}` : '—'}
              hint={a.earnings_move ? `财报 ${a.earnings_move.earnings_date} → ${a.earnings_move.expiration} 到期` : (data.earnings_date ? `财报 ${data.earnings_date}` : '近期无财报')} />
          </div>

          <div>
            <div className="mb-1 text-[11px] text-secondary">期限结构（ATM 隐含波动率 · 跨式价格对应的预期波动）</div>
            <table data-term className="w-full text-[11px] tabular-nums">
              <thead className="text-muted"><tr><th className="text-left font-normal">到期</th><th className="text-right font-normal">天数</th>
                <th className="text-right font-normal">ATM 行权价</th><th className="text-right font-normal">隐含波动率</th>
                <th className="text-right font-normal">跨式</th><th className="text-right font-normal">预期波动</th></tr></thead>
              <tbody>
                {(a.term ?? []).slice(0, 10).map(t => (
                  <tr key={t.expiration} className="border-t border-border/40">
                    <td className="py-0.5">{t.expiration}</td><td className="text-right">{t.dte}</td>
                    <td className="text-right">{num(t.atm_strike)}</td><td className="text-right">{pct(t.atm_iv)}</td>
                    <td className="text-right">{t.straddle != null ? `$${num(t.straddle)}` : '—'}</td>
                    <td className="text-right">{t.expected_move_pct != null ? `±${pct(t.expected_move_pct)}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <div className="mb-1 text-[11px] text-secondary">{`持仓量集中（近 ${a.oi_walls?.expirations.length ?? 0} 个到期日合计）`}</div>
              <div className="space-y-0.5 tabular-nums">
                {(a.oi_walls?.calls_above ?? []).map(w => <div key={`c${w.strike}`}>上方看涨 <b className="text-foreground">{num(w.strike)}</b> · {w.open_interest.toLocaleString()} 张</div>)}
                {(a.oi_walls?.puts_below ?? []).map(w => <div key={`p${w.strike}`}>下方看跌 <b className="text-foreground">{num(w.strike)}</b> · {w.open_interest.toLocaleString()} 张</div>)}
              </div>
            </div>
            <div>
              <div className="mb-1 text-[11px] text-secondary">异常活跃合约（成交量/持仓量 ≥5 且成交量 ≥1000）</div>
              {(a.unusual ?? []).length === 0 ? <div className="text-muted">无</div> : (
                <div className="space-y-0.5 tabular-nums">
                  {(a.unusual ?? []).slice(0, 6).map(u => (
                    <div key={u.contract}>
                      <span className={u.type === 'call' ? 'text-bull' : 'text-bear'}>{u.type === 'call' ? '看涨' : '看跌'}</span>
                      {` ${u.expiration} ${num(u.strike)} · 量 ${u.volume.toLocaleString()} / 仓 ${u.open_interest.toLocaleString()}（${u.ratio}×）`}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="text-[10px] text-muted">期权数据反映市场定价与资金流向，不是方向判断；只用于展示与辅助判断，不参与开仓。</div>
        </>
      )}
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded border border-border/60 px-2 py-1.5">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="text-sm font-medium tabular-nums text-foreground">{value}</div>
      {hint && <div className="truncate text-[10px] text-muted" title={hint}>{hint}</div>}
    </div>
  )
}

// ── 财报: 一致预期 + 电话会 ─────────────────────────────────────────

function revision(r: EstimateRow) {
  const up = r.eps_up_30d ?? 0
  const down = r.eps_down_30d ?? 0
  return `${up} 上调 / ${down} 下调`
}

export function EarningsPanel({ symbol }: { symbol: string }) {
  const est = usePaid(QK.qverisEstimates(symbol), () => api.qverisEstimates(symbol, false), () => api.qverisEstimates(symbol, true))
  const e = est.data?.estimates
  return (
    <div data-earnings-panel className="space-y-4 text-xs">
      <section>
        <div className="mb-1.5 flex items-baseline justify-between">
          <h4 className="text-[12px] font-medium text-foreground">财报一致预期</h4>
          {est.data?.earnings_date && <span className="text-[11px] text-secondary">{`下次财报 ${est.data.earnings_date}`}</span>}
        </div>
        <PaidFetchBar meta={est.data} pending={est.m.isPending} onFetch={() => est.m.mutate()} label="查询预期"
          error={est.m.error ? (est.m.error as Error).message : null} />
        {est.data && !est.data.available && <div className="py-3 text-muted">尚未查询。10 个交易日内有财报的持仓与候选每天美东 08:45 自动查一次。</div>}
        {est.data?.available && e && (
          <div className="mt-2 space-y-2">
            {e.next && (
              <div data-next-estimate className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label={`EPS 共识（期末 ${e.next.date}）`} value={num(e.next.eps_avg, 4)} hint={`区间 ${num(e.next.eps_low, 4)}–${num(e.next.eps_high, 4)} · ${num(e.next.eps_analysts, 0)} 家`} />
                <Stat label="近 30 天修正" value={revision(e.next)} hint={e.next.eps_change_30d_pct != null ? `共识变化 ${pct(e.next.eps_change_30d_pct, 2)}` : ''} />
                <Stat label="营收共识" value={e.next.rev_avg != null ? `$${(e.next.rev_avg / 1e8).toFixed(2)} 亿` : '—'} hint={`${num(e.next.rev_analysts, 0)} 家`} />
                <Stat label="近 7 天修正" value={`${e.next.eps_up_7d ?? 0} 上调 / ${e.next.eps_down_7d ?? 0} 下调`} />
              </div>
            )}
            <table className="w-full text-[11px] tabular-nums">
              <thead className="text-muted"><tr><th className="text-left font-normal">期末</th><th className="text-left font-normal">类型</th>
                <th className="text-right font-normal">EPS 共识</th><th className="text-right font-normal">30 天前</th>
                <th className="text-right font-normal">营收共识</th><th className="text-right font-normal">30 天修正</th></tr></thead>
              <tbody>
                {[...e.quarters, ...e.years].map(r => (
                  <tr key={`${r.horizon}${r.date}`} className="border-t border-border/40">
                    <td className="py-0.5">{r.date}</td><td>{r.horizon.includes('year') ? '财年' : '财季'}</td>
                    <td className="text-right">{num(r.eps_avg, 4)}</td><td className="text-right">{num(r.eps_avg_30d_ago, 4)}</td>
                    <td className="text-right">{r.rev_avg != null ? `$${(r.rev_avg / 1e8).toFixed(2)} 亿` : '—'}</td>
                    <td className="text-right">{revision(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <TranscriptSection symbol={symbol} />
      <div className="text-[10px] text-muted">一致预期为分析师观点汇总；电话会情绪分来自数据源。只用于展示与 AI 分析参考，不参与开仓。</div>
    </div>
  )
}

function defaultQuarter(now = new Date()) {
  const y = now.getUTCFullYear()
  const q = Math.floor(now.getUTCMonth() / 3)
  return q === 0 ? `${y - 1}Q4` : `${y}Q${q}`
}

function TranscriptSection({ symbol }: { symbol: string }) {
  const qc = useQueryClient()
  const [quarter, setQuarter] = useState(defaultQuarter())
  const [open, setOpen] = useState(false)
  const t = usePaid(QK.qverisTranscript(symbol, quarter), () => api.qverisTranscript(symbol, quarter, false),
    () => api.qverisTranscript(symbol, quarter, true))
  const digest = useMutation({
    mutationFn: () => api.qverisDigest(symbol, quarter),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.qverisTranscript(symbol, quarter) }),
  })
  const d = t.data
  const points = digest.data?.digest.points ?? d?.digest?.points
  return (
    <section>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <h4 className="text-[12px] font-medium text-foreground">财报电话会</h4>
        <label className="text-[11px] text-secondary">季度
          <input value={quarter} onChange={ev => setQuarter(ev.target.value.toUpperCase())} maxLength={6}
            className="ml-1 w-[68px] rounded border border-border bg-transparent px-1 py-0.5 font-mono text-[11px]" />
        </label>
      </div>
      <PaidFetchBar meta={d} pending={t.m.isPending} onFetch={() => t.m.mutate()} label="查询电话会"
        error={t.m.error ? (t.m.error as Error).message : null} />
      {d && !d.available && <div className="py-3 text-muted">{d.reason === '尚未查询' ? '尚未查询该季度电话会（查询后永久缓存）。' : d.reason}</div>}
      {d?.available && (
        <div className="mt-2 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-secondary">
            <span>{`${d.segments?.length ?? 0} 段发言`}</span>
            {d.avg_sentiment != null && <span>{`平均情绪 ${d.avg_sentiment.toFixed(2)}`}</span>}
            {!points && (
              <button type="button" onClick={() => digest.mutate()} disabled={digest.isPending}
                className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 hover:bg-elevated disabled:opacity-50">
                {digest.isPending && <Loader2 className="h-3 w-3 animate-spin" />}AI 提炼要点（不扣积分）
              </button>
            )}
            {digest.error && <span className="text-danger">{(digest.error as Error).message}</span>}
          </div>
          {points && (
            <ul data-digest className="list-disc space-y-0.5 pl-4 text-[12px] leading-relaxed text-foreground">
              {points.map(p => <li key={p}>{p}</li>)}
            </ul>
          )}
          <button type="button" onClick={() => setOpen(v => !v)} className="text-[11px] text-accent hover:underline">
            {open ? '收起原文' : '查看原文（英文）'}
          </button>
          {open && (
            <div className="max-h-80 space-y-2 overflow-y-auto rounded border border-border/60 p-2">
              {(d.segments ?? []).map((s, i) => (
                <div key={i}>
                  <div className="text-[10px] text-muted">{`${s.speaker} · ${s.title}${s.sentiment != null ? ` · 情绪 ${s.sentiment}` : ''}`}</div>
                  <div className="text-[11px] leading-relaxed text-secondary">{s.content}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
