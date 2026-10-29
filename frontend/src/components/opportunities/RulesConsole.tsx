/** 规则齿轮 (0.29.0): 机会板 3+3 + 单笔风险, 一个保存按钮 (POST /api/opportunities/settings)。
 * 机会板模式下纸面只下机会板卡 (watchlist_v4.permission: swing.json 存在 → tradable={'swing'}),
 * 旧规则集「纸面许可」勾选已不再开新仓, 所以面板里不再给那张表。规则目录 (回测成绩 / 候选) 打开时才取。
 *
 * 0.30.0 (10-20 产品决策「新加的策略没有回放数据」): 右侧数字写清来源, 没有数字时写原因, 不再只给一个 —。
 *  - 内置策略: config/swing_rule_history.json = 离线回放 (机会板同一张卡)。0.30.1 起按当前自选与最新数据重算,
 *    来源 (窗口 / 名单 / 数据截至) 由文件自己的元数据给出, 齿轮不再写死日期与票数; 成交不足 5 笔的标「样本少」。
 *  - 挖掘 / AI 策略 (source=custom|ai): research/swing_candidates.json, 发布后与周度挖掘后自动验收; 失败写原因。
 *  - 「重新回放」只重跑挖掘 / AI 策略: 收盘后立即跑, 交易时段排队到盘后管道之后; 只写回放结果, 不动 3+3 与交易。
 *
 * 0.35.0: 面板底部「开盘放量推送」= 开关 + 门槛档位,
 *  每档写回放频率 (后端 levels 给数); 与 3+3 / 风险同一个保存按钮, 只改推送不建规则集版本; 渠道跟随全局推送设置。 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type OppSettings, type RadarPushSettings, type SwingCandidatesStatus, type WatchlistRuleCatalogItem, type WatchlistRuleHistory, type WatchlistRulesPayload } from '@/lib/api'
import { displayStrategyName } from '@/lib/strategyDisplay'
import { Sheet } from '@/components/opportunities/Sheet'
import { hm, rr } from '@/components/opportunities/format'

const BAN = new Set(['evo', 'phantom_4h', 'phantom_4h_short', 'phantom_4h_pine'])
const MAX = 3
const tone = (n: number | null | undefined) => (n == null ? '' : n > 0 ? 'text-bull' : n < 0 ? 'text-bear' : '')

type Candidate = WatchlistRuleCatalogItem['candidate']
type Row = { id: string; name: string; history: WatchlistRuleHistory | null | undefined; candidate?: Candidate; kind?: string }

const isDiscovered = (kind?: string) => kind === 'custom' || kind === 'ai'

const HIST_SRC_FALLBACK = '机会板同一张卡（内置策略离线回放）'

/** 0.30.1: 来源 = 回放文件自己的元数据 (信号日窗口 / 名单 / 数据截至), 重算后自动跟上, 不写死日期与票数 */
export function histSource(meta: WatchlistRulesPayload['history_meta'] | undefined): string {
  const win = meta?.window
  if (!win || win.length < 2) return HIST_SRC_FALLBACK
  const span = `${win[0].slice(0, 7)}~${win[1].slice(0, 7)}`
  const n = (meta?.universe ?? '').match(/watchlist\s+(\d+)/i)?.[1]
  const who = n ? `，自选 ${n} 只` : ''
  const end = meta?.data_end ? `，数据到 ${meta.data_end}` : ''
  return `离线回放（${span} 信号日${who}${end}，机会板同一张卡）`
}
const CAND_SRC = '按机会板卡回放（2023-01 起，自选，含账户上限）'

/** 挖掘 / AI 验收失败的原因 (后端 error 类名 + detail) 转短句; 全文放悬停 */
export function candidateError(c: NonNullable<Candidate>): string {
  const d = c.detail ?? ''
  if (/不存在的信号/.test(d)) {
    const m = d.match(/\['([^']+)'/)
    return `缺信号${m ? ` ${m[1].replace(/^csg_/, '')}` : ''}（已删）`
  }
  if (c.error === 'EnrichedGenerationUnavailableError' || /parquet scan failed|thrift|snapshot/.test(d)) return '盘中读数失败 · 收盘后重跑'
  if (c.error === 'no_watchlist_data') return '自选无数据'
  return `回放失败（${c.error ?? '未知'}）`
}

export type HistCell = { left: string; right?: string; rightTone?: string; title: string; note?: boolean }

/** 一行右侧: 笔数 · 每笔 R, 再加比随便挑多出的 R。没有数字时给原因 (而不是一个 —) */
export function historyCell(r: Row, src: string = HIST_SRC_FALLBACK): HistCell {
  if (isDiscovered(r.kind)) {
    const c = r.candidate
    if (c?.error) return { left: candidateError(c), title: c.detail || c.error || '', note: true }
    if (c && typeof c.n === 'number') {
      const title = `${CAND_SRC}：笔数 · 每笔平均 R；右边 = 比 4h 门内随便挑多出的平均 R`
      if (c.n === 0) return { left: '0 笔', title, note: true }
      return { left: `${c.n} · ${rr(c.mean_r)}`, right: rr(c.excess), rightTone: tone(c.excess), title }
    }
    return { left: '未回放', title: '发布后与周度挖掘后自动回放；也可点下方「重新回放」', note: true }
  }
  const h = r.history
  if (!h) return { left: '无回放', title: `不在 ${src} 里`, note: true }
  if (h.thin || typeof h.mean_r !== 'number') {
    return { left: `样本少 · ${h.n} 笔`, note: true,
      title: `${src}：命中 ${h.raw ?? '?'} 次，其中 4h 门同向 ${h.signals ?? h.n} 次，回区成交 ${h.n} 笔；不足 5 笔不算 R` }
  }
  return { left: `${h.n} · ${rr(h.mean_r)}`, right: rr(h.excess), rightTone: tone(h.excess),
    title: `${src}：笔数 · 每笔平均 R；右边 = 比 4h 门内随便挑多出的平均 R` }
}

function PickList({ side, rows, picked, onToggle, src }: { side: 'long' | 'short'; rows: Row[]; picked: Set<string>; onToggle: (id: string) => void; src: string }) {
  return (
    <section data-swing-side={side}>
      <div className="mb-1 flex items-baseline justify-between text-xs">
        <span className={`font-medium ${side === 'long' ? 'text-bull' : 'text-bear'}`}>{`${side === 'long' ? '做多' : '做空'} · 最多 ${MAX}`}</span>
        <span className="tabular-nums text-muted">{`${picked.size}/${MAX}`}</span>
      </div>
      <div data-swing-list={side} className="max-h-64 overflow-y-auto rounded-card border border-border px-2">
        {rows.map((r) => {
          const checked = picked.has(r.id)
          const cell = historyCell(r, src)
          const name = displayStrategyName(r.name)
          return (
            <label key={`${side}-${r.id}`} data-swing-pick={r.id} data-side={side}
              className={`flex min-h-[40px] cursor-pointer items-center gap-2 border-b border-border/50 py-2 text-xs ${checked ? 'text-foreground' : 'text-secondary'}`}>
              <input type="checkbox" data-swing-check={side} aria-label={`${r.name} ${side === 'long' ? '做多' : '做空'}`}
                checked={checked} onChange={() => onToggle(r.id)} />
              {/* 两行而不是单行截断: 做空名的区分在末尾 (…翻空 / …下穿), 单行会被截掉 */}
              <span data-swing-name className="line-clamp-2 min-w-0 flex-1 break-words leading-4" title={name}>{name}</span>
              {r.candidate?.passed && (
                <span data-candidate={r.id} className="shrink-0 rounded border border-accent/40 px-1 text-[10px] leading-4 text-accent"
                  title={`系统发现 · 按机会板卡回放赢过“4h 门内随便挑”: 每笔多 ${rr(r.candidate.excess)}（区间 ${r.candidate.excess_ci ? `${rr(r.candidate.excess_ci[0])} ~ ${rr(r.candidate.excess_ci[1])}` : '—'}），${r.candidate.n} 笔`}>候选</span>
              )}
              <span data-swing-hist={r.id} data-note={cell.note ? '' : undefined} title={cell.title}
                className={`shrink-0 tabular-nums text-muted ${cell.note ? 'line-clamp-2 max-w-[9rem] text-right text-[11px] leading-4' : ''}`}>{cell.left}</span>
              {cell.right && <span className={`w-12 shrink-0 text-right tabular-nums ${cell.rightTone ?? ''}`} title={cell.title}>{cell.right}</span>}
            </label>
          )
        })}
      </div>
    </section>
  )
}

/** 「重新回放」: 只给挖掘 / AI 策略。状态 = 上次回放时刻 / 回放中 / 已排队到盘后。 */
function CandidateRerun({ status }: { status: SwingCandidatesStatus | undefined }) {
  const qc = useQueryClient()
  const run = useMutation({
    mutationFn: () => api.runSwingCandidates(),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['opportunities', 'rules'] }) },
  })
  const running = !!status?.running
  const state = running ? '回放中 · 约 1–2 分钟'
    : status?.pending_at ? '已排队 · 美东 17:05 自动跑（北京 05:05 / 冬令时 06:05）'
    : status?.generated_at ? `上次回放 ${status.generated_at.slice(5, 10)} ${hm(status.generated_at)} 北京` : '还没回放过'
  return (
    <div data-candidate-rerun className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-muted">
      <span>挖掘 / AI 策略</span>
      <button type="button" data-candidate-run disabled={running || run.isPending} onClick={() => run.mutate()}
        title="只重跑挖掘 / AI 策略：美东 17:00 后立即跑；交易日 09:00–17:00 排队到 17:05；只写回放结果，不改 3+3、不下单"
        className="rounded-btn border border-border px-2 py-1 text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-40">
        {run.isPending ? '提交中…' : '重新回放'}
      </button>
      <span data-candidate-state>{state}</span>
      {run.isError && <span className="text-danger">{(run.error as Error).message}</span>}
    </div>
  )
}

const pctTxt = (x: number) => `${Math.round(x * 100)}%`

/** 0.35.0 开盘放量推送: 开关 + 门槛 (档位来自后端 levels) */
export function RadarPushEditor({ cur, enabled, minRvol, onEnabled, onMinRvol }: {
  cur: RadarPushSettings | undefined; enabled: boolean; minRvol: number; onEnabled: (v: boolean) => void; onMinRvol: (v: number) => void
}) {
  const levels = cur?.levels ?? []
  return (
    <div data-radar-push className="mt-4 rounded-card border border-border bg-base p-3 text-xs">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold leading-5 text-foreground">开盘放量推送</div>
          <div className="text-[11px] leading-4 text-muted">09:51 ET 出榜后按全局推送设置发送 · 每日每渠道至多一条 · 观察，不下单</div>
        </div>
        <button type="button" role="switch" aria-checked={enabled} aria-label="开盘放量推送" data-radar-push-toggle=""
          onClick={() => onEnabled(!enabled)}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors ${enabled ? 'border-accent/50 bg-accent dark:bg-blue-700' : 'border-border bg-elevated'}`}>
          <span data-radar-push-knob="" className={`inline-block h-5 w-5 rounded-full border border-black/5 bg-white shadow-sm transition-transform ${enabled ? 'translate-x-[20px]' : 'translate-x-0.5'}`} />
        </button>
      </div>
      <div data-radar-push-levels className={`mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-4 ${enabled ? '' : 'opacity-50'}`}>
        {levels.map((l) => {
          const on = l.v === minRvol
          return (
            <button key={l.v} type="button" data-radar-push-level={String(l.v)} aria-pressed={on} disabled={!enabled}
              onClick={() => onMinRvol(l.v)}
              title={`回放 ${cur?.period ?? ''}：约 ${pctTxt(l.days)} 的交易日有推送，日均 ${l.per_day.toFixed(2)} 只；推到的票当天收涨≥5% ${pctTxt(l.up5)}，其余开盘收阳的票 ${pctTxt(cur?.other ?? 0)}`}
              className={`rounded-btn border px-2 py-1.5 text-left tabular-nums ${on ? 'border-accent bg-accent/10 text-accent dark:bg-blue-700 dark:text-white' : 'border-border text-secondary hover:bg-elevated'}`}>
              <div className="whitespace-nowrap text-[13px] font-semibold leading-5">{`相对量 ≥${l.v}x`}</div>
              <div className="whitespace-nowrap text-[11px] leading-4">{`${pctTxt(l.days)} 交易日有推送`}</div>
              <div className="whitespace-nowrap text-[11px] leading-4">{`日均 ${l.per_day.toFixed(2)} 只`}</div>
              <div className="whitespace-nowrap text-[11px] leading-4">{`收涨≥5% ${pctTxt(l.up5)}`}</div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

export function RulesConsole({ settings, onClose }: { settings: OppSettings | null; onClose: () => void }) {
  const qc = useQueryClient()
  const q = useQuery<WatchlistRulesPayload>({
    queryKey: ['opportunities', 'rules'], queryFn: () => api.watchlistRules(),
    // 回放中每 15 秒刷一次, 结果出来就换上数字
    refetchInterval: (query) => (query.state.data?.candidates?.running ? 15_000 : false),
  })
  const [longPick, setLongPick] = useState<string[] | null>(null)
  const [shortPick, setShortPick] = useState<string[] | null>(null)
  const [risk, setRisk] = useState<string | null>(null)
  const [pushOn, setPushOn] = useState<boolean | null>(null)
  const [pushTh, setPushTh] = useState<number | null>(null)
  const pushCur = settings?.radar_push
  const pushEnabled = pushOn ?? pushCur?.enabled ?? true
  const pushMin = pushTh ?? pushCur?.min_rvol ?? 3
  const pushDirty = (pushOn !== null && pushOn !== pushCur?.enabled) || (pushTh !== null && pushTh !== pushCur?.min_rvol)
  const seedLong = (settings?.long ?? []).filter((id) => !BAN.has(id)).slice(0, MAX)
  const seedShort = (settings?.short ?? []).filter((id) => !BAN.has(id)).slice(0, MAX)
  const longIds = longPick ?? seedLong
  const shortIds = shortPick ?? seedShort
  const longSet = useMemo(() => new Set(longIds), [longIds])
  const shortSet = useMemo(() => new Set(shortIds), [shortIds])
  const riskVal = risk ?? String(settings?.risk_per_trade_pct ?? '')

  const toggle = (side: 'long' | 'short', id: string) => {
    if (BAN.has(id)) return
    const cur = side === 'long' ? [...longIds] : [...shortIds]
    const i = cur.indexOf(id)
    if (i >= 0) cur.splice(i, 1)
    else {
      if (cur.length >= MAX) cur.shift()     // 0.23.0: 已满 3 条时点新策略 = 替换最早选的一条
      cur.push(id)
    }
    if (side === 'long') setLongPick(cur)
    else setShortPick(cur)
  }

  const riskNum = risk === null ? null : Number(risk)
  const riskBad = riskNum !== null && !(Number.isFinite(riskNum) && riskNum >= 0.1 && riskNum <= 2)
  const save = useMutation({
    mutationFn: () => api.saveOppSettings(longIds, shortIds, riskNum, pushDirty ? { enabled: pushEnabled, min_rvol: pushMin } : null),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['opportunities'] })
      setLongPick(null); setShortPick(null); setRisk(null); setPushOn(null); setPushTh(null)
    },
  })
  const dirty = longPick !== null || shortPick !== null || risk !== null || !!settings?.evo || pushDirty

  const d = q.data
  const catalog = (d?.catalog ?? []).filter((r) => !BAN.has(r.id))
  const sortKey = (r: Row) => (isDiscovered(r.kind) ? r.candidate?.excess : r.history?.excess) ?? -9
  const byExcess = (a: Row, b: Row) => sortKey(b) - sortKey(a) || a.name.localeCompare(b.name)
  const nameOf = (id: string) => catalog.find((r) => r.id === id)?.name ?? (d?.short_rules ?? []).find((r) => r.id === id)?.name ?? id
  // 已选但不在目录里的 id (例如只存在于 swing.json) 仍显示, 避免勾选“消失”
  const sticky = (ids: string[], known: Row[]): Row[] => ids.filter((id) => !known.some((r) => r.id === id)).map((id) => ({ id, name: nameOf(id), history: null }))
  const longRows: Row[] = catalog.map((r) => ({ id: r.id, name: r.name, history: r.history, candidate: r.candidate, kind: r.kind })).sort(byExcess)
  const shortBase: Row[] = [
    // 做空列表用空头卡的名字 (吊灯翻空 / 挤压动能转弱 …), 不用 META 里的做多名 (吊灯止损翻多 / 挤压释放做多)
    ...catalog.filter((r) => r.short_capable).map((r) => ({ id: r.id, name: r.name_short || r.name, history: r.history_short ?? r.history, kind: r.kind })),
    ...(d?.short_rules ?? []).filter((r) => !BAN.has(r.id) && !catalog.some((c) => c.id === r.id)).map((r) => ({ id: r.id, name: r.name, history: r.history })),
  ].sort(byExcess)
  const hasDiscovered = catalog.some((r) => isDiscovered(r.kind))
  const src = histSource(d?.history_meta)
  const err = q.isError ? (q.error as Error).message : null

  return (
    <Sheet attr={{ 'data-console': '' }} onClose={onClose} title="出卡策略"
      footer={(
        <>
          <button type="button" data-save disabled={!dirty || riskBad || save.isPending || !settings}
            onClick={() => save.mutate()}
            className="rounded-btn border border-accent/60 px-3 py-1.5 text-accent hover:bg-accent/10 disabled:opacity-40"
          >{save.isPending ? '保存中…' : '保存'}</button>
          {riskBad && <span data-risk-error className="text-danger">单笔风险 0.1–2</span>}
          {save.isError && <span className="text-danger">{(save.error as Error).message}</span>}
        </>
      )}>
      <div data-swing-explain className="mb-3 text-xs text-muted">{`${settings?.gate?.name ?? '方向门'}定方向 · 最多 3 条策略打分 · 纸面只下机会板卡`}</div>
      <div data-forward-summary className="mb-3 flex flex-wrap items-end gap-3 rounded-card border border-border bg-base p-3 text-xs">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] leading-4 text-muted">当前 3+3</div>
          <div className="text-sm tabular-nums leading-5">{`${longIds.length} 多 · ${shortIds.length} 空`}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[11px] leading-4 text-muted">单笔风险 %（纸面）</div>
          <input data-risk aria-label="单笔风险" inputMode="decimal"
            className="mt-0.5 w-20 rounded-input border border-border bg-surface px-2 py-0.5 text-sm tabular-nums text-foreground"
            value={riskVal} onChange={(e) => setRisk(e.target.value)} />
        </div>
      </div>
      {q.isLoading ? <div className="text-sm text-muted">…</div>
        : err ? <div className="text-sm text-danger">{err}</div>
        : (
          <>
            <div data-swing-editor className="grid gap-4 sm:grid-cols-2">
              <PickList side="long" rows={[...sticky(longIds, longRows), ...longRows]} picked={longSet} onToggle={(id) => toggle('long', id)} src={src} />
              <PickList side="short" rows={[...sticky(shortIds, shortBase), ...shortBase]} picked={shortSet} onToggle={(id) => toggle('short', id)} src={src} />
            </div>
            <div data-hist-legend className="mt-2 text-[11px] text-muted">右侧 = 机会板卡回放：笔数 · 每笔 R · 比随便挑多的 R（悬停看来源）</div>
            {hasDiscovered && <CandidateRerun status={d?.candidates} />}
          </>
        )}
      {pushCur && <RadarPushEditor cur={pushCur} enabled={pushEnabled} minRvol={pushMin} onEnabled={setPushOn} onMinRvol={setPushTh} />}
    </Sheet>
  )
}
