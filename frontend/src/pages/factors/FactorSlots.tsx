/** 因子页 C 方案 (0.54.0): 按用途看因子 —— 每个槽位一把尺子, 在用 vs 挑战者同尺对比。
 *
 * 只读现有接口, 不新增后端:
 *  - 槽位 1 机会板 · 做多出卡: 齿轮现值 (oppPage.settings.long) + 规则目录 (/api/opportunities/rules) 的卡回放成绩。
 *    尺子 = 比「4h 门内随便挑」每笔多出的 R; 内置策略读离线回放 history, 挖掘 / AI / 排序因子读 candidate (带 95% 区间)。
 *  - 槽位 2 选股策略: 周度发现 (/api/factor-lab/page) 的现行策略组合 vs 本周候选, 尺子 = 选股回测 (每日前 20, 自选)。
 *  - 槽位 3 策略打分: /api/strategies 的 scoring 权重 vs 近 4 次周度挖掘 (/api/backtest/mining/runs/{id}/result) 的 factors[]。
 *    注意: 周度结果里的 ic_mean 是该次挖掘最后一个外层折的训练窗 (W44 = 2021-07 → 2023-07) 主题池 5 日 Rank IC, 不是最近几周的实现 IC;
 *    所以页面写「训练窗 IC」, 4 次之间几乎不变, 只画一个数。逐周变化看「周度入选」(该次挖掘是否选中它)。
 *    IC 按打分方向折算 (direction=low 取反, descending=false 再取反), 才能看出「打分偏好大值, 训练窗里却是小值更好」。
 *  - 依赖矩阵: 打分因子 × 策略族 (按策略 tags 归类) + 3+3 引用。
 * 写操作只沿用已有接口: 周度候选保存 / 放弃 (factorLabDecide)。改 3+3 去机会页齿轮, 检验去检验页。 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Settings2 } from 'lucide-react'
import { toast } from '@/components/Toast'
import { OPP_PAGE_KEY } from '@/components/opportunities/useOppPage'
import {
  api,
  type DiscoverCandidate,
  type DiscoverWeek,
  type FactorLabPage,
  type FactorLibraryItem, type FactorMaturityMeta,
  type MiningResult,
  type MiningRun,
  type OppSettings,
  type StrategyDetail,
  type WatchlistRuleCatalogItem,
} from '@/lib/api'
import { weeklyText } from './weeklySchedule'
import { navTicks } from './navTicks'
import { maturityTitle } from './FactorLibrary'
import { QK } from '@/lib/queryKeys'
import { RETIRED_MARKET_FIELDS } from '@/lib/retired-market-fields'
import { RulesConsole } from '@/components/opportunities/RulesConsole'
import { displayStrategyName } from '@/lib/strategyDisplay'
import { RegimeBar } from '@/components/opportunities/RegimeBar'
import { CiAxis, CiBar } from '@/components/opportunities/CiBar'
import { FillGrid, GRID } from '@/components/opportunities/Cards'
import { ciScale, isObserve, ledgerOf, regimeOf, TIER_ICON, tierReason, type ReasonPart, type TierReason } from '@/components/opportunities/p1'
import type { SwingBoardCard } from '@/lib/api'

const DASH = '—'
const MINUS = '−'
const WEEKS_SHOWN = 4
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
/** 符号按四舍五入后的数字给: 舍入成 0 的不带正负号 (不出现 −0.00) */
const signed = (v: number, digits: number) => { const a = Math.abs(v).toFixed(digits); return `${Number(a) === 0 ? '' : v > 0 ? '+' : MINUS}${a}` }
export const fmtR = (v: number | null | undefined) => (fin(v) ? `${signed(v, 2)}R` : DASH)
export const fmtIc = (v: number | null | undefined) => (fin(v) ? signed(v, 3) : DASH)
export const fmtPct = (v: number | null | undefined) => (fin(v) ? `${signed(v * 100, 1)}%` : DASH)
const rTone = (v: number | null | undefined) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')
/** R 值 (两位小数) 的取色: 舍入成 0.00 的按平处理, 不染涨跌色 */
const rToneR = (v: number | null | undefined) => rTone(fin(v) && Math.abs(v) < 0.005 ? 0 : v)

// ---------------------------------------------------------------- model (纯函数, 单测守着)
export type Fam = '突破' | '趋势' | '回踩' | '反转'
export const FAMS: Fam[] = ['突破', '趋势', '回踩', '反转']

/** 策略族: 按 META.tags 关键词归类, 顺序优先 (突破 > 回踩 > 反转 > 其余算趋势) */
export function strategyFamily(tags: string[] | undefined): Fam {
  const t = (tags ?? []).join(' ')
  if (/突破/.test(t)) return '突破'
  if (/回踩|回调|低吸/.test(t)) return '回踩'
  if (/反转|超跌|反弹|均值回归/.test(t)) return '反转'
  return '趋势'
}

export type WeeklyRun = { week: string; result: MiningResult | undefined }

/** 周度 IC 表: factor → 每周 IC (旧 → 新, 缺 = null) + 标签 */
export type IcRow = { label: string; ics: (number | null)[]; sel: (boolean | null)[] }
export function weeklyIcTable(runs: WeeklyRun[]): Map<string, IcRow> {
  const out = new Map<string, IcRow>()
  runs.forEach((run, i) => {
    for (const f of run.result?.factors ?? []) {
      const row = out.get(f.factor_name) ?? { label: f.label || f.factor_name, ics: runs.map(() => null), sel: runs.map(() => null) }
      row.ics[i] = fin(f.ic_mean) ? f.ic_mean : null
      row.sel[i] = !!f.selected
      out.set(f.factor_name, row)
    }
  })
  return out
}

const RAW_LABEL: Record<string, string> = { turnover_rate: '换手率', amount: '成交额' }

export type ScoreFlag = '缺数据' | '无检验' | '方向不一' | '方向相反'
export interface ScoreUse {
  id: string; label: string; n: number; share: number; sign: 1 | -1 | 0
  uses: { id: string; name: string; fam: Fam; w: number; inPlay: boolean }[]
  inLib: boolean; ics: (number | null)[]; dirIcs: (number | null)[]; dirMean: number | null; flag: ScoreFlag | null
  sel: (boolean | null)[]
}
/** 反向 = 每周折算 IC 都 < 0 且均值绝对值 ≥ 0.005 (更小的视为接近 0, 不下结论) */
export const REVERSE_MIN = 0.005

export function scoringUses(strategies: StrategyDetail[], lib: Map<string, FactorLibraryItem>,
  ic: Map<string, IcRow>, weeks: number, inPlay: Set<string>): { uses: ScoreUse[]; nStrat: number } {
  const scored = strategies.filter(s => Object.values(s.scoring ?? {}).some(w => fin(w) && w > 0))
  const acc = new Map<string, { share: number; signs: Set<number>; uses: ScoreUse['uses'] }>()
  for (const s of scored) {
    const entries = Object.entries(s.scoring).filter(([, w]) => fin(w) && w > 0)
    const total = entries.reduce((a, [, w]) => a + w, 0)
    const flip = s.descending === false ? -1 : 1
    const fam = strategyFamily(s.tags)
    for (const [fid, w] of entries) {
      const sign = (s.scoring_directions?.[fid] === 'low' ? -1 : 1) * flip
      const a = acc.get(fid) ?? { share: 0, signs: new Set<number>(), uses: [] }
      a.share += w / total
      a.signs.add(sign)
      a.uses.push({ id: s.id, name: displayStrategyName(s.name), fam, w: w / total, inPlay: inPlay.has(s.id) })
      acc.set(fid, a)
    }
  }
  const n = scored.length || 1
  const uses: ScoreUse[] = [...acc.entries()].map(([id, a]) => {
    const sign: 1 | -1 | 0 = a.signs.size === 1 ? (a.signs.has(-1) ? -1 : 1) : 0
    const row = ic.get(id)
    const ics = row?.ics ?? Array.from({ length: weeks }, () => null)
    const dirIcs = ics.map(v => (v == null || sign === 0 ? null : v * sign))
    const vals = dirIcs.filter(fin)
    const dirMean = vals.length ? vals.reduce((x, y) => x + y, 0) / vals.length : null
    const inLib = lib.has(id)
    let flag: ScoreFlag | null = null
    if (RETIRED_MARKET_FIELDS.has(id)) flag = '缺数据'
    else if (!inLib && !ics.some(fin)) flag = '无检验'
    else if (sign === 0) flag = '方向不一'
    else if (vals.length === dirIcs.length && vals.length > 0 && vals.every(v => v < 0) && dirMean != null && Math.abs(dirMean) >= REVERSE_MIN) flag = '方向相反'
    return { id, label: lib.get(id)?.label ?? row?.label ?? RAW_LABEL[id] ?? id, n: a.uses.length, share: a.share / n, sign, uses: a.uses, inLib, ics, dirIcs, dirMean, flag,
      sel: row?.sel ?? Array.from({ length: weeks }, () => null) }
  })
  uses.sort((x, y) => y.share - x.share || y.n - x.n || x.label.localeCompare(y.label))
  return { uses, nStrat: scored.length }
}

export interface IcChallenger { id: string; label: string; dir: '高' | '低' | null; dirMean: number | null; dirIcs: (number | null)[]; sel: (boolean | null)[]; tag: 'pending' | 'watch' | 'pool'; note?: string }
/** 打分挑战者: 待审采集公式 + 已入库观察的自定义因子 + 周度里方向稳定、|IC| 最大的未用内置因子 */
export function scoringChallengers(uses: ScoreUse[], lib: Map<string, FactorLibraryItem>,
  ic: Map<string, IcRow>, page: FactorLabPage | undefined, weeks: number, topN = 4): IcChallenger[] {
  const used = new Set(uses.map(u => u.id))
  const out: IcChallenger[] = []
  for (const card of page?.intake?.cards ?? []) {
    if (card.stage !== 'pending') continue
    for (const f of card.formulas) {
      if (f.merged_id || f.screen?.duplicate) continue
      const v = f.screen?.ic
      const sign = f.direction === 'low' ? -1 : 1
      out.push({ id: `intake:${card.id}:${f.name}`, label: f.name, dir: f.direction === 'low' ? '低' : '高', dirMean: fin(v) ? v * sign : null,
        dirIcs: [], sel: [], tag: 'pending', note: `采集 · ${card.source_name}` })
    }
  }
  for (const item of lib.values()) {
    if (used.has(item.id) || item.origin == null || item.origin === 'builtin') continue
    if (item.status !== 'watch' && item.status !== 'active') continue
    const row = ic.get(item.id)
    const vals = (row?.ics ?? []).filter(fin)
    const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null
    out.push({ id: item.id, label: item.label, dir: mean == null ? null : mean >= 0 ? '高' : '低', dirMean: mean == null ? null : Math.abs(mean),
      dirIcs: (row?.ics ?? []).map(v => (v == null || mean == null ? null : v * (mean >= 0 ? 1 : -1))), sel: row?.sel ?? [], tag: 'watch', note: vals.length ? undefined : '下次周度起参评' })
  }
  const pool = [...ic.entries()]
    .filter(([id, r]) => !used.has(id) && lib.get(id)?.available !== false && lib.get(id)?.origin !== 'intake' && lib.get(id)?.origin !== 'custom'
      && r.ics.length === weeks && r.ics.every(fin))
    .map(([id, r]) => {
      const vals = r.ics as number[]
      const mean = vals.reduce((a, b) => a + b, 0) / vals.length
      const steady = vals.every(v => Math.sign(v) === Math.sign(mean))
      return { id, label: lib.get(id)?.label ?? r.label, mean, steady, vals, sel: r.sel }
    })
    .filter(x => x.steady)
    .sort((a, b) => Math.abs(b.mean) - Math.abs(a.mean))
    .slice(0, topN)
  for (const x of pool) {
    const s = x.mean >= 0 ? 1 : -1
    out.push({ id: x.id, label: x.label, dir: s > 0 ? '高' : '低', dirMean: Math.abs(x.mean), dirIcs: x.vals.map(v => v * s), sel: x.sel, tag: 'pool' })
  }
  return out
}

/** 名称里夹带的因子 id (如「因子组合 · annual_vol_20d」) 换成因子库中文名 */
export function humanizeName(name: string, lib: Map<string, FactorLibraryItem>): string {
  return name.replace(/\b[a-z][a-z0-9_]*_[a-z0-9_]+\b/g, id => lib.get(id)?.label ?? id)
}

export interface BoardRow {
  id: string; name: string; role: 'champ' | 'ch'; kind: string
  n: number | null; excess: number | null; lo: number | null; hi: number | null; p: number | null; passed: boolean; note: string | null
}
export { navTicks }
const KIND_LABEL: Record<string, string> = { ranker: '排序因子', custom: '挖掘', ai: 'AI', builtin: '内置', ported: '移植' }
export const boardKind = (k: string) => KIND_LABEL[k] ?? k

/** 槽位 1: 在用 = 齿轮现值做多 3 条 (离线回放 history); 挑战 = 挖掘 / AI / 排序因子 (candidate, 带区间) */
export function boardRows(longIds: string[], catalog: WatchlistRuleCatalogItem[]): BoardRow[] {
  const byId = new Map(catalog.map(c => [c.id, c]))
  const rows: BoardRow[] = []
  for (const id of longIds) {
    const c = byId.get(id)
    const h = c?.history
    const d = c?.candidate
    // 入选的排序因子 / 挖掘 / AI 策略没有离线回放 history, 成绩在 candidate (与挑战者同一把尺子): 不能写成「无回放 · 0 笔」
    if (!h && c && ['ranker', 'custom', 'ai'].includes(c.kind) && d && !d.error && fin(d.n)) {
      const enough = d.n >= 30
      rows.push({ id, name: displayStrategyName(c.name), role: 'champ', kind: c.kind, n: d.n,
        excess: enough ? (d.excess ?? null) : null, lo: enough ? (d.excess_ci?.[0] ?? null) : null, hi: enough ? (d.excess_ci?.[1] ?? null) : null,
        p: null, passed: false, note: d.n === 0 ? '0 笔' : !enough ? `样本少 ${d.n} 笔` : null })
      continue
    }
    const thin = !h || h.thin || !fin(h.mean_r)
    rows.push({ id, name: displayStrategyName(c?.name ?? id), role: 'champ', kind: c?.kind ?? 'builtin',
      n: h?.n ?? null, excess: thin ? null : (h?.excess ?? null), lo: null, hi: null, p: fin(h?.p) ? h!.p! : null, passed: false,
      note: !h ? '无回放' : thin ? `样本少 ${h.n} 笔` : null })
  }
  for (const c of catalog) {
    if (longIds.includes(c.id) || !['ranker', 'custom', 'ai'].includes(c.kind)) continue
    const d = c.candidate
    const n = fin(d?.n) ? d!.n! : null
    const enough = n != null && n >= 30
    rows.push({ id: c.id, name: displayStrategyName(c.name), role: 'ch', kind: c.kind, n,
      excess: enough ? (d?.excess ?? null) : null, lo: enough ? (d?.excess_ci?.[0] ?? null) : null, hi: enough ? (d?.excess_ci?.[1] ?? null) : null,
      p: null, passed: !!d?.passed,
      note: d?.error ? '回放失败' : n == null ? '未回放' : n === 0 ? '0 笔' : !enough ? `样本少 ${n} 笔` : null })
  }
  // 挑战者: 有成绩的按区间下限排, 样本少的放后面
  const champs = rows.filter(r => r.role === 'champ')
  const chs = rows.filter(r => r.role === 'ch').sort((a, b) => (b.excess != null ? 1 : 0) - (a.excess != null ? 1 : 0) || (b.lo ?? -9) - (a.lo ?? -9) || (b.n ?? 0) - (a.n ?? 0))
  return [...champs, ...chs]
}

/** 挑战者理由 (同一把尺子 = 比 4h 门内随便挑每笔多出的 R; 门槛与验收一致: n ≥ 30 且区间下限 > 0) */
export function challengerReason(r: Pick<BoardRow, 'n' | 'excess' | 'lo' | 'hi' | 'passed' | 'note'>): TierReason {
  const parts: ReasonPart[] = []
  if (r.note) parts.push({ text: r.note, hot: true })
  else if (r.lo != null && r.hi != null) {
    if (r.lo > 0) parts.push({ text: `区间下限 ${fmtR(r.lo)} > 0` })
    else if (r.hi < 0) parts.push({ text: `区间上限 ${fmtR(r.hi)} < 0`, hot: true })
    else parts.push({ text: `区间 ${fmtR(r.lo)} ~ ${fmtR(r.hi)} 跨 0`, hot: true })
    parts.push({ text: `样本 ${r.n ?? 0}` })
  } else parts.push({ text: `超额 ${fmtR(r.excess)}` }, { text: `样本 ${r.n ?? 0}` })
  if (r.passed) parts.push({ text: '已过验收' })
  return { word: '挑战', off: false, parts }
}

/** 一行一句理由。换下行: 浅色底 + 加粗正文 + 触发条件高亮片段 (2026-11-26 用户指定细节) */
const WORD_CLS: Record<string, string> = {
  留用: 'bg-accent/10 text-accent dark:text-blue-300', 复活: 'bg-accent/10 text-accent dark:text-blue-300', 观察: 'bg-elevated text-foreground',
  挑战: 'bg-elevated text-secondary', 换下: 'bg-warning text-white dark:bg-amber-600', 待定: 'bg-elevated text-secondary',
}
export function ReasonLine({ r }: { r: TierReason }) {
  return (
    <span data-reason={r.word} className={`flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded px-1.5 py-0.5 text-[12px] ${r.off ? 'bg-warning/10 font-semibold text-foreground ring-1 ring-inset ring-warning/40' : 'text-secondary'}`}>
      <span className={`inline-flex h-5 shrink-0 items-center rounded px-1.5 text-[11px] font-bold ${r.off ? WORD_CLS['换下'] : WORD_CLS[r.word] ?? WORD_CLS['挑战']}`}>{r.off && r.word === '待定' ? '待定·不出卡' : r.word}</span>
      {r.parts.map((p, i) => (
        <span key={i} className="inline-flex items-center gap-1.5 whitespace-nowrap">
          {i > 0 && <span aria-hidden="true" className="text-secondary">·</span>}
          {p.hot ? <mark data-reason-hot="" className={`rounded px-1 tabular-nums text-foreground underline decoration-warning decoration-2 underline-offset-[3px] ${r.off ? 'bg-warning/30 font-extrabold' : 'bg-warning/15 font-semibold'}`}>{p.text}</mark> : <span className="tabular-nums">{p.text}</span>}
        </span>
      ))}
    </span>
  )
}

/** 百分比取整 (最大余数法): 各项合计正好 100, 避免 21+21+20… = 99 */
export function roundShares(shares: number[]): number[] {
  const total = shares.reduce((a, b) => a + b, 0)
  if (total <= 0) return shares.map(() => 0)
  const raw = shares.map(v => (v / total) * 100)
  const out = raw.map(Math.floor)
  let left = 100 - out.reduce((a, b) => a + b, 0)
  const order = raw.map((v, i) => [v - Math.floor(v), i] as const).sort((a, b) => b[0] - a[0])
  for (const [, i] of order) { if (left <= 0) break; out[i] += 1; left -= 1 }
  return out
}

export function pendingCount(page: FactorLabPage | undefined): number {
  if (!page) return 0
  const w = page.week
  const cands = w ? w.candidates.filter(c => !c.error && !page.decisions[`${w.week}|${c.signature}`]).length : 0
  return cands + (page.intake?.funnel.pending ?? 0)
}

// ---------------------------------------------------------------- data hook
function useSlotData() {
  const lab = useQuery({ queryKey: QK.factorLabPage, queryFn: () => api.factorLabPage(), staleTime: 60_000 })
  const rules = useQuery({ queryKey: ['opportunities', 'rules'], queryFn: () => api.watchlistRules(), staleTime: 60_000 })
  const opp = useQuery({ queryKey: OPP_PAGE_KEY, queryFn: () => api.oppPage(), staleTime: 60_000 })
  const strategies = useQuery({ queryKey: [...QK.screenerStrategies('all'), 'all'], queryFn: () => api.screenerStrategies(undefined, 'all'), staleTime: 300_000 })
  const lib = useQuery({ queryKey: QK.factorLibrary('all'), queryFn: () => api.factorLibrary(), staleTime: 300_000 })
  const runs = useQuery({ queryKey: QK.miningRuns, queryFn: () => api.miningRuns(), staleTime: 300_000 })
  const weekly: MiningRun[] = useMemo(() => (runs.data?.items ?? [])
    .filter(r => r.source === 'scheduled' && String(r.status).startsWith('succeeded'))
    .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, WEEKS_SHOWN).reverse(), [runs.data])
  const results = useQueries({ queries: weekly.map(r => ({ queryKey: QK.miningResult(r.run_id), queryFn: () => api.miningResult(r.run_id), staleTime: Infinity })) })
  const weeklyRuns: WeeklyRun[] = weekly.map((r, i) => ({ week: r.run_id.replace(/^weekly-\d{4}-/, ''), result: results[i]?.data }))
  const icLoading = runs.isLoading || results.some(q => q.isLoading)
  const libMap = useMemo(() => new Map((lib.data?.factors ?? []).map(f => [f.id, f])), [lib.data])
  const icMap = useMemo(() => weeklyIcTable(weeklyRuns), [weeklyRuns.map(w => w.result?.run_id ?? '').join('|')]) // eslint-disable-line react-hooks/exhaustive-deps
  const settings: OppSettings | null = opp.data?.settings ?? null
  const inPlay = useMemo(() => new Set([...(settings?.long ?? []), ...(settings?.short ?? [])]), [settings])
  const scoring = useMemo(() => scoringUses(strategies.data?.presets ?? [], libMap, icMap, weeklyRuns.length, inPlay),
    [strategies.data, libMap, icMap, weeklyRuns.length, inPlay])
  return { lab, rules, opp, strategies, lib, runs, weeklyRuns, icLoading, libMap, icMap, settings, scoring }
}
type SlotData = ReturnType<typeof useSlotData>

// ---------------------------------------------------------------- small pieces
function Role({ champ }: { champ: boolean }) {
  return champ
    ? <span className="inline-flex h-5 shrink-0 items-center rounded px-1.5 text-[11px] font-bold text-violet-800 ring-1 ring-inset ring-violet-500/40 bg-violet-500/15 dark:text-violet-200">在用</span>
    : <span className="inline-flex h-5 shrink-0 items-center rounded px-1.5 text-[11px] font-bold text-secondary ring-1 ring-inset ring-muted">挑战</span>
}
function Tag({ children, tone = 'mute' }: { children: ReactNode; tone?: 'mute' | 'warn' | 'run' | 'ok' }) {
  const cls = tone === 'warn' ? 'bg-warning/15 text-foreground' : tone === 'run' ? 'bg-accent/15 text-foreground' : tone === 'ok' ? 'bg-elevated text-foreground' : 'bg-elevated text-secondary'
  const dot = tone === 'warn' ? 'bg-warning' : tone === 'run' ? 'bg-accent' : 'bg-current'
  return <span className={`inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 text-[11px] font-semibold ${cls}`}><span className={`h-1.5 w-1.5 rounded-full ${dot}`} />{children}</span>
}
function SlotCard({ no, title, sub, ruler, children, footer, attr, className = '' }: {
  no: number; title: string; sub: ReactNode; ruler: string; children: ReactNode; footer: ReactNode; attr: Record<string, string>; className?: string
}) {
  return (
    <section {...attr} className={`flex min-w-0 flex-col overflow-hidden rounded-card border border-border bg-surface ${className}`}>
      <header className="flex min-w-0 items-center gap-2.5 border-b border-border px-4 py-2.5">
        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-elevated text-[12px] font-bold text-foreground">{no}</span>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-foreground">{title}</div>
          <div className="text-[11px] text-secondary md:truncate">{sub}</div>
        </div>
        <span data-ruler="" className="ml-auto shrink-0 whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-medium text-foreground ring-1 ring-inset ring-border">口径 · {ruler}</span>
      </header>
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      <footer data-slot-footer="" className="mt-auto flex min-h-[44px] flex-wrap items-center gap-2 border-t border-border px-4 py-2 text-xs">{footer}</footer>
    </section>
  )
}
function SlotError({ text }: { text: string }) {
  return <div className="flex flex-1 items-center justify-center p-6 text-xs text-muted">{text}</div>
}

const BAR_W = 176

/** 周度入选: 每次周度挖掘一个点, 实心 = 该次被选中 */
function SelDots({ sel, weeks = [] }: { sel: (boolean | null)[]; weeks?: string[] }) {
  if (!sel.length || sel.every(v => v == null)) return <span className="text-[11px] text-muted">{DASH}</span>
  const k = sel.filter(Boolean).length
  return (
    <span data-sel-dots="" className="inline-flex items-center gap-2" title={sel.map((v, i) => `${weeks[i] ?? i + 1} ${v ? '入选' : v === false ? '落选' : '无'}`).join(' · ')}>
      <span className="inline-flex gap-1">{sel.map((v, i) => <i key={i} className={`h-2.5 w-2.5 rounded-full ${v ? 'bg-foreground/80' : v === false ? 'ring-[1.5px] ring-inset ring-secondary/70' : 'bg-transparent'}`} />)}</span>
      <span className="text-[12px] tabular-nums text-secondary">{`${k}/${sel.length}`}</span>
    </span>
  )
}

/** IC 横条: 0 居中, 正 = 灰, 负 = 琥珀, 全表同一刻度 */
export const IC_GATE = 0.02
function IcBar({ v, scale }: { v: number | null; scale: number }) {
  const W = 150, H = 14, mid = W / 2
  const px = (t: number) => mid + (Math.max(-scale, Math.min(scale, t)) / scale) * (mid - 3)
  const gates = <>{[-IC_GATE, IC_GATE].map(g => <line key={g} x1={px(g)} x2={px(g)} y1={0} y2={H} stroke="hsl(var(--border))" strokeDasharray="2 2" />)}</>
  if (v == null) return <svg width={W} height={H} aria-hidden="true">{gates}<line x1={mid} x2={mid} y1={0} y2={H} stroke="hsl(var(--fg-muted))" /></svg>
  const len = Math.max(3, Math.abs(px(v) - mid))
  const pass = Math.abs(v) >= IC_GATE
  return (
    <svg width={W} height={H} aria-hidden="true" data-ic-bar="">
      {gates}
      <line x1={mid} x2={mid} y1={0} y2={H} stroke="hsl(var(--fg-muted))" />
      <rect x={v >= 0 ? mid : mid - len} y={2} width={len} height={H - 4} rx={2}
        fill={v < 0 ? 'hsl(var(--warning))' : pass ? 'hsl(var(--fg-primary) / 0.85)' : 'hsl(var(--fg-secondary) / 0.6)'} />
    </svg>
  )
}
function IcAxis({ scale }: { scale: number }) {
  const W = 150, mid = W / 2
  const px = (t: number) => mid + (t / scale) * (mid - 3)
  return (
    <svg width={W} height={12} aria-hidden="true">
      {[[-IC_GATE, `${MINUS}${IC_GATE}`], [0, '0'], [IC_GATE, `+${IC_GATE}`]].map(([t, lab]) => (
        <text key={String(t)} x={px(t as number)} y={10} fontSize="10" textAnchor="middle" fill="hsl(var(--fg-secondary))">{lab as string}</text>
      ))}
    </svg>
  )
}

/** 净值线: svg 绝对定位铺满容器, 不参与容器自然高度 (同高拉伸时不会反向撑高) */
function NavLines({ dates, series }: { dates: string[]; series: { key: string; values: (number | null)[]; color: string; width: number; dash?: string }[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 520, h: 180 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => { if (el.clientWidth > 0 && el.clientHeight > 0) setSize({ w: el.clientWidth, h: el.clientHeight }) }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const { w: W, h: H } = size
  const L = 34, R = 44, T = 14, B = 22
  const n = dates.length
  const all = series.flatMap(s => s.values.filter(fin))
  const mx = all.length ? Math.max(...all) * 1.08 : 2
  const mn = all.length ? Math.min(0.9, Math.min(...all)) : 0.9
  // 时间比例尺: 曲线点是不等距采样 (月末 / 最后一天), 按日期而不是下标排横坐标
  const ts = dates.map(d => Date.parse(d))
  const t0 = ts[0] ?? 0, t1 = ts[n - 1] ?? 1
  const xt = (t: number) => L + (W - L - R) * (t - t0) / Math.max(t1 - t0, 1)
  const x = (i: number) => xt(ts[i] ?? t0)
  const y = (v: number) => T + (H - T - B) * (1 - (v - mn) / (mx - mn || 1))
  const path = (vals: (number | null)[]) => vals.map((v, i) => (v == null ? '' : `${i && vals[i - 1] != null ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join('')
  const ticks = navTicks(mn, mx)
  const years = Array.from(new Set(dates.map(d => d.slice(0, 4)))).slice(1)
  const ends = series.map(s => { const last = [...s.values].reverse().find(fin); return { y: last == null ? 0 : y(last), v: last, color: s.color } })
    .filter(e => e.v != null).sort((a, b) => a.y - b.y)
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14
  return (
    <div ref={ref} className="relative min-h-[170px] flex-1" data-nav-lines="">
      {n >= 2 && all.length > 0 && (
        <svg className="absolute inset-0" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
          {ticks.map(t => (
            <g key={t}>
              <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="hsl(var(--border))" strokeDasharray={t === 1 ? '3 3' : undefined} />
              <text x={L - 6} y={y(t) + 3.5} fontSize="10.5" textAnchor="end" fill="hsl(var(--fg-secondary))">{t.toFixed(1)}</text>
            </g>
          ))}
          {[...series].reverse().map(s => <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={s.width} strokeDasharray={s.dash} strokeLinejoin="round" />)}
          {ends.map((e, i) => <text key={i} x={W - R + 6} y={e.y + 4} fontSize="11" fontWeight="600" fill={e.color}>{(e.v as number).toFixed(2)}</text>)}
          <text x={L} y={H - 6} fontSize="10.5" fill="hsl(var(--fg-secondary))">{dates[0].slice(0, 7)}</text>
          {years.map(yy => { const xx = xt(Date.parse(`${yy}-01-01`)); return (xx - L > 44 && W - R - xx > 52) && <text key={yy} x={xx} y={H - 6} fontSize="10.5" textAnchor="middle" fill="hsl(var(--fg-secondary))">{`${yy}-01`}</text> })}
          <text x={W - R} y={H - 6} fontSize="10.5" textAnchor="end" fill="hsl(var(--fg-secondary))">{dates[n - 1].slice(0, 7)}</text>
        </svg>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- slot 1 · 机会板做多
/** 闭环: 槽位里每条在用规则 → 它引用的因子 (排序因子写公式) + 账本档位 → 今日命中它的卡 */
function BoardLoop({ champs, tierOf, cards, catalog }: { champs: BoardRow[]; tierOf: (r: BoardRow) => string | null; cards: SwingBoardCard[]; catalog: WatchlistRuleCatalogItem[] }) {
  const byId = new Map(catalog.map(c => [c.id, c]))
  if (!champs.length) return <div data-loop="board" className="mt-1.5 text-[12px] text-muted">槽位暂无在用规则</div>
  return (
    <div data-loop="board" className="mt-1.5 flex flex-col gap-2">
      {champs.map(r => {
        const hit = cards.filter(c => c.strategies.some(s => s.id === r.id))
        const cat = byId.get(r.id)
        const tier = tierOf(r)
        return (
          <div key={r.id} data-loop-rule={r.id} className="rounded-btn border border-border/70">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-2.5 py-1.5 text-[12px]">
              <b className="text-foreground">{r.name}</b>
              {tier && <span className="text-secondary">{`${TIER_ICON[tier] ?? '·'} ${tier}`}</span>}
              <span className="text-muted">{`引用 ${cat?.formula ? `公式 ${cat.formula}` : `${boardKind(r.kind)}规则 ${r.id}`}`}</span>
              <span className="text-secondary">{`近期 ${fmtR(r.excess)} · ${r.n ?? 0} 笔`}</span>
              <span data-loop-count={hit.length} className="ml-auto font-semibold tabular-nums text-foreground">{`今日卡 ${hit.length}`}</span>
            </div>
            {hit.length > 0 && (
              <FillGrid data-loop-cards={r.id} className={GRID.replace('px-3 pb-3', 'px-2.5 pb-2.5').replace('280px', '150px').replace('auto-rows-fr ', '')}>
                {hit.map(c => (
                  <div key={c.card_id ?? `${c.symbol}${c.side}`} data-loop-card={c.symbol}
                    className={`flex min-w-0 flex-wrap items-center gap-x-1.5 rounded-card border px-2 py-1.5 text-[12px] ${isObserve(c) ? 'border-dashed border-secondary/70' : 'border-border'}`}>
                    <b className="text-foreground">{c.symbol}</b>
                    <span className="text-secondary">{c.side === 'short' ? '↓' : '↑'}</span>
                    <span className="text-muted">{c.timing_tier === 'T1' ? '盘中切换' : c.timing_tier === 'T0' ? '跨日有效' : ''}</span>
                    {isObserve(c) && <span className="text-secondary">观察级</span>}
                    <span className="ml-auto tabular-nums text-foreground">{fmtR(c.score)}</span>
                  </div>
                ))}
              </FillGrid>
            )}
          </div>
        )
      })}
    </div>
  )
}

function BoardSlot({ d }: { d: SlotData }) {
  const [gear, setGear] = useState(false)
  const longIds = d.settings?.long ?? []
  const rows = boardRows(longIds, d.rules.data?.catalog ?? []).map(r => ({ ...r, name: humanizeName(r.name, d.libMap) }))
  const champs = rows.filter(r => r.role === 'champ')
  const chs = rows.filter(r => r.role === 'ch')
  // 全表同一把尺子, 刻度按数据收紧 (不再用固定 −0.15 ~ +0.25 全幅, 小区间不会挤成一段)
  const { min, max } = ciScale(rows.flatMap(r => [r.excess, r.lo, r.hi]))
  const swing = d.opp.data?.swing
  const ledger = ledgerOf(swing)
  const cellOf = (id: string) => ledger?.cells[`${id}|long`]
  const reasonOf = (r: BoardRow): TierReason => {
    if (r.role === 'ch') return challengerReason(r)
    if (r.kind === 'ranker' || r.id.startsWith('rank_')) return { word: '留用', off: false, parts: [{ text: '排序因子 · rank-lab-v2 确认合同' }, { text: `超额 ${fmtR(r.excess)}` }] }
    const c = cellOf(r.id)
    if (!ledger || ledger.status === 'disabled') return tierReason('在用', { n: r.n, excess: r.excess, lo: r.lo, hi: r.hi })
    if (ledger.status === 'unavailable') return { word: '待定', off: true, parts: [{ text: '本月无账本复核', hot: true }, { text: '全部按待定 · 不出卡' }] }
    return tierReason(c?.tier ?? '待定', { n: fin(c?.n) ? c!.n! : r.n, excess: fin(c?.excess) ? c!.excess! : null, lo: fin(c?.ci_low) ? c!.ci_low! : r.lo, hi: fin(c?.ci_high) ? c!.ci_high! : r.hi })
  }
  const tierOf = (r: BoardRow) => (r.role === 'ch' ? null : r.kind === 'ranker' ? '排序因子' : ledger?.status === 'disabled' ? '在用' : ledger?.status === 'unavailable' ? '待定' : (cellOf(r.id)?.tier ?? (ledger ? '待定' : null)))
  const [loop, setLoop] = useState(false)
  const todayCards: SwingBoardCard[] = [...(swing?.long ?? []), ...(swing?.short ?? [])].filter(c => c.state !== 'holding')
  const passed = chs.filter(r => r.passed)
  const scored = chs.filter(r => r.lo != null)
  const best = [...scored].sort((a, b) => (b.lo ?? -9) - (a.lo ?? -9))[0]
  const err = d.opp.isError || d.rules.isError
  const scoredChs = chs.filter(r => r.excess != null)
  const thinChs = chs.filter(r => r.excess == null)
  const row = (r: BoardRow) => { const why = reasonOf(r); const tier = tierOf(r); return [
    <tr key={r.id} data-board-row={r.role} data-tier={tier ?? ''} className={`${r.role === 'champ' ? 'bg-violet-500/[0.05]' : ''} ${why.off ? 'shadow-[inset_3px_0_0_hsl(var(--warning))]' : ''}`}>
      <td className="py-1.5 pl-4 pr-2 align-middle"><Role champ={r.role === 'champ'} /></td>
      <td className="py-1.5 pr-2 align-middle">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="break-words text-[13px] font-semibold text-foreground" title={r.name}>{r.name}</span>
          {r.passed && <Tag tone="ok">过验收</Tag>}
        </div>
        <div className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-secondary">
          <span>{`${r.role === 'champ' && !['ranker', 'custom', 'ai'].includes(r.kind) ? '离线回放' : boardKind(r.kind)} · ${r.n ?? 0} 笔`}</span>
          {tier && <span data-board-tier={tier} className={`inline-flex h-[18px] shrink-0 items-center gap-0.5 whitespace-nowrap rounded px-1 font-semibold ${tier === '待定' || tier === '冷冻' ? 'border border-dashed border-warning/70 text-warning' : 'bg-elevated text-secondary'}`}>{`${TIER_ICON[tier] ?? '·'} ${tier}`}</span>}
        </div>
      </td>
      <td className={`whitespace-nowrap py-1.5 pr-3 text-right align-middle text-[13px] font-semibold tabular-nums ${rToneR(r.excess)}`}>{r.note ?? fmtR(r.excess)}</td>
      <td className="whitespace-nowrap py-1.5 pr-3 text-right align-middle text-[12px] tabular-nums text-secondary">
        {r.lo != null && r.hi != null ? `${fmtR(r.lo).replace('R', '')} ~ ${fmtR(r.hi).replace('R', '')}` : r.p != null ? `p ${r.p.toFixed(3)}` : DASH}
      </td>
      <td className="py-0.5 pr-4 align-middle"><CiBar v={r.excess} lo={r.lo} hi={r.hi} min={min} max={max} width={BAR_W} strong={r.role === 'champ'} dim={why.off} /></td>
    </tr>,
    <tr key={`${r.id}-why`} data-reason-row={r.id} className={`border-b border-border/60 ${r.role === 'champ' ? 'bg-violet-500/[0.05]' : ''}`}>
      <td className="pb-1.5 pl-4" /><td colSpan={4} className="pb-1.5 pr-4"><ReasonLine r={why} /></td>
    </tr>,
  ] }
  return (
    <SlotCard no={1} title="机会板 · 做多策略" ruler="机会回放" attr={{ 'data-slot': 'board' }}
      sub={`做多 ${champs.length} 条 · 每笔超额 R（对比 4h 同向随机入场）`}
      footer={<>
        <span data-slot-verdict="" className="min-w-0 flex-1 text-secondary md:truncate">
          {err ? '读取失败' : passed.length ? `${passed.map(r => r.name).join(' · ')} 通过检验 · 可在机会板策略中启用`
            : best ? `挑战者区间都跨 0 · 下限最高 ${best.name} ${fmtR(best.lo)}` : '暂无可比挑战者'}
        </span>
        <button type="button" data-go-gear="" onClick={() => setGear(true)} disabled={!d.settings} title="和机会页右上角齿轮是同一个面板"
          className="inline-flex h-7 shrink-0 items-center gap-1 rounded-btn border border-border bg-surface px-2.5 text-xs font-medium text-secondary hover:border-accent/50 hover:text-foreground disabled:opacity-50">
          <Settings2 className="h-3.5 w-3.5" />机会板策略
        </button>
        {gear && <RulesConsole settings={d.settings} onClose={() => setGear(false)} />}
      </>}>
      {err ? <SlotError text="齿轮或规则目录读取失败" /> : (
        <div className="min-h-0 flex-1 overflow-x-auto">
          <table className="w-full min-w-[540px] table-fixed text-xs" data-board-table="">
            <colgroup><col className="w-[66px]" /><col /><col className="w-[88px]" /><col className="w-[104px]" /><col className="w-[192px]" /></colgroup>
            <thead><tr className="border-b border-border text-[11px] text-secondary">
              <th className="py-1.5 pl-4 text-left font-medium" /><th className="py-1.5 text-left font-medium">策略</th>
              <th className="py-1.5 pr-3 text-right font-medium">超额/笔</th><th className="py-1.5 pr-3 text-right font-medium" title="挑战者 = 95% 区间 · 在用 = p 值">区间 · p</th>
              <th className="py-1.5 pr-4 text-left font-medium"><CiAxis min={min} max={max} width={BAR_W} /></th>
            </tr></thead>
            <tbody>
              {champs.map(row)}
              {scoredChs.length > 0 && <tr aria-hidden="true"><td colSpan={5} className="h-1.5 border-b border-dashed border-muted p-0" /></tr>}
              {scoredChs.map(row)}
            </tbody>
          </table>
          {thinChs.length > 0 && (
            <div data-board-thin="" className="flex min-w-0 items-center gap-2 border-t border-border/60 px-4 py-2 text-[12px] text-secondary">
              <span className="shrink-0 font-medium text-foreground">{`样本不足 ${thinChs.length} 个`}</span><span className="shrink-0 text-muted">·</span>
              <span className="min-w-0 break-words" title={thinChs.map(r => `${r.name} ${r.n ?? 0} 笔`).join(' / ')}>{thinChs.map(r => `「${r.name}」${r.n ?? 0} 笔`).join('  ')}</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-[11px] text-secondary">
            <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm bg-violet-600" />在用 · 条 = 95% 区间 · 圈 = 每笔超额</span>
            <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm bg-accent" />挑战 · 实色 = 区间整体 &gt; 0</span>
            <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm" style={{ background: 'repeating-linear-gradient(45deg, hsl(var(--warning)) 0 2px, transparent 2px 4px)' }} />区间跨 0 的负段 · 不显著</span>
            <span className="inline-flex items-center gap-1.5"><i className="inline-block h-2 w-3 rounded-sm bg-bear" />区间整体 &lt; 0</span>
            <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-[2px] bg-foreground/75" />0 基准</span>
          </div>
          <div className="border-t border-border/60 px-4 py-2">
            <button type="button" data-loop-toggle="board" aria-expanded={loop} onClick={() => setLoop(v => !v)} className="text-[12px] font-medium text-accent hover:underline dark:text-blue-400">
              {loop ? '闭环 ▾ 因子 → 槽位 → 卡' : `闭环 ▸ 因子 → 槽位 → 卡 · 今日卡 ${todayCards.length}`}
            </button>
            {loop && <BoardLoop champs={champs} tierOf={tierOf} cards={todayCards} catalog={d.rules.data?.catalog ?? []} />}
          </div>
        </div>
      )}
    </SlotCard>
  )
}

// ---------------------------------------------------------------- slot 2 · 选股策略
function StockSlot({ d, onOpenReview, onOpenCandidate }: {
  d: SlotData; onOpenReview: () => void; onOpenCandidate?: (runId: string, signature: string) => void
}) {
  const qc = useQueryClient()
  const page = d.lab.data
  const w: DiscoverWeek | null | undefined = page?.week
  const decide = useMutation({
    mutationFn: ({ sig, action }: { sig: string; action: 'save' | 'discard' }) => api.factorLabDecide(w!.week, sig, action),
    onSuccess: (r, v) => {
      // 保存成功要给下一步: 「去发布」直达该候选所属那一轮挖掘并选中它 (只写「已保存」等于没给路)
      const runId = w?.run_id
      if (v.action === 'save') {
        if (r.candidate_id) toast('已保存 · 候选方案', 'success', onOpenCandidate && runId ? { action: { label: '去发布', onClick: () => onOpenCandidate(runId, v.sig) } } : undefined)
        else toast('已记录 · 候选方案未建出', 'error')
      } else toast('未采用', 'success')
      void qc.invalidateQueries({ queryKey: QK.factorLabPage }); void qc.invalidateQueries({ queryKey: QK.researchCandidates })
    },
  })
  const cands = (w?.candidates ?? []).filter((c): c is DiscoverCandidate & { stats: NonNullable<DiscoverCandidate['stats']> } => !c.error && !!c.stats)
  const decision = (c: DiscoverCandidate) => (w ? page?.decisions[`${w.week}|${c.signature}`]?.action : undefined)
  const engine = w?.engine
  const dates = (w?.curves?.dates ?? []) as string[]
  const lead = cands[0]
  const series = [
    { key: 'engine', values: (w?.curves?.engine ?? []) as (number | null)[], color: 'rgb(139 92 246)', width: 2.2 },
    ...(lead ? [{ key: lead.signature, values: (w?.curves?.[lead.signature] ?? []) as (number | null)[], color: 'hsl(var(--accent))', width: 1.6, dash: decision(lead) === 'discard' ? '6 4' : undefined }] : []),
    { key: 'spy', values: (w?.curves?.spy ?? []) as (number | null)[], color: 'hsl(var(--fg-muted))', width: 1.3, dash: '5 4' },
  ]
  const hold = (v: number | null | undefined) => (fin(v) ? `${v.toFixed(1)} 天` : DASH)
  const pending = cands.filter(c => !decision(c))
  const ann = (v: number | null | undefined) => <span className={`font-semibold tabular-nums ${rTone(v)}`}>{fmtPct(v)}</span>
  const [loop, setLoop] = useState(false)
  return (
    <SlotCard no={2} title="选股策略" ruler="选股回测" attr={{ 'data-slot': 'stock' }}
      sub={w ? `每日前 20 · 自选 ${w.symbols} · ${w.window?.[0]?.slice(0, 7) ?? ''} → ${w.window?.[1]?.slice(0, 7) ?? ''} · ${w.week.replace(/^\d{4}-/, '')}` : '本周未生成'}
      footer={<>
        <span data-slot-verdict="" className="min-w-0 flex-1 truncate text-secondary">
          {d.lab.isError ? '读取失败' : !w ? '本周未生成' : !lead ? `本周无候选 · ${weeklyText(page?.schedule)}`
            : pending.length ? `待审 ${pending.length} · 年化比在用 ${fmtPct((lead.stats.annual_return ?? 0) - (engine?.stats.annual_return ?? 0))}`
              : `本周已处理 · ${weeklyText(page?.schedule)}`}
        </span>
        {pending.length > 0 && lead && !decision(lead) && <>
          <button type="button" data-decide="discard" disabled={decide.isPending} onClick={() => decide.mutate({ sig: lead.signature, action: 'discard' })}
            className="inline-flex h-7 shrink-0 items-center rounded-btn border border-border bg-surface px-2.5 text-xs font-medium text-secondary hover:text-foreground disabled:opacity-50">不采用</button>
          <button type="button" data-decide="save" disabled={decide.isPending} onClick={() => decide.mutate({ sig: lead.signature, action: 'save' })}
            className="inline-flex h-7 shrink-0 items-center rounded-btn border border-accent bg-accent/10 px-2.5 text-xs font-semibold text-foreground hover:bg-accent/20 disabled:opacity-50">保存</button>
        </>}
        <button type="button" data-go-review="" onClick={onOpenReview}
          className="inline-flex h-7 shrink-0 items-center gap-1 rounded-btn border border-border bg-surface px-2.5 text-xs font-medium text-secondary hover:border-accent/50 hover:text-foreground">
          详情<ArrowRight className="h-3.5 w-3.5" />
        </button>
      </>}>
      {d.lab.isError ? <SlotError text="周度发现读取失败" /> : !w || !engine ? <SlotError text={d.lab.isLoading ? '加载中' : '本周未生成'} /> : <>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] table-fixed text-xs" data-stock-table="">
            <colgroup><col className="w-[66px]" /><col /><col className="w-[76px]" /><col className="w-[56px]" /><col className="w-[84px]" /><col className="w-[84px]" /></colgroup>
            <thead><tr className="border-b border-border text-[11px] text-secondary">
              <th className="py-1.5 pl-4 text-left font-medium" /><th className="py-1.5 text-left font-medium">方案</th>
              <th className="py-1.5 pr-3 text-right font-medium">年化</th><th className="py-1.5 pr-3 text-right font-medium">夏普</th>
              <th className="py-1.5 pr-3 text-right font-medium">最大回撤</th><th className="py-1.5 pr-4 text-right font-medium">平均持有</th>
            </tr></thead>
            <tbody>
              <tr data-stock-row="champ" className="border-b border-border/60 bg-violet-500/[0.05]">
                <td className="py-1.5 pl-4 pr-2"><Role champ /></td>
                <td className="py-1.5 pr-2"><div className="truncate text-[13px] font-semibold text-foreground">现行策略组合</div>
                  <div className="truncate text-[11px] text-secondary" title={engine.legs.map(l => displayStrategyName(l.name)).join(' · ')}>{engine.legs.map(l => displayStrategyName(l.name)).join(' · ')}</div></td>
                <td className="py-1.5 pr-3 text-right">{ann(engine.stats.annual_return)}</td>
                <td className="py-1.5 pr-3 text-right font-semibold tabular-nums text-foreground">{fin(engine.stats.sharpe) ? engine.stats.sharpe.toFixed(2) : DASH}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums text-foreground">{fmtPct(engine.stats.max_drawdown)}</td>
                <td className="py-1.5 pr-4 text-right tabular-nums text-foreground">{hold(engine.stats.avg_holding_days)}</td>
              </tr>
              {cands.map(c => {
                const dd = decision(c)
                const long = fin(c.stats.avg_holding_days) && c.stats.avg_holding_days > 7
                return (
                  <tr key={c.signature} data-stock-row="ch" className="border-b border-border/60 last:border-b-0">
                    <td className="py-1.5 pl-4 pr-2"><Role champ={false} /></td>
                    <td className="py-1.5 pr-2"><div className="flex min-w-0 items-center gap-1.5"><span className="min-w-0 truncate text-[13px] font-semibold text-foreground" title={c.name}>{c.name}</span>
                      {dd === 'save' ? <Tag tone="ok">已保存</Tag> : dd === 'discard' ? <Tag>未采用</Tag> : <Tag tone="run">待审</Tag>}</div>
                      <div className="truncate text-[11px] text-secondary">{`周度挖掘 · ${(c.legs ?? []).length} 个因子 · 样本外夏普 ${fin(c.oos?.oos_sharpe) ? c.oos!.oos_sharpe!.toFixed(2) : DASH}`}</div></td>
                    <td className="py-1.5 pr-3 text-right">{ann(c.stats.annual_return)}</td>
                    <td className="py-1.5 pr-3 text-right font-semibold tabular-nums text-foreground">{fin(c.stats.sharpe) ? c.stats.sharpe.toFixed(2) : DASH}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-foreground">{fmtPct(c.stats.max_drawdown)}</td>
                    <td className={`py-1.5 pr-4 text-right tabular-nums ${long ? 'font-semibold text-warning' : 'text-foreground'}`} title={long ? '波段上限 7 天' : undefined}>{hold(c.stats.avg_holding_days)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-4 px-4 pt-2 text-[11px] text-secondary" data-stock-legend="">
          <span className="inline-flex items-center gap-1.5"><i className="inline-block h-[3px] w-4 rounded bg-violet-500" />在用</span>
          {lead && <span className="inline-flex items-center gap-1.5"><i className="inline-block h-[3px] w-4 rounded" style={decision(lead) === 'discard' ? { background: 'repeating-linear-gradient(90deg, hsl(var(--accent)) 0 4px, transparent 4px 7px)' } : { background: 'hsl(var(--accent))' }} />{decision(lead) === 'discard' ? '挑战 · 未采用' : '挑战'}</span>}
          <span className="inline-flex items-center gap-1.5"><i className="inline-block h-[3px] w-4 rounded" style={{ background: 'repeating-linear-gradient(90deg, hsl(var(--fg-muted)) 0 4px, transparent 4px 7px)' }} />SPY</span>
          <span className="ml-auto" title="曲线约每 24 个交易日取一点 · 最大回撤按日净值计算，可能深于曲线所示">抽样净值 · 起点 1.00</span>
        </div>
        <div className="flex min-h-0 flex-1 px-2 pb-1"><NavLines dates={dates} series={series} /></div>
        <div className="border-t border-border/60 px-4 py-2">
          <button type="button" data-loop-toggle="stock" aria-expanded={loop} onClick={() => setLoop(v => !v)} className="text-[12px] font-medium text-accent hover:underline dark:text-blue-400">
            {loop ? '闭环 ▾ 引用 → 槽位' : `闭环 ▸ 引用 ${engine.legs.length} 个策略`}
          </button>
          {loop && (
            <div data-loop="stock" className="mt-1.5 flex flex-col gap-1 text-[12px]">
              <div className="flex flex-wrap gap-1.5">{engine.legs.map(l => <span key={l.name} data-loop-leg="" className="rounded bg-elevated px-1.5 py-0.5 text-foreground">{displayStrategyName(l.name)}</span>)}</div>
              <div className="text-secondary">{`近期表现 年化 ${fmtPct(engine.stats.annual_return)} · 夏普 ${fin(engine.stats.sharpe) ? engine.stats.sharpe.toFixed(2) : DASH} · 不在策略账本 (口径 = 选股回测 · 与机会板超额 R 不同尺)`}</div>
            </div>
          )}
        </div>
      </>}
    </SlotCard>
  )
}

// ---------------------------------------------------------------- slot 3 · 策略打分
const FLAG_TIP: Record<ScoreFlag, string> = {
  缺数据: '没有美股数据源 · 打分时这一项被跳过',
  无检验: '原始列 · 能算但不在因子库 · 周度挖掘不评',
  方向不一: '有的策略要大值 · 有的要小值',
  方向相反: `近 ${WEEKS_SHOWN} 次周度的训练窗 IC 折算后都 < 0 且 |IC| ≥ ${REVERSE_MIN} · 打分偏好的方向在主题池上表现更差 · 非策略命中后的条件效果`,
}
/** 2026-11-15 在用策略引用了退役或重复因子 → 琥珀标签 (成熟度只当标签, 不改打分权重) */
function matTags(f: FactorLibraryItem | undefined, meta: FactorMaturityMeta | undefined, libMap: Map<string, FactorLibraryItem>) {
  const m = f?.maturity
  if (!f || !m) return null
  // 一行只放一个标签 (退役 / 重复 / 退役·重复), 说明放悬停: 多个标签竖叠会把表格行撑高
  const retired = !!meta?.launched && m.state === 'retired'
  const parts = [retired ? '退役' : '', m.dup ? '重复' : ''].filter(Boolean)
  if (!parts.length) return null
  const title = [retired ? maturityTitle(m, meta) : '', m.dup ? `与 ${libMap.get(m.dup.of)?.label ?? m.dup.of} 秩相关 ${m.dup.rho.toFixed(2)} · 对方更强` : ''].filter(Boolean).join('\n')
  return <span data-mat-tag={retired ? (m.dup ? 'retired dup' : 'retired') : 'dup'} title={title}><Tag tone="warn">{parts.join('·')}</Tag></span>
}

function ScoringSlot({ d, onInspect }: { d: SlotData; onInspect: (id: string) => void }) {
  const { uses, nStrat } = d.scoring
  const weeks = d.weeklyRuns.map(w => w.week)
  const challengers = useMemo(() => scoringChallengers(uses, d.libMap, d.icMap, d.lab.data, weeks.length), [uses, d.libMap, d.icMap, d.lab.data, weeks.length])
  const reverse = uses.filter(u => u.flag === '方向相反')
  const reversePct = (() => { const r = roundShares(uses.map(x => x.share)); return uses.reduce((a, u, i) => a + (u.flag === '方向相反' ? r[i] : 0), 0) })()
  const bestCh = challengers.filter(c => c.tag === 'pool' && c.dirMean != null).sort((a, b) => (b.dirMean ?? 0) - (a.dirMean ?? 0))[0]
  const err = d.strategies.isError
  const cell = 'py-1.5 pr-3'
  const pctOf = new Map(uses.map((u, i) => [u.id, roundShares(uses.map(x => x.share))[i]]))
  const icScale = Math.max(IC_GATE * 1.5, ...[...uses.map(u => u.dirMean), ...challengers.map(c => c.dirMean)].filter(fin).map(Math.abs)) * 1.05
  const lastRes = d.weeklyRuns[d.weeklyRuns.length - 1]?.result
  const lastFold = (lastRes?.folds ?? []).filter(f => !f.skipped).slice(-1)[0]
  const trainWin = lastFold?.train_start && lastFold.train_end ? `${lastFold.train_start.slice(0, 7)} → ${lastFold.train_end.slice(0, 7)}` : ''
  return (
    <SlotCard no={3} title="策略打分" ruler="周度 IC" attr={{ 'data-slot': 'scoring' }}
      sub={`${nStrat} 个策略的打分权重 · IC = 周度挖掘训练窗${trainWin ? ` ${trainWin}` : ''} 主题池 5 日 Rank IC · 已按打分方向折算`}
      footer={<>
        <span data-slot-verdict="" className="min-w-0 flex-1 truncate text-secondary">
          {err ? '读取失败' : reverse.length ? `方向相反 ${reverse.length} 个 · 合计权重 ${reversePct}%${bestCh ? ` · 挑战者最高 ${bestCh.label} ${fmtIc(bestCh.dirMean)}` : ''}`
            : bestCh ? `挑战者最高 ${bestCh.label} ${fmtIc(bestCh.dirMean)}` : '暂无挑战者'}
        </span>
        <span className="shrink-0 text-[11px] text-muted">{`方向相反 = 4 次都 < 0 且 |IC| ≥ ${REVERSE_MIN} · 周度门槛 |IC| ≥ ${IC_GATE}`}</span>
      </>}>
      {err ? <SlotError text="策略列表读取失败" /> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[960px] table-fixed text-xs" data-scoring-table="">
            <colgroup><col className="w-[66px]" /><col className="w-[200px]" /><col className="w-[88px]" /><col className="w-[60px]" /><col /><col className="w-[150px]" /><col className="w-[232px]" /><col className="w-[150px]" /></colgroup>
            <thead><tr className="border-b border-border text-[11px] text-secondary">
              <th className="py-1.5 pl-4 text-left font-medium" /><th className="py-1.5 text-left font-medium">因子</th>
              <th className="py-1.5 pr-3 text-right font-medium" title="各策略内部权重归一后平均">权重占比</th>
              <th className="py-1.5 pr-3 text-right font-medium">引用</th>
              <th className="py-1.5 pr-3 text-left font-medium">策略</th>
              <th className="py-1.5 pr-3 text-left font-medium" title="每个点 = 一次周度挖掘 · 实心 = 被选中">周度入选<span className="ml-1 font-normal text-muted">{weeks.length ? `${weeks[0]}–${weeks[weeks.length - 1]}` : ''}</span></th>
              <th className="py-1.5 pr-3 font-medium" title="近几次周度的训练窗 IC 平均 · 按打分方向折算 · 虚线 = 周度门槛 ±0.02"><div className="flex items-center justify-end gap-2"><IcAxis scale={icScale} /><span className="w-[52px] text-right">折算 IC</span></div></th>
              <th className="py-1.5 pr-4 text-left font-medium">状态</th>
            </tr></thead>
            <tbody>
              {uses.map(u => {
                const by = [...u.uses].sort((a, b) => b.w - a.w)
                return (
                  <tr key={u.id} data-score-row="champ" data-flag={u.flag ?? ''} className={`border-b border-border/60 bg-violet-500/[0.04] ${u.flag === '方向相反' || u.flag === '缺数据' ? 'shadow-[inset_3px_0_0_hsl(var(--warning))]' : ''}`}>
                    <td className="py-1.5 pl-4 pr-2"><Role champ /></td>
                    <td className="py-1.5 pr-2"><span className="block truncate text-[13px] font-semibold text-foreground" title={u.id}>{u.label}</span></td>
                    <td className={`${cell} text-right font-semibold tabular-nums text-foreground`}>{pctOf.get(u.id)}%</td>
                    <td className={`${cell} text-right font-semibold tabular-nums text-foreground`}>{u.n}</td>
                    <td className={cell}><span className="block truncate text-[12px] text-secondary" title={by.map(x => `${x.name} ${x.w.toFixed(2)}`).join(' / ')}>{by.slice(0, 3).map(x => x.name).join(' · ')}{by.length > 3 ? ` 等 ${by.length} 个` : ''}</span></td>
                    <td className={cell}><SelDots sel={u.sel} weeks={weeks} /></td>
                    <td className={cell}><div className="flex items-center justify-end gap-2"><IcBar v={u.sign === 0 ? null : u.dirMean} scale={icScale} />
                      <span className={`w-[52px] text-right font-semibold tabular-nums ${u.flag === '方向相反' ? 'text-warning' : 'text-foreground'}`}>{fmtIc(u.sign === 0 ? null : u.dirMean)}</span></div></td>
                    <td className="py-1.5 pr-4"><span className="flex flex-nowrap gap-1 whitespace-nowrap">
                      {u.flag && <span title={FLAG_TIP[u.flag]}><Tag tone={u.flag === '方向不一' || u.flag === '无检验' ? 'mute' : 'warn'}>{u.flag}</Tag></span>}
                      {matTags(d.libMap.get(u.id), d.lib.data?.maturity, d.libMap)}
                    </span></td>
                  </tr>
                )
              })}
              {challengers.length > 0 && <tr aria-hidden="true"><td colSpan={8} className="h-1.5 border-b border-dashed border-muted p-0" /></tr>}
              {challengers.map(c => (
                <tr key={c.id} data-score-row="ch" className="border-b border-border/60 last:border-b-0">
                  <td className="py-1.5 pl-4 pr-2"><Role champ={false} /></td>
                  <td className="py-1.5 pr-2"><div className="flex min-w-0 items-center gap-1.5"><span className="min-w-0 truncate text-[13px] font-semibold text-foreground" title={c.id}>{c.label}</span>
                    {c.tag === 'pending' ? <Tag tone="run">待审</Tag> : c.tag === 'watch' ? <Tag>观察</Tag> : null}</div>
                    {c.dir && <div className="truncate text-[11px] text-secondary">{`${c.dir}值优先`}</div>}</td>
                  <td className={`${cell} text-right text-muted`}>{DASH}</td>
                  <td className={`${cell} text-right text-muted`}>{DASH}</td>
                  <td className={`${cell} text-[12px] text-secondary`}>{c.note ?? (c.tag === 'pool' ? '未被打分引用' : DASH)}</td>
                  <td className={cell}>{c.tag === 'pending' ? <span className="text-[11px] text-muted">待审 · 采集复核</span> : <SelDots sel={c.sel} weeks={weeks} />}</td>
                  <td className={cell}><div className="flex items-center justify-end gap-2"><IcBar v={c.dirMean} scale={icScale} />
                    <span className="w-[52px] text-right font-semibold tabular-nums text-foreground">{fmtIc(c.dirMean)}</span></div></td>
                  <td className="py-1.5 pr-4">{c.tag !== 'pending' && (
                    <button type="button" data-inspect={c.id} onClick={() => onInspect(c.id)} className="text-[12px] font-medium text-accent hover:underline">检验</button>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SlotCard>
  )
}

// ---------------------------------------------------------------- idle strip
function IdleStrip({ d }: { d: SlotData }) {
  const lib = d.lib.data?.factors ?? []
  const used = new Set(d.scoring.uses.map(u => u.id))
  const inScore = lib.filter(f => used.has(f.id)).length
  const outLib = d.scoring.uses.length - inScore
  const missing = lib.filter(f => f.available === false && !used.has(f.id)).length
  const spare = lib.length - inScore - missing
  const it = d.lab.data?.intake
  const next = it?.next_tick ? new Date(it.next_tick).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }) : DASH
  return (
    <div data-idle="" className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-card border border-border bg-surface px-4 py-2.5 text-xs text-secondary">
      <span>因子库 <b className="tabular-nums text-foreground">{lib.length || DASH}</b></span>
      <span>进打分 <b className="tabular-nums text-foreground">{inScore}</b>{outLib > 0 && <span className="text-muted">{` + 库外 ${outLib}`}</span>}</span>
      <span>未使用 <b className="tabular-nums text-foreground">{lib.length ? spare : DASH}</b></span>
      <span>缺数据 <b className="tabular-nums text-foreground">{missing}</b></span>
      <span className="ml-auto">采集 · 下次 <b className="tabular-nums text-foreground">{next}</b> 北京</span>
      <span data-weekly-sched={d.lab.data?.schedule.enabled === false ? 'off' : 'on'}>周度挖掘 · <b className="tabular-nums text-foreground">{weeklyText(d.lab.data?.schedule)}</b></span>
    </div>
  )
}

// ---------------------------------------------------------------- views
export function FactorSlots({ onInspect, onOpenReview, onOpenCandidate }: {
  onInspect: (id: string) => void; onOpenReview: () => void
  /** 保存成功提示里的「去发布」: 打开该候选所属那一轮挖掘并选中它 */
  onOpenCandidate?: (runId: string, signature: string) => void
}) {
  const d = useSlotData()
  const op = d.opp.data
  return (
    <div data-factor-slots="" className="pb-2">
      <RegimeBar regime={regimeOf(op?.swing)} clockEt={op?.radar_view?.clock.et} tradingDay={op?.radar_view?.clock.trading_day} />
      {/* 1 / 2 同一行等高: grid 默认 stretch, 卡片 h-full + 页脚 mt-auto, 净值线铺满剩余高度 */}
      <div data-slot-row="" className="grid grid-cols-1 items-stretch gap-3 xl:grid-cols-2">
        <BoardSlot d={d} />
        <StockSlot d={d} onOpenReview={onOpenReview} onOpenCandidate={onOpenCandidate} />
      </div>
      <div className="mt-3"><ScoringSlot d={d} onInspect={onInspect} /></div>
      <IdleStrip d={d} />
    </div>
  )
}

export function FactorMatrix() {
  const d = useSlotData()
  const { uses, nStrat } = d.scoring
  const strategies = d.strategies.data?.presets ?? []
  const scoredStrats = strategies.filter(s => Object.values(s.scoring ?? {}).some(w => fin(w) && w > 0))
  const famCount = FAMS.map(f => scoredStrats.filter(s => strategyFamily(s.tags) === f).length)
  const inPlayIds = new Set([...(d.settings?.long ?? []), ...(d.settings?.short ?? [])])
  const inPlayScored = scoredStrats.filter(s => inPlayIds.has(s.id))
  const pcts = roundShares(uses.map(u => u.share))
  const weeks = d.weeklyRuns.map(w => w.week)
  // 底色 4 档: 该族里这个因子的权重和 (每个策略内部归一后相加)
  const BINS = [0.5, 1.5, 3]
  const bin = (w: number) => (w < BINS[0] ? 0 : w < BINS[1] ? 1 : w < BINS[2] ? 2 : 3)
  const BIN_BG = ['hsl(var(--accent) / 0.14)', 'hsl(var(--accent) / 0.32)', 'hsl(var(--accent) / 0.55)', 'hsl(var(--accent) / 0.8)']
  if (d.strategies.isError) return <div className="rounded-card border border-border bg-surface p-6 text-center text-xs text-muted">策略列表读取失败</div>
  return (
    <div data-factor-matrix="" className="pb-2">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <h2 className="text-[14px] font-semibold text-foreground">依赖矩阵</h2>
        <span className="text-xs text-secondary">{`打分因子 ${uses.length} × ${nStrat} 个打分策略 · 按策略族`}</span>
        <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-secondary">
          <span>格内 = 引用策略数</span>
          <span className="inline-flex items-center gap-1">权重和
            {BIN_BG.map((bg, i) => <i key={i} className="inline-block h-2.5 w-4 rounded-sm" style={{ background: bg }} title={['< 0.5', '0.5–1.5', '1.5–3', '≥ 3'][i]} />)}
          </span>
          <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-4 rounded-sm ring-[1.5px] ring-inset ring-warning" />方向相反或缺数据仍在用</span>
        </span>
      </div>
      <div className="overflow-x-auto rounded-card border border-border bg-surface">
        <table className="w-full min-w-[980px] table-fixed text-xs" data-matrix-table="">
          <colgroup><col className="w-[150px]" />{FAMS.map(f => <col key={f} className="w-[96px]" />)}<col className="w-[64px]" /><col className="w-[96px]" /><col className="w-[80px]" /><col className="w-[120px]" /><col className="w-[84px]" /><col className="w-[104px]" /></colgroup>
          <thead><tr className="border-b border-border text-[11px] text-secondary">
            <th className="py-2 pl-4 text-left font-medium">因子</th>
            {FAMS.map((f, i) => <th key={f} className="px-2 py-2 text-center font-medium" title={scoredStrats.filter(s => strategyFamily(s.tags) === f).map(s => displayStrategyName(s.name)).join(' / ')}>{f}<span className="ml-1 font-normal text-muted">{famCount[i]}</span></th>)}
            <th className="px-2 py-2 text-right font-medium" title="四族合计">引用</th>
            <th className="border-x border-border bg-violet-500/[0.06] px-2 py-2 text-center font-medium" title={`入选策略中用到打分的策略（已含在左侧各族内）: ${inPlayScored.map(s => displayStrategyName(s.name)).join(' / ') || '无'}`}>其中入选<span className="ml-1 font-normal text-muted">{inPlayScored.length}</span></th>
            <th className="px-2 py-2 text-right font-medium">权重占比</th>
            <th className="px-2 py-2 text-left font-medium" title="每个点 = 一次周度挖掘 · 实心 = 被选中">周度入选</th>
            <th className="px-2 py-2 text-right font-medium" title="训练窗 IC · 已按打分方向折算">折算 IC</th>
            <th className="py-2 pl-3 pr-4 text-left font-medium">状态</th>
          </tr></thead>
          <tbody>
            {uses.map((u, ui) => {
              const risky = u.flag === '方向相反' || u.flag === '缺数据'
              const play = u.uses.filter(x => x.inPlay)
              const cellOf = (hits: ScoreUse['uses'], key: string, inPlay = false) => {
                if (!hits.length) return <td key={key} className="px-2 py-1.5 text-center text-muted">·</td>
                const wsum = hits.reduce((a, x) => a + x.w, 0)
                return (
                  <td key={key} className={`px-2 py-1.5 text-center ${inPlay ? 'border-x border-border bg-violet-500/[0.06]' : ''}`} title={hits.map(x => `${x.name} ${x.w.toFixed(2)}`).join(' / ')}>
                    <span data-matrix-cell="" data-bin={bin(wsum)} className={`inline-flex h-6 min-w-[56px] items-center justify-center gap-0.5 rounded px-1.5 text-[12px] text-foreground ${risky ? 'ring-[1.5px] ring-inset ring-warning' : ''}`}
                      style={{ background: BIN_BG[bin(wsum)] }}><b className="tabular-nums">{hits.length}</b><span className="text-[11px]">策略</span></span>
                  </td>
                )
              }
              return (
                <tr key={u.id} data-matrix-row={u.id} className={`border-b border-border/60 last:border-b-0 ${risky ? 'shadow-[inset_3px_0_0_hsl(var(--warning))]' : ''}`}>
                  <td className="py-1.5 pl-4 pr-2"><span className="block truncate text-[13px] font-semibold text-foreground" title={u.id}>{u.label}</span></td>
                  {FAMS.map(f => cellOf(u.uses.filter(x => x.fam === f), f))}
                  <td className="px-2 py-1.5 text-right font-semibold tabular-nums text-foreground">{u.n}</td>
                  {play.length ? cellOf(play, 'play', true) : <td className="border-x border-border bg-violet-500/[0.06] px-2 py-1.5 text-center text-muted">·</td>}
                  <td className="px-2 py-1.5 text-right font-semibold tabular-nums text-foreground">{pcts[ui]}%</td>
                  <td className="px-2 py-1.5">{u.ics.some(fin) ? <SelDots sel={u.sel} weeks={weeks} /> : <span className="text-[11px] text-muted">{DASH}</span>}</td>
                  <td className={`px-2 py-1.5 text-right font-semibold tabular-nums ${u.flag === '方向相反' ? 'text-warning' : 'text-foreground'}`}>{fmtIc(u.sign === 0 ? null : u.dirMean)}</td>
                  <td className="py-1.5 pl-3 pr-4">{u.flag && <span title={FLAG_TIP[u.flag]}><Tag tone={u.flag === '方向不一' || u.flag === '无检验' ? 'mute' : 'warn'}>{u.flag}</Tag></span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        <span>{`缺数据 = 没有美股数据源 · 打分时这一项被跳过`}</span>
        <span>{`无检验 = 原始列 · 能算但不在因子库 · 周度不评`}</span>
        <span>{`方向相反 = ${weeks.length || WEEKS_SHOWN} 次周度训练窗 IC 折算后都 < 0 且 |IC| ≥ ${REVERSE_MIN}`}</span>
        <span>方向不一 = 有的策略要大值 · 有的要小值 · 不折算</span>
        <span>权重和 = 该族里每个策略对这个因子的权重相加 · 每个策略内部合计 1</span>
      </div>
    </div>
  )
}
