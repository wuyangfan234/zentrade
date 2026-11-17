/**
 * 情报页六宫格 (0.57.0): 卡片外壳 + 模型状态卡 / 抽屉。
 * - 每张卡同一结构: 标题行 (固定高) → 结论行 (固定高) → 明细 (卡内滚动) → 事后条 (只在历史日打开时出现)。
 *   同排卡由网格行高拉齐; 标题行与结论行高度写死, 各卡分隔线在同一条水平线上。
 * - 模型状态只展示排名模型的健康状况 (冠军 / 挑战者 / 前向 / 覆盖 / 漂移), 不放名单, 不进下单。
 */
import type { ReactNode } from 'react'
import type { IntelModel, IntelPostItem, LsSeriesLite } from '@/lib/api'
import { badge } from '@/components/ui/badge'
import { Sheet } from '@/components/opportunities/Sheet'

export const GRID_CARD = 'flex min-h-0 min-w-0 flex-col overflow-hidden rounded-card border border-border bg-surface max-md:max-h-[460px]'
export const CARD_HEAD = 'flex h-9 shrink-0 items-center gap-2 px-3'
export const CARD_LEAD = 'flex h-[52px] shrink-0 flex-wrap content-start items-baseline gap-x-2.5 gap-y-0 overflow-hidden border-b border-border/70 px-3'
export const CARD_BODY = 'flex min-h-0 flex-1 flex-col px-3 py-2'
export const CARD_SCROLL = 'overflow-y-auto scroll-y-visible'

const md = (s?: string | null) => (s ? s.slice(5, 10) : '—')
export const fmtAtr = (v?: number | null, dp = 2) => {
  if (v == null) return '—'
  const t = Math.abs(v).toFixed(dp)
  return Number(t) === 0 ? t : `${v > 0 ? '+' : '−'}${t}`
}
const pctTxt = (v: number) => {
  const t = Math.abs(v).toFixed(2)
  return Number(t) === 0 ? `${t}%` : `${v > 0 ? '+' : '−'}${t}%`
}
const sgn = (v?: number | null) => (v == null || Number(Math.abs(v).toFixed(2)) === 0 ? 'text-secondary' : v > 0 ? 'text-bull' : 'text-bear')

export function Missing({ label = '当天未存档' }: { label?: string }) {
  return (
    <div data-missing className="flex min-h-[96px] flex-1 items-center justify-center rounded-btn border border-dashed border-muted text-xs text-secondary">
      {label}
    </div>
  )
}

export function PostStrip({ items }: { items: IntelPostItem[] }) {
  return (
    <div data-card-post className="flex shrink-0 flex-wrap items-center gap-x-3.5 gap-y-1 border-t border-dashed border-muted bg-base px-3 py-1.5 text-xs text-secondary">
      <span className="rounded-[4px] border border-warning/60 px-1 text-[11px] font-medium leading-4 text-warning">事后</span>
      {items.length === 0 && <span>无结算项</span>}
      {items.map((it) => (
        <span key={it.label} className="whitespace-nowrap">
          {it.label}{' '}
          <b className={`font-semibold tabular-nums ${sgn(it.pct ?? it.atr)}`}>{it.pct != null ? pctTxt(it.pct) : `${fmtAtr(it.atr)} ATR`}</b>
        </span>
      ))}
    </div>
  )
}

export function GridCard({ k, title, tags, lead, children, post, onExpand, scroll = true, onBodyClick }: {
  k: string; title: string; tags?: ReactNode; lead: ReactNode; children: ReactNode
  /** 历史日且「事后」打开时给数组 (可为空); 其余不给 */
  post?: IntelPostItem[] | null; onExpand?: () => void
  /** false = 明细自己管滚动 (日历 / 新闻: 筛选条不随列表滚走) */
  scroll?: boolean; onBodyClick?: () => void
}) {
  return (
    <section data-intel={k} data-grid-card className={GRID_CARD}>
      <div data-card-head className={CARD_HEAD}>
        <h2 className="shrink-0 whitespace-nowrap text-[13px] font-semibold text-foreground">{title}</h2>
        <div className="flex min-w-0 items-center gap-2 overflow-hidden">{tags}</div>
        {onExpand && (
          <button type="button" data-expand={k} onClick={onExpand} className="ml-auto shrink-0 whitespace-nowrap text-[11px] text-accent hover:underline dark:text-blue-400">展开</button>
        )}
      </div>
      <div data-card-lead className={CARD_LEAD}>{lead}</div>
      <div data-card-body className={`${CARD_BODY} ${scroll ? CARD_SCROLL : ''} ${onBodyClick ? 'cursor-pointer' : ''}`} onClick={onBodyClick}>{children}</div>
      {post && <PostStrip items={post} />}
    </section>
  )
}

/** 结论行: 一个大字 + 两三个短读数 */
export function Lead({ big, cls, children }: { big: ReactNode; cls?: string; children?: ReactNode }) {
  return (
    <>
      <span data-lead-big className={`whitespace-nowrap text-[20px] font-bold leading-7 tabular-nums ${cls ?? 'text-foreground'}`}>{big}</span>
      {children}
    </>
  )
}
export const LEAD_SUB = 'whitespace-nowrap text-xs text-secondary'

// ── 模型状态 ──────────────────────────────────────────────────────

export const FEATURE_ZH: Record<string, string> = {
  rev1: '昨日反转', ibs: '日内位置', rev5: '5 日反转', mom: '动量', hksoc: '日内收益', on20: '隔夜收益', atrp: '波动占比', ladv: '成交额',
}
const ROLE_ZH: Record<string, string> = { champion: '冠军', challenger: '对照中', archived: '归档', rejected: '未晋级', promoted: '已晋级' }
const COMPARE: { k: keyof LsSeriesLite; label: string; lowBetter?: boolean; gate?: boolean }[] = [
  { k: 'long_minus_all', label: '看多 − 自选全体', gate: true },
  { k: 'long', label: '看多每笔' },
  { k: 'short', label: '看空每笔 · 越低越好', lowBetter: true },
]

type DriftRow = NonNullable<IntelModel['drift']>['rows'][number]
/** 注意线: 该特征自己的线 (过去一年里 20 日小窗的最高 PSI, 不低于底线); 旧文件没有就用底线 */
export const driftLine = (r: DriftRow, floor = 0.25) => r.line ?? floor
export const driftWarn = (r: DriftRow, floor = 0.25) => r.warn ?? r.psi >= driftLine(r, floor)

export function driftCount(m: IntelModel) {
  const rows = m.drift?.rows ?? []
  const warn = rows.filter((r) => driftWarn(r, m.drift?.warn)).length
  return { n: rows.length, warn, ok: rows.length - warn }
}

export function challengerBadge(m: IntelModel) {
  const c = m.challenger
  if (!c) return <span data-model-badge="none" className={badge('limit')}>无挑战者</span>
  if (c.state === 'running') return <span data-model-badge="running" className={badge('status')}>挑战者对照中</span>
  if (c.state === 'promoted') return <span data-model-badge="promoted" className={badge('status', 'ok')}>挑战者已晋级</span>
  return <span data-model-badge="rejected" className={badge('warn')}>挑战者未晋级</span>
}

export function failText(m: IntelModel) {
  const f = m.forward
  if (f.failed === true) return '已触发'
  if (f.failed === false) return `第 ${f.check_days} 日 · 未触发`
  return `未到期 · ${f.days} / ${f.check_days} 日`
}

function Kv({ k, label, children, warn }: { k: string; label: string; children: ReactNode; warn?: boolean }) {
  return (
    <div data-model-kv={k} className="grid min-h-[30px] flex-1 grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-2 border-t border-border/55 text-xs first:border-t-0 md:max-h-[48px]">
      <span className="whitespace-nowrap text-secondary">{label}</span>
      <span className={`truncate text-right tabular-nums ${warn ? 'text-warning' : 'text-foreground'}`}>{children}</span>
    </div>
  )
}

export function ModelBody({ m }: { m: IntelModel }) {
  const f = m.forward
  const c = m.challenger
  const dc = driftCount(m)
  const lma = f.total?.long_minus_all
  return (
    <div data-model-body className="flex min-h-0 flex-1 flex-col">
      <Kv k="forward" label="前向">{f.days > 0 ? `${f.days} 日 · 看多 − 自选全体 ${fmtAtr(lma)} ATR` : '未结算'}</Kv>
      <Kv k="fail" label="失败判据" warn={f.failed === true}>{failText(m)}</Kv>
      <Kv k="challenger" label="挑战者">
        {!c ? '无' : c.state === 'running' ? `${c.name} · 同窗 ${c.n} / ${c.gate_n} 份`
          : `${c.name} · ${fmtAtr(c.chal?.long_minus_all)} 对 ${fmtAtr(c.champ?.long_minus_all)}`}
      </Kv>
      <Kv k="verdict" label="裁决" warn={c?.state === 'rejected'}>
        {!c ? '—' : c.state === 'running' ? '未到期' : `${md(c.decided)} · ${c.state === 'promoted' ? '已晋级' : '保留冠军'}`}
      </Kv>
      <Kv k="coverage" label="覆盖">{`${m.coverage.ranked} / ${m.coverage.total} · 新股 ${m.coverage.young}`}</Kv>
      <Kv k="drift" label="特征漂移">
        {dc.n === 0 ? (m.asof ? '未存档' : '待盘后计算') : <>{`${dc.ok} 正常 · `}<span className={dc.warn ? 'text-warning' : ''}>{`${dc.warn} 注意`}</span></>}
      </Kv>
      <Kv k="retrain" label="下次重训">{`${m.next_retrain.quarter} · ${md(m.next_retrain.train_end)} 后`}</Kv>
    </div>
  )
}

const TH = 'h-7 px-2 text-[11px] font-normal text-secondary'
const TD = 'h-8 px-2 tabular-nums'

export function ModelSheet({ m, onClose }: { m: IntelModel; onClose: () => void }) {
  const c = m.challenger
  const d = m.drift
  const sum: { k: string; label: string; v: string; s: string; warn?: boolean }[] = [
    { k: 'champion', label: '冠军', v: m.champion.name, s: `训练截至 ${md(m.champion.trained_to)}` },
    { k: 'challenger', label: '挑战者', v: c ? c.name : '无', s: c ? `训练截至 ${md(c.trained_to)}` : `下次重训 ${m.next_retrain.quarter}` },
    { k: 'verdict', label: c?.decided ? `裁决 ${md(c.decided)}` : '裁决', warn: c?.state === 'rejected',
      v: !c ? '—' : c.state === 'running' ? '对照中' : c.state === 'promoted' ? '已晋级' : '保留冠军', s: c ? `同窗 ${c.n} / ${c.gate_n} 份` : `门槛 ${m.gate.n} 份` },
  ]
  return (
    <Sheet attr={{ 'data-model-sheet': '' }} onClose={onClose}
      title={<span className="flex items-center gap-2">模型状态<span className={badge('limit')}>仅参考</span>{m.asof && <span className={badge('warn')}>{`历史日 ${md(m.asof)}`}</span>}</span>}>
      <div className="space-y-4 text-xs">
        <div data-model-sum className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {sum.map((x) => (
            <div key={x.k} data-model-sum-cell={x.k} className="flex min-w-0 flex-col rounded-btn border border-border px-2.5 py-1.5">
              <span className="text-[11px] text-secondary">{x.label}</span>
              <span className={`whitespace-nowrap text-[14px] font-bold ${x.warn ? 'text-warning' : 'text-foreground'}`}>{x.v}</span>
              <span className="whitespace-nowrap text-[11px] text-muted">{x.s}</span>
            </div>
          ))}
        </div>

        <section data-model-compare>
          <div className="mb-1 text-[11px] font-medium text-secondary">{`同窗对照 · 每笔 ATR${c?.since ? ` · ${md(c.since)} 起` : ''}`}</div>
          {!c ? <div className="rounded-btn border border-dashed border-muted px-3 py-3 text-secondary">{`无挑战者 · 下次重训 ${m.next_retrain.quarter}（${md(m.next_retrain.train_end)} 后）`}</div> : (
            <table className="w-full">
              <thead><tr className="border-b border-border text-left"><th className={TH}>指标</th><th className={`${TH} text-right`}>冠军</th><th className={`${TH} text-right`}>挑战者</th><th className={`${TH} text-right`}>结果</th></tr></thead>
              <tbody>
                {COMPARE.map((r) => {
                  const a = c.champ?.[r.k] as number | null | undefined
                  const b = c.chal?.[r.k] as number | null | undefined
                  const ok = a == null || b == null ? null : r.lowBetter ? b <= a : b >= a
                  return (
                    <tr key={r.k} data-compare={r.k} className={`border-b border-border/60 last:border-0 ${r.gate ? 'bg-accent/[0.06]' : ''}`}>
                      <td className={`${TD} whitespace-nowrap`}>{r.label}{r.gate && <span className={`${badge('limit')} ml-1.5`}>门槛</span>}</td>
                      <td className={`${TD} text-right`}>{fmtAtr(a)}</td>
                      <td className={`${TD} text-right`}>{fmtAtr(b)}</td>
                      <td className={`${TD} whitespace-nowrap text-right ${ok === false ? 'text-warning' : 'text-secondary'}`}>{ok == null ? '—' : ok ? '不低于冠军' : '低于冠军'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>

        <section data-model-forward>
          <div className="mb-1 text-[11px] font-medium text-secondary">{`冠军前向 · ${m.champion.since ? `${md(m.champion.since)} 起` : ''}`}</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
            {[
              ['已结算', `${m.forward.days} / ${m.forward.check_days} 日`],
              ['看多 − 自选全体', `${fmtAtr(m.forward.total?.long_minus_all)} ATR`],
              ['回放同项', m.backtest.long_minus_all != null ? `${fmtAtr(m.backtest.long_minus_all)} ATR` : '—'],
              ['看多每笔', `${fmtAtr(m.forward.total?.long)} ATR`],
              ['看空每笔', `${fmtAtr(m.forward.total?.short)} ATR`],
              ['失败判据', failText(m)],
            ].map(([k, v]) => (
              <div key={k} className="flex items-baseline justify-between gap-2 border-b border-border/55 py-1">
                <span className="whitespace-nowrap text-secondary">{k}</span><span className="whitespace-nowrap tabular-nums text-foreground">{v}</span>
              </div>
            ))}
          </div>
        </section>

        <section data-model-drift>
          <div className="mb-1 text-[11px] font-medium text-secondary">
            {d ? `特征漂移 · PSI · ${md(d.cur.from)} ~ ${md(d.cur.to)} 对 训练截止前一年 · 竖线 = 该特征一年内最高（不低于 ${d.warn.toFixed(2)}）` : '特征漂移'}
          </div>
          {!d ? <div className="rounded-btn border border-dashed border-muted px-3 py-3 text-secondary">{m.asof ? '当天未存档' : '待盘后计算'}</div> : (
            <div>
              {d.rows.map((r) => {
                const line = driftLine(r, d.warn)
                const w = driftWarn(r, d.warn)
                return (
                  <div key={r.key} data-psi={r.key} data-psi-warn={w ? '1' : '0'} className="grid h-6 grid-cols-[4.5rem_minmax(0,1fr)_5.5rem] items-center gap-2">
                    <span className={`whitespace-nowrap ${w ? 'text-warning' : 'text-secondary'}`}>{FEATURE_ZH[r.key] ?? r.key}</span>
                    <span className="relative block h-1.5 rounded-full bg-muted/30">
                      <i className={`block h-full rounded-full ${w ? 'bg-warning' : 'bg-secondary'}`} style={{ width: `${Math.min(100, (r.psi / (line * 1.6)) * 100)}%` }} />
                      <u data-psi-line className="absolute -bottom-1 -top-1 w-0.5 bg-warning no-underline" style={{ left: `${(1 / 1.6) * 100}%` }} />
                    </span>
                    <span className="whitespace-nowrap text-right tabular-nums">
                      <b className={`font-semibold ${w ? 'text-warning' : 'text-foreground'}`}>{r.psi.toFixed(2)}</b>
                      <span data-psi-limit className="text-muted">{` / ${line.toFixed(2)}`}</span>
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </section>

        <section data-model-registry>
          <div className="mb-1 text-[11px] font-medium text-secondary">模型登记</div>
          <table className="w-full">
            <thead><tr className="border-b border-border text-left"><th className={TH}>版本</th><th className={TH}>训练截至</th><th className={TH}>状态</th><th className={`${TH} text-right`}>已结算</th><th className={`${TH} text-right`}>看多 − 自选全体</th></tr></thead>
            <tbody>
              {m.registry.map((r) => (
                <tr key={r.quarter} data-registry={r.quarter} className="border-b border-border/60 last:border-0">
                  <td className={`${TD} whitespace-nowrap font-semibold`}>{r.name}</td>
                  <td className={TD}>{md(r.trained_to)}</td>
                  <td className={TD}><span className={r.role === 'champion' ? badge('status') : r.role === 'rejected' ? badge('warn') : badge('limit')}>{ROLE_ZH[r.role] ?? r.role}</span></td>
                  <td className={`${TD} text-right`}>{`${r.days} 份`}</td>
                  <td className={`${TD} text-right`}>{fmtAtr(r.long_minus_all)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </Sheet>
  )
}
