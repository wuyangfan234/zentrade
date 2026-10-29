import { useQuery } from '@tanstack/react-query'
import { api, type SwingReconcileRow, type SwingSettleProp, type SwingSettleRow, type SwingSettleSide, type SwingSettlePayload } from '@/lib/api'

/**
 * 卡结局 (0.26.0, 10-18 产品决策“机会页折中方案”): 每张上过板的卡的自动结局 (实际) 与 30 分钟K 回放 (回放), 多空分开。
 * 每个数并排给随时有效 95% 区间 —— 不设“攒够多少笔才算数”, 任何时候看都成立。
 * 实际与回放的区间不重叠 = 回放算错了 (撮合规则与纸面不一致), 该行标红: 先修回放, 再采信回放的数。
 */
const pc = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)
const r2 = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}`)
const ci = (c: number[] | null | undefined, f: (v: number) => string) => (c && c.length === 2 ? `${f(c[0])} ~ ${f(c[1])}` : '—')

const ROWS: ['fill' | 'win' | 'target' | 'stop' | 'time', string][] = [
  ['fill', '回区'], ['win', '赢'], ['target', '目标'], ['stop', '止损'], ['time', '到期'],
]

function Prop({ p }: { p?: SwingSettleProp | null }) {
  if (!p) return <span className="text-muted">—</span>
  return <span>{pc(p.p)}<span className="ml-1 text-muted">{ci(p.ci, (v) => pc(v))}</span></span>
}

function Side({ name, s }: { name: string; s: SwingSettleSide }) {
  const a = s.actual
  const b = s.replay
  const bad = new Set(s.mismatch)
  return (
    <section data-swing-settle-side={name} className="min-w-0 flex-1">
      <div className="mb-1 flex items-baseline gap-2">
        <h3 className={`text-sm font-semibold ${name === 'long' ? 'text-bull' : 'text-bear'}`}>{name === 'long' ? '做多' : '做空'}</h3>
        <span className="text-muted">{`实际 ${a.cards} 张 · 成交 ${a.filled}`}{s.void ? ` · 取消 ${s.void}` : ''}{b ? ` · 回放 ${b.cards} 张` : ''}</span>
      </div>
      <table className="w-full whitespace-nowrap">
        <thead className="text-muted">
          <tr className="border-b border-border">
            <th className="py-1 pr-3 text-left font-normal" />
            <th className="pr-3 text-right font-normal">实际</th>
            <th className="text-right font-normal">回放</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map(([k, label]) => (
            <tr key={k} data-settle-row={k} data-mismatch={bad.has(k) ? '' : undefined}
              className={`border-b border-border/50 ${bad.has(k) ? 'text-danger' : ''}`}>
              <td className="py-1 pr-3">{label}</td>
              <td className="pr-3 text-right"><Prop p={a[k]} /></td>
              <td className="text-right"><Prop p={b ? b[k] : null} /></td>
            </tr>
          ))}
          <tr data-settle-row="mean_r" data-mismatch={bad.has('mean_r') ? '' : undefined}
            className={bad.has('mean_r') ? 'text-danger' : ''}>
            <td className="py-1 pr-3">均 R</td>
            <td className="pr-3 text-right">{r2(a.mean_r.v)}<span className="ml-1 text-muted">{ci(a.mean_r.ci, r2)}</span></td>
            <td className="text-right">{b ? <>{r2(b.mean_r.v)}<span className="ml-1 text-muted">{ci(b.mean_r.ci, r2)}</span></> : '—'}</td>
          </tr>
        </tbody>
      </table>
    </section>
  )
}

const WHY: Record<string, string> = { target: '目标', stop: '止损', time: '到期', premarket_beyond_stop: '盘前越止损取消' }

const ISSUE: Record<string, string> = {
  signal_missing: '回放有、生产没出', signal_extra: '生产出了、回放没有',
  book_card_not_recomputed: '簿里有、重算板上没有', levels_differ: '价位对不上', no_signal_file: '当天没有信号表',
}

/** 对账角标的悬停说明: 逐条列不一致 (规则 · 票 · 方向) */
export function reconcileTitle(rec: SwingReconcileRow): string {
  const head = rec.signal_check === 'unavailable' ? '信号层未核（回放不可用）' : '信号层：生产 vs 回放逐票'
  const rows = rec.issues.slice(0, 12).map((x) => `${ISSUE[x.kind] ?? x.kind} ${x.rule ?? ''} ${x.symbol ?? ''}${x.side === 'short' ? ' 空' : ''}`.trim())
  return [head, rec.board_day ? `板层：${rec.board_day} 的簿` : '板层：无已落盘的簿', ...rows].join('\n')
}

export function SwingSettlement() {
  const q = useQuery<SwingSettlePayload>({ queryKey: ['opportunities', 'swing-settlement'], queryFn: () => api.swingSettlement(), refetchInterval: 300_000 })
  if (q.isError) return <div data-swing-settle-error className="py-6 text-sm text-danger">{(q.error as Error)?.message || '—'}</div>
  const d = q.data
  if (!d) return <div className="py-6 text-sm text-muted">…</div>
  const rec = d.reconcile?.[0]
  return (
    <div data-swing-settle className="text-xs tabular-nums">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-muted">
        <span>{`${d.card_contract} · 结算 ${d.contract}`}</span>
        <span>{`回放 ${d.base_window ? `${d.base_window[0]} ~ ${d.base_window[1]}` : '—'}`}</span>
        <span>{`区间 ${Math.round((1 - d.alpha) * 100)}% 随时有效`}</span>
        {rec && (
          <span data-reconcile={rec.ok ? 'ok' : 'bad'} className={rec.ok ? '' : 'text-danger'}
            title={reconcileTitle(rec)}>
            {`对账 ${rec.day.slice(5)} ${rec.ok ? '一致' : `${rec.issues.length} 处不一致`}`}
          </span>
        )}
        {rec?.zero_hit_rules && Object.keys(rec.zero_hit_rules).length > 0 && (
          <span data-zero-hit className="text-warning" title="连续 0 命中天数已超过这条规则在回放里的历史最长">
            {Object.entries(rec.zero_hit_rules).map(([k, v]) => `${k} 连续 ${v.days} 天 0 命中（历史最长 ${v.history_max}）`).join(' · ')}
          </span>
        )}
      </div>
      <div className="flex flex-col gap-4 lg:flex-row">
        <Side name="long" s={d.sides.long} />
        <Side name="short" s={d.sides.short} />
      </div>
      {d.recent.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full whitespace-nowrap">
            <thead className="text-muted">
              <tr className="border-b border-border">
                {['信号日', '', '票', '挂单区', '止损', '目标', '结局', 'R'].map((h, i) => (
                  <th key={`${h}-${i}`} className={`py-1 pr-3 font-normal ${i >= 3 ? 'text-right' : 'text-left'}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {d.recent.map((r: SwingSettleRow) => (
                <tr key={r.card_id} data-settle-card={r.symbol} className="border-b border-border/50">
                  <td className="py-1 pr-3">{r.signal_day.slice(5)}</td>
                  <td className="pr-3">{r.side === 'short' ? '空' : '多'}</td>
                  <td className="pr-3 font-semibold">{r.symbol}</td>
                  <td className="pr-3 text-right">{`${r.zone[0].toFixed(2)}–${r.zone[1].toFixed(2)}`}</td>
                  <td className="pr-3 text-right">{r.stop.toFixed(2)}</td>
                  <td className="pr-3 text-right">{r.target.toFixed(2)}</td>
                  <td className="pr-3 text-right">{r.status === 'no_fill' ? '未回区' : WHY[r.why ?? ''] ?? r.why}</td>
                  <td className="text-right">{r.status === 'closed' ? r2(r.r) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
