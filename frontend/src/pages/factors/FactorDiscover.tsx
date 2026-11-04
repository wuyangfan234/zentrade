/**
 * 因子页「发现」首页 (0.47.0)。
 *
 * 一条自动流水线: 采集 (每 6 小时) → 筛因子 → 组合 → 验证 → 回测 (每周五) → 审核。
 * 页面只读 /api/factor-lab/page 一个聚合接口; 系统不主动发布, 候选只有「保存 / 放弃」。
 * 文案约定: 只放短名词 + 数字 + 状态 + 按钮, 不放整句说明。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sheet } from '@/components/opportunities/Sheet'
import { toast } from '@/components/Toast'
import {
  api,
  type DiscoverCandidate,
  type DiscoverCheck,
  type DiscoverStats,
  type DiscoverTrade,
  type FactorLabPage,
  type IntakeCard,
  type IntakeSource,
  type IntakeSubmission,
  type DiscoverRecalc,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const DASH = '—'
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return DASH
  const s = `${(Math.abs(v) * 100).toFixed(digits)}%`
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s
}
function num(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? DASH : v.toFixed(digits)
}
function tone(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v === 0) return 'text-foreground'
  return v > 0 ? 'text-bull' : 'text-bear'
}
const CHECK_LABEL: Record<string, string> = {
  trades: '回测交易 ≥ 30',
  oos_sharpe: '样本外夏普 ≥ 0.5',
  years_positive: '2023–2025 正收益',
  hold_days: '平均持有 ≤ 7 天',
  max_drawdown: '最大回撤 ≤ 25%',
  corr_engine: '与当前出卡引擎相关 < 0.7',
  corr_spy: '与 SPY 相关 < 0.7',
  quote: '原句核对',
  compile: '编译',
  period: '周期一致',
  dedupe: '查重 < 0.9',
  t5: '5日 t ≥ 2 (按方向)',
}
function checkValue(c: DiscoverCheck): string {
  const [, key, v] = c
  if (v == null) return DASH
  if (typeof v === 'string') return v
  if (key === 'max_drawdown') return pct(v)
  if (key === 'hold_days') return `${v.toFixed(1)} 天`
  if (key === 'trades') return String(v)
  return v.toFixed(2)
}

const OBSERVE_NOTE = '观察通过 · 只过样本外夏普 · 未过完整门槛'

// ---------------------------------------------------------------- small pieces
function Card({ children, className = '', dashed = false, attr }: { children: ReactNode; className?: string; dashed?: boolean; attr?: Record<string, string> }) {
  return (
    <div {...attr} className={`rounded-card border ${dashed ? 'border-dashed border-muted bg-base' : 'border-border bg-surface'} ${className}`}>
      {children}
    </div>
  )
}
function Chip({ children, dashed = false }: { children: ReactNode; dashed?: boolean }) {
  return <span className={`inline-flex h-5 items-center whitespace-nowrap rounded-[5px] border bg-surface px-1.5 text-[11px] text-secondary ${dashed ? 'border-dashed border-muted' : 'border-border'}`}>{children}</span>
}
function Pill({ children, kind = 'mute' }: { children: ReactNode; kind?: 'run' | 'mute' | 'warn' }) {
  const cls = kind === 'run' ? 'bg-accent/10 text-foreground' : kind === 'warn' ? 'bg-warning/10 text-foreground' : 'bg-elevated text-secondary'
  const dot = kind === 'run' ? 'bg-accent' : kind === 'warn' ? 'bg-warning' : 'bg-current'
  return <span className={`inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-full px-2 text-[11px] font-semibold ${cls}`}><span className={`h-1.5 w-1.5 rounded-full ${dot}`} />{children}</span>
}
function Seg<T extends string>({ value, options, onChange, attr }: { value: T; options: [T, string][]; onChange: (v: T) => void; attr?: string }) {
  return (
    <div data-seg={attr} className="inline-flex overflow-hidden rounded-btn border border-border bg-surface">
      {options.map(([v, label]) => (
        <button key={v} type="button" onClick={() => onChange(v)}
          className={`h-7 whitespace-nowrap border-r border-border px-2.5 text-xs last:border-r-0 ${value === v ? 'bg-accent/10 font-semibold text-foreground shadow-[inset_0_-2px_0_hsl(var(--accent))]' : 'text-secondary hover:text-foreground'}`}>
          {label}
        </button>
      ))}
    </div>
  )
}
function Btn({ children, onClick, primary = false, disabled = false, attr }: { children: ReactNode; onClick?: () => void; primary?: boolean; disabled?: boolean; attr?: Record<string, string> }) {
  return (
    <button {...attr} type="button" onClick={onClick} disabled={disabled}
      className={`inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-btn border px-2.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${primary ? 'border-accent bg-accent/10 font-semibold text-foreground hover:bg-accent/20' : 'border-border bg-surface text-secondary hover:border-accent/50 hover:text-foreground'}`}>
      {children}
    </button>
  )
}
function Bar({ checks }: { checks: DiscoverCheck[] }) {
  return (
    <span className="flex gap-0.5">
      {checks.map((c, i) => <i key={i} className={`h-1.5 w-2.5 rounded-sm ${c[0] ? 'bg-secondary/35' : 'bg-warning'}`} />)}
    </span>
  )
}
function Metric({ label, value, cls = 'text-foreground' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-secondary">{label}</div>
      <div className={`truncate text-[15px] font-semibold tabular-nums ${cls}`}>{value}</div>
    </div>
  )
}

// ---------------------------------------------------------------- flow bar
function FlowBar({ page, onStep }: { page: FactorLabPage; onStep: (i: number) => void }) {
  const w = page.week
  const it = page.intake
  const pendingCands = (w?.candidates ?? []).filter(c => !c.error && !page.decisions[`${w?.week}|${c.signature}`]).length
  const pendingFactors = it?.funnel.pending ?? 0
  const observeCount = (w?.candidates ?? []).filter(c => c.oos_gate === 'observe').length
  const steps: { no: number; t: string; tag: string; big: string; unit: string; sub: string }[] = [
    { no: 1, t: '采集', tag: '每 6h', big: it ? String(it.funnel.formulas) : DASH, unit: '公式', sub: it ? '近 30 天' : '读取失败' },
    { no: 2, t: '筛因子', tag: '每周五', big: String(w?.funnel.factors_selected ?? DASH), unit: `/ ${w?.funnel.factors_total ?? DASH} 因子`, sub: w?.week?.replace(/^\d{4}-/, '') ?? DASH },
    { no: 3, t: '组合', tag: '每周五', big: String(w?.funnel.combos ?? DASH), unit: '个', sub: w?.week?.replace(/^\d{4}-/, '') ?? DASH },
    { no: 4, t: '验证', tag: '每周五', big: String(w?.funnel.oos_passed ?? w?.funnel.passed ?? DASH), unit: '通过', sub: observeCount ? `样本外 · 其中观察通过 ${observeCount}` : '样本外' },
    { no: 5, t: '回测', tag: '每周五', big: String((w?.candidates ?? []).filter(c => !c.error).length), unit: '候选', sub: `自选 ${w?.symbols ?? DASH}` },
    { no: 6, t: '审核', tag: '人工', big: String(pendingCands + pendingFactors), unit: '待审', sub: `策略 ${pendingCands} · 因子 ${pendingFactors}` },
  ]
  return (
    <Card attr={{ 'data-flow': '' }}>
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6">
        {steps.map((s, i) => (
          <button key={s.no} type="button" data-step={i} onClick={() => onStep(i)}
            className={`relative px-4 py-3.5 text-left hover:bg-elevated/60 ${i === 0 ? 'xl:border-r xl:border-dashed xl:border-muted' : 'xl:border-r xl:border-border'} last:border-r-0 ${i === 5 ? 'rounded-r-card bg-accent/10 shadow-[inset_0_0_0_2px_hsl(var(--accent))]' : ''}`}>
            <div className="flex items-center gap-2 text-[13.5px] font-semibold text-foreground">
              <span className={`grid h-5 w-5 place-items-center rounded-full text-[11px] font-bold ${i === 5 ? 'bg-surface text-foreground ring-2 ring-accent' : 'bg-elevated text-foreground'}`}>{s.no}</span>
              {s.t}
              <span className={`ml-auto rounded px-1.5 py-px text-[10.5px] font-semibold ${i === 5 ? 'bg-surface text-foreground ring-1 ring-accent' : 'bg-elevated text-secondary'}`}>{s.tag}</span>
            </div>
            <div className="mt-2.5 text-[22px] font-semibold leading-none tabular-nums text-foreground">{s.big}<small className="ml-1 text-[12.5px] font-medium text-secondary">{s.unit}</small></div>
            <div className="mt-1 whitespace-nowrap text-[11px] text-secondary">{s.sub}</div>
            {i < 5 && <span className="absolute right-[-11px] top-1/2 z-[1] hidden h-[22px] w-[22px] -translate-y-1/2 place-items-center rounded-full border border-border bg-surface text-xs text-secondary xl:grid" style={i === 4 ? { right: 3 } : undefined}>›</span>}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-4 py-2.5 text-xs text-secondary">
        <span>{w ? `${w.week.replace(/^\d{4}-/, '')} · ${w.data_as_of?.slice(5)} 收盘后` : '本周未生成'}</span>
        {it ? <span>采集 <b className="tabular-nums text-foreground">{it.sources.filter(s => s.enabled).length}</b> 源</span> : <span className="text-warning">采集 · 读取失败</span>}
        <span>股票池 <b className="tabular-nums text-foreground">{w?.funnel.pool ?? DASH}</b></span>
        <span>自选 <b className="tabular-nums text-foreground">{w?.symbols ?? DASH}</b></span>
        <span>已发布 <b className="tabular-nums text-foreground">{(w?.candidates ?? []).filter(c => c.published_strategy_id).length}</b></span>
        <span className="ml-auto">下次 · {page.schedule.next_weekly?.slice(5) ?? DASH} 周五 收盘后</span>
      </div>
    </Card>
  )
}

// ---------------------------------------------------------------- equity chart
type Series = { key: string; values: (number | null)[]; color: string; label: string; width: number; dash?: string }

function NavChart({ dates, series }: { dates: string[]; series: Series[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(760)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => { if (el.clientWidth > 0) setW(el.clientWidth) }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const H = 260, L = 44, R = 56, T = 14, B = 28
  const n = dates.length
  const all = series.flatMap(s => s.values.filter((v): v is number => v != null && Number.isFinite(v)))
  if (n < 2 || !all.length) return <div ref={ref} className="h-[260px]" />
  const mx = Math.ceil(Math.max(...all)), mn = Math.min(0.6, Math.floor(Math.min(...all) * 10) / 10)
  const x = (i: number) => L + (W - L - R) * i / (n - 1)
  const y = (v: number) => T + (H - T - B) * (1 - (v - mn) / (mx - mn))
  const path = (vals: (number | null)[]) => vals.map((v, i) => v == null ? '' : `${i && vals[i - 1] != null ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const ticks: number[] = []
  for (let t = 1; t <= mx; t++) ticks.push(t)
  const years = Array.from(new Set(dates.map(d => d.slice(0, 4)))).slice(1)
  const yi = years.map(yy => dates.findIndex(d => d >= yy)).filter(i => i > 0)
  const ends = series.map(s => {
    const last = [...s.values].reverse().find(v => v != null) as number | undefined
    return { y: last == null ? 0 : y(last), v: last, color: s.color }
  }).filter(e => e.v != null).sort((a, b) => a.y - b.y)
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14
  return (
    <div ref={ref}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} data-nav-chart="">
        {ticks.map(t => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="hsl(var(--border))" />
            <text x={L - 8} y={y(t) + 3.5} fontSize="10.5" fill="hsl(var(--fg-secondary))" textAnchor="end">{t.toFixed(1)}</text>
          </g>
        ))}
        {yi.map(i => <line key={i} x1={x(i)} x2={x(i)} y1={T} y2={H - B} stroke="hsl(var(--border))" strokeDasharray="3 3" />)}
        {[...series].reverse().map(s => <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={s.width} strokeDasharray={s.dash} strokeLinejoin="round" />)}
        {ends.map((e, i) => <text key={i} x={W - R + 8} y={e.y + 4} fontSize="11" fontWeight="600" fill={e.color}>{(e.v as number).toFixed(2)}</text>)}
        <text x={L} y={H - 8} fontSize="10.5" fill="hsl(var(--fg-secondary))">{dates[0].slice(0, 7)}</text>
        {yi.map((i, j) => (x(i) - L > 40 && W - R - x(i) > (W < 500 ? 70 : 44)) && <text key={i} x={x(i)} y={H - 8} fontSize="10.5" fill="hsl(var(--fg-secondary))" textAnchor="middle">{W < 500 ? years[j] : `${years[j]}-01`}</text>)}
        <text x={W - R} y={H - 8} fontSize="10.5" fill="hsl(var(--fg-secondary))" textAnchor="end">{dates[n - 1].slice(0, 7)}</text>
      </svg>
    </div>
  )
}

// ---------------------------------------------------------------- detail panel
type Sel = { kind: 'cand'; sig: string } | { kind: 'engine' } | { kind: 'intake'; id: string; formula?: number }

const selectionKey = (s: Sel) => (s.kind === 'cand' ? `cand:${s.sig}` : s.kind === 'intake' ? `intake:${s.id}:${s.formula ?? ''}` : 'engine')

/** 采集卡与「我的投递」共用一份详情: 两处的条目都能打开回测 (旧实现只查采集卡, 投递的「看回测」实际调了并入)。 */
function intakeItem(page: FactorLabPage, id: string): { id: string; stage: string; formulas: IntakeCard['formulas'] } | undefined {
  return page.intake?.cards.find(x => x.id === id) ?? page.intake?.submissions.find(x => x.id === id)
}

function TradesTable({ rows }: { rows: DiscoverTrade[] }) {
  return (
    <table className="w-full text-xs" data-trades="">
      <thead><tr className="text-secondary"><th className="px-3 py-1.5 text-left font-medium">代码</th><th className="px-3 py-1.5 text-left font-medium">买入</th><th className="px-3 py-1.5 text-left font-medium">卖出</th><th className="px-3 py-1.5 text-right font-medium">收益</th></tr></thead>
      <tbody>{rows.map((t, i) => (
        <tr key={i} className="border-t border-border/60"><td className="px-3 py-1.5 font-medium text-foreground">{t.symbol}</td><td className="px-3 py-1.5 text-secondary">{t.entry.slice(5)}</td><td className="px-3 py-1.5 text-secondary">{t.exit.slice(5)}</td><td className={`px-3 py-1.5 text-right font-semibold tabular-nums ${tone(t.pnl_pct)}`}>{pct(t.pnl_pct)}</td></tr>
      ))}</tbody>
    </table>
  )
}

function Detail({ page, sel, onDecide, onIntake }: {
  page: FactorLabPage; sel: Sel
  onDecide: (sig: string, action: 'save' | 'discard') => void
  onIntake: (id: string, action: 'merge' | 'ignore', formula: number) => void
}) {
  const w = page.week
  const engine = w?.engine ?? null
  const curves = w?.curves ?? { dates: [] as string[] }
  const engineYearly = engine?.yearly ?? {}
  const spyYearly = w?.spy?.yearly ?? {}
  const [pool, setPool] = useState<'watchlist' | 'theme' | 'custom'>('watchlist')
  const [customSymbols, setCustomSymbols] = useState('')
  const [start, setStart] = useState(w?.window[0] ?? '')
  const [end, setEnd] = useState(w?.window[1] ?? '')
  const symbols = customSymbols.split(/[\s,，]+/).map(x => x.trim().toUpperCase()).filter(Boolean)
  const canRecalc = ISO_DATE.test(start) && ISO_DATE.test(end) && start < end && (pool !== 'custom' || symbols.length > 0)
  // 重算结果绑定发出时的选中项: 迟到的 A 结果不能盖到已切换的 B 上
  const [override, setOverride] = useState<{ key: string; data: DiscoverRecalc } | null>(null)
  const selKey = selectionKey(sel)
  const latestKey = useRef(selKey)
  latestKey.current = selKey
  const recalc = useMutation({
    mutationFn: ({ key }: { key: string }) => api.factorLabRecalc({
      signature: sel.kind === 'engine' ? 'engine' : sel.kind === 'cand' ? sel.sig : '',
      pool, start, end, ...(pool === 'custom' ? { symbols } : {}),
    }).then(data => ({ key, data })),
    onSuccess: r => {
      if (r.key !== latestKey.current) return
      setOverride(r)
      toast('重算完成', 'success')
    },
  })
  const ov = override && override.key === selKey ? override.data : null
  const [formulaIndex, setFormulaIndex] = useState(0)
  useEffect(() => { setFormulaIndex(0) }, [selKey])

  let title = '', tag = '', stats: DiscoverStats = {}, yearly: Record<string, number> = {}, checks: DiscoverCheck[] = []
  let main: Series | null = null, legsHead: string[] = [], legs: string[][] = [], trades: DiscoverTrade[] = []
  let actions: ReactNode = null, note: ReactNode = null
  let dates = curves.dates as string[]
  const C_MAIN = 'hsl(var(--accent))', C_REF = 'hsl(var(--fg-secondary))', C_SPY = 'hsl(var(--fg-muted))'
  if (sel.kind === 'engine' && engine) {
    title = '当前出卡引擎'; tag = '对照'; stats = engine.stats; yearly = engineYearly; checks = engine.checks
    legsHead = ['规则', '资金', '年化', '交易']
    legs = engine.legs.map(l => [l.name, `1/${engine.legs.length}`, pct(l.annual_return), String(l.n_trades ?? DASH)])
    trades = engine.last_trades
  } else if (sel.kind === 'cand') {
    const c = w?.candidates.find(x => x.signature === sel.sig)
    if (c && c.stats) {
      title = c.name ?? c.signature; tag = c.oos_gate === 'observe' ? '候选 · 观察通过' : '候选'; stats = c.stats; yearly = c.stats.yearly ?? {}; checks = c.checks ?? []
      if (c.oos_gate === 'observe') {
        note = <div className="border-b border-border bg-warning/10 px-4 py-1.5 text-[11.5px] text-secondary" data-observe-note="">{OBSERVE_NOTE}{c.oos_gate_reasons?.length ? ` · ${c.oos_gate_reasons.length} 项未过` : ''}</div>
      }
      main = { key: c.signature, values: (curves[c.signature] as (number | null)[]) ?? [], color: C_MAIN, label: '候选', width: 2.4 }
      legsHead = ['因子', '方向', '权重', '样本外夏普']
      legs = (c.legs ?? []).map(l => [l.label, l.direction === 'low' ? '低' : '高', l.weight.toFixed(1), num(c.oos?.oos_sharpe)])
      trades = c.last_trades ?? []
      const decided = page.decisions[`${w!.week}|${c.signature}`]?.action
      actions = (
        <>
          <Btn primary={decided !== 'save'} disabled={decided === 'save'} onClick={() => onDecide(c.signature, 'save')} attr={{ 'data-act': 'save' }}>{decided === 'save' ? '已保存' : '保存'}</Btn>
          <Btn disabled={decided === 'discard'} onClick={() => onDecide(c.signature, 'discard')} attr={{ 'data-act': 'discard' }}>{decided === 'discard' ? '已放弃' : decided === 'save' ? '放弃 · 撤销保存' : '放弃'}</Btn>
        </>
      )
    }
  } else if (sel.kind === 'intake') {
    const it = intakeItem(page, sel.id)
    const idx = sel.formula ?? formulaIndex
    const f = it?.formulas[idx]
    const bt = f?.screen?.backtest
    if (it && f && bt) {
      title = f.name; tag = '采集'; stats = bt; yearly = bt.yearly ?? {}
      checks = intakeChecks(f)
      // 没有周度结果时用采集回测自带的日期轴, 不依赖周度曲线
      if (!dates.length && bt.curve?.dates?.length) dates = bt.curve.dates
      const map = Object.fromEntries((bt.curve?.dates ?? []).map((d, i) => [d, bt.curve!.values[i]]))
      main = { key: it.id, values: dates.map(d => map[d] ?? null), color: C_MAIN, label: '采集因子', width: 2.4 }
      legsHead = ['因子', '方向', '权重', '5日IC']
      legs = [[f.name, f.direction === 'low' ? '低' : '高', '1.0', f.screen?.ic == null ? DASH : (f.screen.ic > 0 ? '+' : '') + f.screen.ic.toFixed(3)]]
      trades = bt.last_trades ?? []
      actions = it.stage === 'pending' ? (
        <>
          <Btn primary disabled={!!f.screen?.duplicate} onClick={() => onIntake(it.id, 'merge', idx)} attr={{ 'data-act': 'merge' }}>{f.screen?.duplicate ? '重复 · 不可并入' : '并入因子库'}</Btn>
          <Btn onClick={() => onIntake(it.id, 'ignore', idx)}>忽略</Btn>
        </>
      ) : <Pill>{it.stage === 'merged' ? '已并入' : '已归档'}</Pill>
    }
  }
  const s: DiscoverStats = ov ?? stats
  const yr = ov?.yearly ?? yearly
  if (ov?.checks) checks = ov.checks
  if (ov?.last_trades) trades = ov.last_trades
  if (ov?.curve?.dates?.length) {
    // 重算区间可能和周度窗口不同: 用重算自己的日期轴画主线, 对照线按日期对齐
    const remap = (vals: (number | null)[] | undefined) => {
      const m = Object.fromEntries(dates.map((d, i) => [d, vals?.[i] ?? null]))
      return ov.curve!.dates.map(d => m[d] ?? null)
    }
    const engineVals = remap(curves.engine as (number | null)[] | undefined)
    const spyVals = remap(curves.spy as (number | null)[] | undefined)
    dates = ov.curve.dates
    curves.engine = sel.kind === 'engine' ? ov.curve.values : engineVals
    curves.spy = spyVals
    if (main) main = { ...main, values: ov.curve.values }
  }
  const series: Series[] = sel.kind === 'engine'
    ? [{ key: 'engine', values: (curves.engine as number[]) ?? [], color: C_MAIN, label: '当前出卡引擎', width: 2.4 },
       { key: 'spy', values: (curves.spy as number[]) ?? [], color: C_SPY, label: 'SPY', width: 1.6, dash: '4 3' }]
    : [main!, { key: 'engine', values: (curves.engine as number[]) ?? [], color: C_REF, label: '当前出卡引擎', width: 1.6 },
       { key: 'spy', values: (curves.spy as number[]) ?? [], color: C_SPY, label: 'SPY', width: 1.6, dash: '4 3' }]
  const cols = sel.kind === 'engine' ? ['当前出卡引擎', 'SPY'] : [main?.label ?? '', '当前出卡引擎', 'SPY']
  const years = Array.from(new Set([...Object.keys(engineYearly), ...Object.keys(yr)])).sort()
  const thisYear = years[years.length - 1]
  const ok = checks.filter(c => c[0]).length
  const kv: [string, string, string][] = [
    ['总收益', pct(s.total_return), tone(s.total_return)], ['年化', pct(s.annual_return), tone(s.annual_return)],
    ['夏普', num(s.sharpe), ''], ['卡玛', num(s.calmar), ''], ['最大回撤', pct(s.max_drawdown), tone(s.max_drawdown)],
    ['交易', String(s.n_trades ?? DASH), ''], ['胜率', pct(s.win_rate).replace('+', ''), ''],
    ['平均持有', s.avg_holding_days == null ? DASH : `${s.avg_holding_days.toFixed(1)} 天`, ''],
    ['平均盈利', pct(s.avg_win), tone(s.avg_win)], ['平均亏损', s.avg_loss == null ? DASH : pct(-Math.abs(s.avg_loss)), 'text-bear'],
    ['盈亏比', num(s.profit_factor), ''], ['成本', '$0.005/股 · 5bp', ''],
  ]
  return (
    <div id="fl-detail" className="mt-3 grid scroll-mt-24 grid-cols-1 gap-3.5 lg:grid-cols-[minmax(0,1fr)_340px]" data-detail="">
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
          <b className="mr-1 text-[13px] text-foreground" data-detail-name="">{title}</b><Chip dashed={tag === '对照'}>{tag}</Chip>
          {sel.kind === 'intake' && (() => {
            const it = intakeItem(page, sel.id)
            return it && it.formulas.length > 1 && sel.formula == null
              ? <Seg value={String(formulaIndex)} onChange={v => setFormulaIndex(Number(v))} attr="formula"
                  options={it.formulas.map((f, i) => [String(i), `${i + 1}. ${f.name}${f.screen?.duplicate ? ' · 重复' : ''}`] as [string, string])} />
              : null
          })()}
          {sel.kind !== 'intake' && w && <>
            <span className="ml-auto text-[11px] text-secondary">股票池</span>
            <Seg value={pool} onChange={setPool} options={[['watchlist', `自选 ${w.symbols}`], ['theme', `主题 ${w.funnel.pool ?? ''}`], ['custom', '自定义']]} />
            {pool === 'custom' && <input value={customSymbols} onChange={e => setCustomSymbols(e.target.value)} placeholder="AAPL, NVDA" aria-label="自定义股票" className="h-7 w-36 rounded-input border border-border bg-surface px-2 text-xs" />}
            <span className="text-[11px] text-secondary">区间</span>
            <input value={start} onChange={e => setStart(e.target.value)} className="h-7 w-[92px] rounded-input border border-border bg-surface px-2 text-xs tabular-nums" />
            <span className="text-muted">→</span>
            <input value={end} onChange={e => setEnd(e.target.value)} className="h-7 w-[92px] rounded-input border border-border bg-surface px-2 text-xs tabular-nums" />
            <Btn primary disabled={recalc.isPending || !canRecalc} onClick={() => recalc.mutate({ key: selKey })}>{recalc.isPending ? '重算中' : '重算'}</Btn>
          </>}
        </div>
        {note}
        <div className="flex flex-wrap items-center gap-4 px-4 pt-2.5 text-xs text-secondary" data-legend="">
          {series.map(x => {
            const last = [...x.values].reverse().find(v => v != null) as number | undefined
            return <span key={x.key} className="inline-flex items-center gap-1.5"><i className="inline-block h-[3px] w-4 rounded" style={x.dash ? { background: `repeating-linear-gradient(90deg, ${x.color} 0 4px, transparent 4px 7px)` } : { background: x.color }} />{x.label} <b className="tabular-nums text-foreground">{last == null ? DASH : last.toFixed(2)}</b></span>
          })}
          <span className="ml-auto">净值 · 起点 1.00</span>
        </div>
        <div className="px-2"><NavChart dates={dates} series={series} /></div>
        <div className="grid grid-cols-2 gap-px border-y border-border bg-border sm:grid-cols-4 lg:grid-cols-6">
          {kv.map(([k, v, c]) => <div key={k} className="bg-surface px-3 py-2"><div className="text-[11px] text-secondary">{k}</div><div className={`mt-0.5 text-[13px] font-semibold tabular-nums ${c || 'text-foreground'}`}>{v}</div></div>)}
        </div>
        <table className="w-full text-xs" data-yearly="">
          <thead><tr className="text-secondary"><th className="px-4 py-1.5 text-left font-medium">年份</th>{cols.map(c => <th key={c} className="px-4 py-1.5 text-right font-medium">{c}</th>)}</tr></thead>
          <tbody>{years.map(y => {
            const vals = sel.kind === 'engine' ? [yr[y], spyYearly[y]] : [yr[y], engineYearly[y], spyYearly[y]]
            return <tr key={y} className="border-t border-border/60"><td className="px-4 py-1.5 text-secondary">{y === thisYear ? '本年度' : y}</td>{vals.map((v, i) => <td key={i} className={`px-4 py-1.5 text-right font-semibold tabular-nums ${tone(v)}`}>{pct(v)}</td>)}</tr>
          })}</tbody>
        </table>
      </Card>
      <Card className="flex flex-col">
        <div className="flex items-center px-3 py-2.5"><b className="text-[13px] text-foreground">检查</b><span className="ml-auto text-[11px] text-secondary" data-ckn="">{ok} / {checks.length}</span></div>
        <div className="flex flex-col">
          {checks.map((c, i) => (
            <div key={i} className={`flex items-center gap-2 px-3 py-1.5 text-xs ${c[0] ? '' : 'bg-warning/10'}`}>
              <span className={`grid h-4 w-4 place-items-center rounded-full text-[10px] font-bold ${c[0] ? 'bg-elevated text-secondary' : 'bg-surface text-foreground ring-2 ring-warning'}`}>{c[0] ? '✓' : '!'}</span>
              <span className="text-foreground">{CHECK_LABEL[c[1]] ?? c[1]}</span>
              <span className={`ml-auto tabular-nums ${c[0] ? 'text-secondary' : 'font-semibold text-warning'}`}>{checkValue(c)}</span>
            </div>
          ))}
        </div>
        <div className="mt-2 border-t border-border px-3 py-2.5"><b className="text-[13px] text-foreground">构成</b></div>
        <table className="w-full text-xs">
          <thead><tr className="text-secondary">{legsHead.map((h, i) => <th key={h} className={`px-3 py-1.5 font-medium ${i >= 2 ? 'text-right' : 'text-left'}`}>{h}</th>)}</tr></thead>
          <tbody>{legs.map((r, k) => <tr key={k} className="border-t border-border/60">{r.map((v, i) => <td key={i} className={`px-3 py-1.5 ${i === 0 ? 'text-foreground' : i >= 2 ? 'text-right tabular-nums ' + (v.startsWith('+') ? 'text-bull' : v.startsWith('−') ? 'text-bear' : 'text-foreground') : 'text-secondary'}`}>{v}</td>)}</tr>)}</tbody>
        </table>
        <div className="mt-2 border-t border-border px-3 py-2.5"><b className="text-[13px] text-foreground">最近成交</b></div>
        <TradesTable rows={trades} />
        {actions && <div className="mt-auto flex gap-2 border-t border-border px-3 py-3" data-actions="">{actions}</div>}
      </Card>
    </div>
  )
}

function intakeChecks(f: { screen?: { t?: number | null; corr?: number | null; duplicate?: boolean; backtest?: DiscoverStats }; warmup?: number; direction?: string }): DiscoverCheck[] {
  const sc = f.screen ?? {}
  const bt = sc.backtest ?? {}
  // 方向折算: 声明「值大利多」时 t 必须为正, 「值小利多」时必须为负; 旧实现只看 |t|, t=-3 也打勾
  const signedT = sc.t == null ? null : (f.direction === 'low' ? -sc.t : sc.t)
  return [
    [1, 'quote', '逐字'], [1, 'compile', '通过'], [1, 'period', f.warmup ? `${f.warmup} 日` : '通过'],
    [sc.duplicate ? 0 : 1, 'dedupe', sc.corr == null ? DASH : sc.corr.toFixed(2)],
    [signedT != null && signedT >= 2 ? 1 : 0, 't5', sc.t == null ? DASH : sc.t.toFixed(2)],
    [(bt.n_trades ?? 0) >= 30 ? 1 : 0, 'trades', bt.n_trades ?? null],
  ]
}

// ---------------------------------------------------------------- candidate cards
function CandidateCards({ page, sel, setSel, filter, onDecide }: {
  page: FactorLabPage; sel: Sel; setSel: (s: Sel) => void; filter: string; onDecide: (sig: string, a: 'save' | 'discard') => void
}) {
  const w = page.week!
  const decision = (c: DiscoverCandidate) => page.decisions[`${w.week}|${c.signature}`]?.action
  const list = w.candidates.filter(c => !c.error && c.stats).filter(c => {
    const d = decision(c)
    return filter === 'all' || (filter === 'week' && !d) || (filter === 'saved' && d === 'save') || (filter === 'discarded' && d === 'discard')
  })
  const engine = w.engine
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3" data-cands="">
      {list.map((c, i) => {
        const st = c.stats!
        const ok = (c.checks ?? []).filter(x => x[0]).length
        const active = sel.kind === 'cand' && sel.sig === c.signature
        const d = decision(c)
        return (
          <Card key={c.signature} className={`flex min-h-[178px] min-w-0 cursor-pointer flex-col gap-2.5 p-3.5 ${active ? 'border-accent shadow-[inset_0_0_0_1px_hsl(var(--accent))]' : ''}`} attr={{ 'data-cand': c.signature }}>
            <div onClick={() => setSel({ kind: 'cand', sig: c.signature })} className="flex min-w-0 flex-col gap-2.5">
              <div className="flex items-center gap-2"><Chip>候选 {i + 1}</Chip><span className="truncate text-sm font-semibold text-foreground">{c.name}</span>
                <span className="ml-auto">{d === 'save' ? <Pill>已保存</Pill> : d === 'discard' ? <Pill>已放弃</Pill> : <Pill kind="run">待审</Pill>}</span></div>
              <div className="flex flex-wrap gap-1">{(c.legs ?? []).map(l => <Chip key={l.factor}>{l.label} {l.direction === 'low' ? '低' : '高'}</Chip>)}<Chip>每日前 20</Chip>{c.oos_gate === 'observe' && <span title={OBSERVE_NOTE}><Chip dashed>观察通过</Chip></span>}</div>
              <div className="grid grid-cols-4 gap-1.5">
                <Metric label="年化" value={pct(st.annual_return)} cls={tone(st.annual_return)} />
                <Metric label="夏普" value={num(st.sharpe)} />
                <Metric label="最大回撤" value={pct(st.max_drawdown)} cls={tone(st.max_drawdown)} />
                <Metric label="交易" value={String(st.n_trades ?? DASH)} />
              </div>
            </div>
            <div className="mt-auto flex items-center gap-2 border-t border-border pt-2 text-[11.5px] text-secondary">
              <span>检查</span><b className="text-foreground">{ok} / {(c.checks ?? []).length}</b><Bar checks={c.checks ?? []} />
              {!d && <span className="ml-auto flex gap-1.5"><Btn onClick={() => onDecide(c.signature, 'save')}>保存</Btn><Btn onClick={() => onDecide(c.signature, 'discard')}>放弃</Btn></span>}
            </div>
          </Card>
        )
      })}
      {engine && (
        <Card dashed className={`flex min-h-[178px] min-w-0 cursor-pointer flex-col gap-2.5 p-3.5 ${sel.kind === 'engine' ? 'border-accent shadow-[inset_0_0_0_1px_hsl(var(--accent))]' : ''}`} attr={{ 'data-ref': '' }}>
          <div onClick={() => setSel({ kind: 'engine' })} className="flex min-w-0 flex-col gap-2.5">
            <div className="flex items-center gap-2"><Chip dashed>对照</Chip><span className="truncate text-sm font-semibold text-foreground">当前出卡引擎</span><span className="ml-auto"><Pill>在用</Pill></span></div>
            <div className="flex flex-wrap gap-1">{engine.legs.map(l => <Chip key={l.id}>{l.name}</Chip>)}</div>
            <div className="grid grid-cols-4 gap-1.5">
              <Metric label="年化" value={pct(engine.stats.annual_return)} cls="text-secondary" />
              <Metric label="夏普" value={num(engine.stats.sharpe)} cls="text-secondary" />
              <Metric label="最大回撤" value={pct(engine.stats.max_drawdown)} cls="text-secondary" />
              <Metric label="交易" value={String(engine.stats.n_trades ?? DASH)} cls="text-secondary" />
            </div>
          </div>
          <div className="mt-auto flex items-center gap-2 border-t border-border pt-2 text-[11.5px] text-secondary">
            <span>检查</span><b className="text-foreground">{engine.checks.filter(x => x[0]).length} / {engine.checks.length}</b><Bar checks={engine.checks} />
          </div>
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- intake
function IntakeCards({ page, sel, setSel, filter, onIntake }: {
  page: FactorLabPage; sel: Sel; setSel: (s: Sel) => void; filter: string; onIntake: (id: string, a: 'merge' | 'ignore' | 'restore', formula?: number) => void
}) {
  if (!page.intake) return <div className="p-6 text-center text-xs text-warning" data-intake-error="">读取失败</div>
  const cards = page.intake.cards.filter(c => filter === 'all' || (filter === 'pending' && c.stage === 'pending') || (filter === 'archived' && c.stage !== 'pending'))
  if (!cards.length) return <div className="p-6 text-center text-xs text-muted">暂无</div>
  return (
    <div className="grid grid-cols-1 gap-3 p-3 lg:grid-cols-2" data-intake-cards="">
      {cards.map(c => <IntakeCardView key={c.id} c={c} active={sel.kind === 'intake' && sel.id === c.id} onOpen={i => setSel({ kind: 'intake', id: c.id, formula: i })} onIntake={onIntake} />)}
    </div>
  )
}

function IntakeCardView({ c, active, onOpen, onIntake }: { c: IntakeCard; active: boolean; onOpen: (formula?: number) => void; onIntake: (id: string, a: 'merge' | 'ignore' | 'restore', formula?: number) => void }) {
  // 一条内容最多 6 个公式: 默认指向第一个「不重复」的公式, 并入时带上它的下标
  const pick = Math.max(0, c.formulas.findIndex(x => !x.screen?.duplicate && !x.screen?.error))
  const f = c.formulas[pick]
  const sc = f?.screen ?? {}
  const archived = c.stage !== 'pending'
  const checks = intakeChecks(f ?? {})
  const ok = checks.filter(x => x[0]).length
  const metrics: [string, string, string][] = c.duplicate
    ? [['最像', sc.nearest_label ?? DASH, ''], ['相关', sc.corr == null ? DASH : sc.corr.toFixed(3), tone(sc.corr)], ['方向', f?.direction === 'low' ? '低' : '高', ''], ['预热', f?.warmup ? `${f.warmup} 日` : DASH, '']]
    : [['5日IC', sc.ic == null ? DASH : (sc.ic > 0 ? '+' : '') + sc.ic.toFixed(3), tone(sc.ic)], ['t', sc.t == null ? DASH : sc.t.toFixed(2), ''],
       ['最像', sc.corr == null ? DASH : sc.corr.toFixed(2), ''], ['自选年化', pct(sc.backtest?.annual_return), tone(sc.backtest?.annual_return)]]
  return (
    <Card dashed={archived} className={`flex min-w-0 flex-col gap-2 p-3.5 ${active ? 'border-accent shadow-[inset_0_0_0_1px_hsl(var(--accent))]' : ''}`} attr={{ 'data-intake': c.id, 'data-stage': c.stage }}>
      <div className="flex min-h-[22px] items-center gap-2">
        <Chip dashed={archived}>采集</Chip>
        <span className="truncate text-[11.5px] text-secondary">{c.source_name} · {c.published?.slice(5, 10) ?? DASH} · {c.title}</span>
        <span className="ml-auto shrink-0">{c.stage === 'pending' ? <Pill kind="run">待审</Pill> : c.stage === 'merged' ? <Pill>已并入</Pill> : <Pill>{c.duplicate ? '重复' : (c.reason ?? '已归档')}</Pill>}</span>
      </div>
      <div className={`text-sm font-semibold ${archived ? 'text-secondary' : 'text-foreground'}`}>{f?.name}</div>
      {c.formulas.length > 1 && (
        <div className="flex flex-col gap-0.5 text-[11.5px] text-secondary" data-formulas="">
          {c.formulas.map((x, i) => (
            <button key={i} type="button" onClick={() => onOpen(i)} className={`truncate text-left hover:text-foreground ${i === pick ? 'text-foreground' : ''}`}>
              {i + 1}. {x.name}{x.screen?.duplicate ? ' · 重复' : x.screen?.error ? ' · 快筛失败' : ''}
            </button>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        <Chip dashed={archived}>方向 {f?.direction === 'low' ? '低' : '高'}</Chip>
        {sc.nearest_label && <Chip dashed={archived}>最像 {sc.nearest_label}</Chip>}
        {f?.warmup && <Chip dashed={archived}>预热 {f.warmup} 日</Chip>}
      </div>
      <div className={`min-h-[44px] rounded-r-md border-l-[3px] border-border bg-base px-2.5 py-1.5 text-xs italic ${archived ? 'text-muted' : 'text-secondary'}`}>“{f?.quote}”</div>
      <div title={f?.dsl} className={`truncate rounded-md border border-border bg-base px-2.5 py-1.5 font-mono text-[11.5px] ${archived ? 'text-secondary' : 'text-foreground'}`}>{f?.dsl}</div>
      <div className="grid grid-cols-4 gap-1.5">{metrics.map(([k, v, cl]) => <Metric key={k} label={k} value={v} cls={archived ? 'text-secondary' : cl || 'text-foreground'} />)}</div>
      <div className="mt-auto flex items-center gap-2 border-t border-border pt-2 text-[11.5px] text-secondary">
        <span>检查</span><b className="text-foreground">{ok} / {checks.length}</b><Bar checks={checks} />
        <span className="ml-auto flex gap-1.5">
          {c.stage === 'pending' && <>
            {sc.backtest && <Btn onClick={() => onOpen(pick)} attr={{ 'data-act': 'open' }}>看回测</Btn>}
            <Btn onClick={() => onIntake(c.id, 'ignore')}>忽略</Btn>
            <Btn primary disabled={!!sc.duplicate} onClick={() => onIntake(c.id, 'merge', pick)}>并入因子库</Btn>
          </>}
          {c.stage === 'archived' && <Btn onClick={() => onIntake(c.id, 'restore')}>恢复</Btn>}
          {c.link && <a href={c.link} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-btn border border-border px-2.5 text-xs text-secondary hover:text-foreground">原文 ↗</a>}
        </span>
      </div>
    </Card>
  )
}

// ------------------------------------------------- 我的投递 (0.47.3)
const STAGE_LABEL: Record<string, string> = {
  queued: '排队中', to_extract: '排队中', to_screen: '快筛中', pending: '待审', merged: '已并入', archived: '失败',
}

function SubmissionRow({ s, flash, onRetry, onIntake, onOpen }: {
  s: IntakeSubmission; flash: boolean
  onRetry: () => void; onIntake: (a: 'merge' | 'ignore' | 'restore', formula?: number) => void; onOpen: (formula: number) => void
}) {
  const [open, setOpen] = useState(false)
  const pick = Math.max(0, s.formulas.findIndex(x => !x.screen?.duplicate && !x.screen?.error))
  const f = s.formulas[pick]
  const failed = s.stage === 'archived'
  const ignored = s.reason === '已忽略'
  const label = failed ? (ignored ? '已忽略' : s.reason || '失败') : STAGE_LABEL[s.stage] ?? s.stage
  const kind = s.stage === 'pending' ? 'run' : failed && !ignored ? 'warn' : 'mute'
  const busy = s.stage === 'queued' || s.stage === 'to_extract' || s.stage === 'to_screen'
  return (
    <div className={`border-t border-border/60 px-3.5 py-2 transition-colors ${flash ? 'bg-accent/10' : ''}`}
      data-submission={s.id} data-stage={s.stage}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs">
        <span className="tabular-nums text-secondary">{s.published ? s.published.slice(5, 16).replace('T', ' ') : DASH}</span>
        <span className={`min-w-0 max-w-[38rem] flex-1 truncate ${failed ? 'text-secondary' : 'text-foreground'}`} title={s.title}>{s.title || '未命名'}</span>
        {f && <Chip dashed={failed}>{f.name}</Chip>}
        <Pill kind={kind}>{label}</Pill>
        {!failed && !busy && s.retry_count > 0 && <span className="text-[11px] text-secondary">重试 {s.retry_count}</span>}
        {s.attempts > 1 && <span className="text-[11px] text-secondary">尝试 {s.attempts}</span>}
        <span className="ml-auto flex flex-wrap items-center gap-1.5">
          {failed && !ignored && s.retry_count < 5 && <Btn onClick={onRetry} attr={{ 'data-act': 'retry' }}>重试</Btn>}
          {failed && f && <Btn onClick={() => onIntake('restore')}>恢复</Btn>}
          {s.stage === 'pending' && <>
            {f?.screen?.backtest && <Btn onClick={() => onOpen(pick)} attr={{ 'data-act': 'open' }}>看回测</Btn>}
            <Btn onClick={() => onIntake('ignore')}>忽略</Btn>
            <Btn primary disabled={!!f?.screen?.duplicate} onClick={() => onIntake('merge', pick)}>并入因子库</Btn>
          </>}
          {!!s.model_text && <Btn onClick={() => setOpen(v => !v)} attr={{ 'data-act': 'model' }}>{open ? '收起返回' : '模型返回'}</Btn>}
          {s.link && <a href={s.link} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center rounded-btn border border-border px-2.5 text-xs text-secondary hover:text-foreground">原文 ↗</a>}
        </span>
      </div>
      {f?.dsl && <div title={f.dsl} className="mt-1 truncate font-mono text-[11px] text-secondary">{f.dsl}</div>}
      {open && <pre data-model-text="" className="mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-base p-2 text-[11px] leading-relaxed text-secondary">{s.model_text}</pre>}
    </div>
  )
}

function SubmissionsBlock({ page, flash, onRetry, onIntake, onProcess, onOpen, busy }: {
  page: FactorLabPage; flash: string | null
  onRetry: (id: string) => void; onIntake: (id: string, a: 'merge' | 'ignore' | 'restore', formula?: number) => void
  onProcess: () => void; onOpen: (id: string, formula: number) => void; busy: boolean
}) {
  const list = page.intake?.submissions ?? []
  const u = page.intake?.usage
  const disabled = !!page.intake && page.intake.settings.daily_limit === 0
  const exhausted = !!u && !disabled && u.today >= u.limit
  const waiting = list.filter(s => s.stage === 'queued' || s.stage === 'to_extract').length
  if (!page.intake) return <Card attr={{ 'data-mine': '' }}><div className="p-4 text-center text-xs text-warning">读取失败</div></Card>
  const next = page.intake?.next_tick ? new Date(page.intake.next_tick) : null
  const nextLabel = next && !Number.isNaN(next.getTime())
    ? `${String(next.getHours()).padStart(2, '0')}:${String(next.getMinutes()).padStart(2, '0')}` : null
  return (
    <Card attr={{ 'data-mine': '' }}>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3.5 py-2.5 text-xs">
        <span className="text-[13.5px] font-semibold text-foreground">我的投递</span>
        <span className="text-secondary">{list.length} 条</span>
        {waiting > 0 && (
          <span className="text-secondary" data-waiting={waiting}>
            排队 {waiting} · {nextLabel ? `下次自动 ${nextLabel}` : '等后台唤醒'}
          </span>
        )}
        {waiting > 0 && <Btn onClick={onProcess} disabled={busy} attr={{ 'data-act': 'process' }}>立即处理</Btn>}
        <span className="ml-auto flex flex-wrap items-center gap-2 text-secondary">
          <span>今日模型 <b className="tabular-nums text-foreground">{u?.today ?? 0}</b> / {u?.limit ?? 0}</span>
          {!!u?.retries_today && <span>重试 <b className="tabular-nums text-foreground">{u.retries_today}</b> · 不占额度</span>}
          {disabled && <Pill kind="warn">模型调用已停用 · 每日上限为 0</Pill>}
          {exhausted && <Pill kind="warn">额度用尽 · 明天自动继续</Pill>}
        </span>
      </div>
      {list.length === 0
        ? <div className="p-4 text-center text-xs text-muted">还没有投递 · 右上角「投递」放链接或正文</div>
        : list.map(s => (
          <SubmissionRow key={s.id} s={s} flash={flash === s.id}
            onRetry={() => onRetry(s.id)} onIntake={(a, i) => onIntake(s.id, a, i)} onOpen={i => onOpen(s.id, i)} />
        ))}
    </Card>
  )
}

const KIND_LABEL: Record<IntakeSource['kind'], string> = { person: '人物', site: '网站', aggregator: '聚合', paper: '论文' }
const STATUS_LABEL: Record<IntakeSource['status'], [string, 'run' | 'mute' | 'warn']> = {
  active: ['活跃', 'run'], idle: ['沉寂', 'mute'], failed: ['失败', 'warn'], off: ['关闭', 'mute'], new: ['待抓', 'mute'],
}

function SourcesSheet({ page, onClose }: { page: FactorLabPage; onClose: () => void }) {
  const qc = useQueryClient()
  const it = page.intake!
  const [filter, setFilter] = useState<'all' | IntakeSource['status']>('all')
  const [limit, setLimit] = useState(String(it.settings.daily_limit))
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [kind, setKind] = useState<IntakeSource['kind']>('person')
  const refresh = () => void qc.invalidateQueries({ queryKey: QK.factorLabPage })
  const toggle = useMutation({ mutationFn: (s: IntakeSource) => api.intakeToggleSource(s.id, !s.enabled), onSuccess: refresh })
  const save = useMutation({ mutationFn: () => api.intakeSettings(Number(limit)), onSuccess: () => { refresh(); toast('已保存', 'success') } })
  const add = useMutation({ mutationFn: () => api.intakeAddSource({ name, url, kind }), onSuccess: () => { refresh(); setName(''); setUrl(''); toast('已添加', 'success') } })
  const rows = it.sources.filter(s => filter === 'all' || s.status === filter)
  const count = (st: IntakeSource['status']) => it.sources.filter(s => s.status === st).length
  const sum = (k: 'n30' | 'paywall' | 'llm' | 'formulas') => it.sources.reduce((a, s) => a + (s[k] || 0), 0)
  const zero = (v: number) => (v ? 'text-foreground' : 'text-muted')
  return (
    <Sheet title="监控清单" onClose={onClose} attr={{ 'data-sheet': 'sources' }}>
      <div className="flex flex-col gap-3">
        <Card className="flex flex-wrap items-center gap-3 px-3.5 py-2.5 text-xs">
          <span className="text-secondary">每日上限</span>
          <input value={limit} onChange={e => setLimit(e.target.value.replace(/\D/g, ''))} className="h-7 w-16 rounded-input border border-border bg-surface px-2 tabular-nums" />
          <Btn onClick={() => save.mutate()} disabled={save.isPending}>保存</Btn>
          <span className="text-secondary">今日</span><b className="tabular-nums text-foreground">{it.usage.today} / {it.usage.limit}</b>
          <span className="ml-auto text-secondary">近 30 天</span><b className="tabular-nums text-foreground">{it.usage.days30_calls} 次 · {it.usage.days30_tokens.toLocaleString('en-US')} tokens</b>
        </Card>
        <Card>
          <table className="w-full text-xs">
            <thead><tr className="text-secondary"><th className="px-3 py-1.5 text-left font-medium">模型 · 环节</th><th className="px-3 py-1.5 text-left font-medium">任务</th><th className="px-3 py-1.5 text-right font-medium">调用</th><th className="px-3 py-1.5 text-right font-medium">tokens</th></tr></thead>
            <tbody>
              {(['factor_triage', 'factor_extract'] as const).map(t => (
                <tr key={t} className="border-t border-border/60"><td className="px-3 py-1.5 font-medium text-foreground">{t === 'factor_triage' ? '分拣' : '抽取'}</td><td className="px-3 py-1.5 font-mono text-[11px] text-secondary">{t}</td><td className="px-3 py-1.5 text-right tabular-nums text-foreground">{it.usage.by_task[t] ?? 0}</td><td className="px-3 py-1.5 text-right tabular-nums text-foreground">{(it.usage.tokens_by_task[t] ?? 0).toLocaleString('en-US')}</td></tr>
              ))}
            </tbody>
          </table>
          {it.last_error && <div className="border-t border-border px-3 py-1.5 text-[11px] text-warning">模型错误 · 排队 {it.queued}</div>}
        </Card>
        <div className="flex items-center gap-2">
          <Seg value={filter} onChange={setFilter} attr="src" options={[['all', `全部 ${it.sources.length}`], ['active', `活跃 ${count('active')}`], ['idle', `沉寂 ${count('idle')}`], ['failed', `失败 ${count('failed')}`], ['off', `关闭 ${count('off')}`]]} />
        </div>
        <Card>
          <table className="w-full text-xs" data-src-table="">
            <thead><tr className="text-secondary"><th className="px-3 py-1 text-left font-medium">来源</th><th className="px-3 py-1 text-left font-medium">类型</th><th className="px-3 py-1 text-right font-medium">30天</th><th className="px-3 py-1 text-right font-medium">付费</th><th className="px-3 py-1 text-right font-medium">送模型</th><th className="px-3 py-1 text-right font-medium">公式</th><th className="px-3 py-1 text-left font-medium">状态</th><th className="px-3 py-1 text-left font-medium">开关</th></tr></thead>
            <tbody>
              {rows.map(s => {
                const [label, k] = STATUS_LABEL[s.status]
                return (
                  <tr key={s.id} className={`border-t border-border/60 ${s.enabled ? '' : 'text-muted'}`}>
                    <td className={`px-3 py-1 font-medium ${s.enabled ? 'text-foreground' : 'text-muted'}`}>{s.name}</td>
                    <td className="px-3 py-1 text-secondary">{KIND_LABEL[s.kind]}</td>
                    {(['n30', 'paywall', 'llm', 'formulas'] as const).map(key => <td key={key} className={`px-3 py-1 text-right tabular-nums ${zero(s[key])}`}>{s[key]}</td>)}
                    <td className="px-3 py-1"><Pill kind={k}>{label}</Pill></td>
                    <td className="px-3 py-1">
                      <button type="button" aria-label={s.enabled ? '关闭' : '打开'} onClick={() => toggle.mutate(s)} data-toggle={s.id}
                        className={`relative inline-block h-[17px] w-[30px] rounded-full border ${s.enabled ? 'border-accent bg-accent' : 'border-border bg-elevated'}`}>
                        <span className={`absolute top-[2px] h-[11px] w-[11px] rounded-full ${s.enabled ? 'left-[15px] bg-white' : 'left-[2px] bg-muted'}`} />
                      </button>
                    </td>
                  </tr>
                )
              })}
              <tr className="border-t border-border font-semibold"><td className="px-3 py-1 text-foreground">合计</td><td /><td className="px-3 py-1 text-right tabular-nums">{sum('n30')}</td><td className="px-3 py-1 text-right tabular-nums">{sum('paywall')}</td><td className="px-3 py-1 text-right tabular-nums">{sum('llm')}</td><td className="px-3 py-1 text-right tabular-nums">{sum('formulas')}</td><td /><td /></tr>
            </tbody>
          </table>
        </Card>
        <div className="flex flex-wrap gap-2">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="名称" className="h-7 w-28 rounded-input border border-border bg-surface px-2 text-xs" />
          <input value={url} onChange={e => setUrl(e.target.value)} placeholder="RSS / Atom / arXiv API" className="h-7 min-w-0 flex-1 rounded-input border border-border bg-surface px-2 text-xs" />
          <Seg value={kind} onChange={setKind} options={[['person', '人物'], ['site', '网站'], ['aggregator', '聚合'], ['paper', '论文']]} />
          <Btn primary disabled={!name || !url || add.isPending} onClick={() => add.mutate()}>添加来源</Btn>
        </div>
      </div>
    </Sheet>
  )
}

function SubmitSheet({ onClose, onDone }: { onClose: () => void; onDone: (id: string, ahead: number) => void }) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'link' | 'text'>('link')
  const [url, setUrl] = useState('')
  const [text, setText] = useState('')
  const sub = useMutation({
    mutationFn: () => api.intakeSubmit(mode === 'link' ? { url } : { text }),
    onSuccess: (d) => { void qc.invalidateQueries({ queryKey: QK.factorLabPage }); onClose(); onDone(d.id, d.ahead ?? 0) },
  })
  return (
    <Sheet title="投递" onClose={onClose} attr={{ 'data-sheet': 'submit' }}
      footer={<><Chip>X</Chip><Chip>TradingView</Chip><Chip>SSRN</Chip><span className="ml-auto" /><Btn primary disabled={sub.isPending || (mode === 'link' ? !url : !text)} onClick={() => sub.mutate()}>提交</Btn></>}>
      <div className="flex flex-col gap-3">
        <Seg value={mode} onChange={setMode} options={[['link', '链接'], ['text', '正文 / Pine / 公式']]} />
        {mode === 'link'
          ? <input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://" className="h-8 rounded-input border border-border bg-surface px-2 text-xs" />
          : <textarea value={text} onChange={e => setText(e.target.value)} className="min-h-[220px] rounded-input border border-border bg-base p-2 text-xs" />}
      </div>
    </Sheet>
  )
}

// ---------------------------------------------------------------- page
export function FactorDiscover({ onGo }: { onGo: (tab: 'inspect' | 'mining') => void }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: QK.factorLabPage, queryFn: () => api.factorLabPage(), refetchInterval: 60_000 })
  const page = q.data
  const [sel, setSel] = useState<Sel | null>(null)
  const [candFilter, setCandFilter] = useState<'week' | 'saved' | 'discarded' | 'all'>('week')
  const [intakeFilter, setIntakeFilter] = useState<'all' | 'pending' | 'archived'>('all')
  const [sheet, setSheet] = useState<'sources' | 'submit' | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const flashTimer = useRef<number | undefined>(undefined)
  const refresh = () => void qc.invalidateQueries({ queryKey: QK.factorLabPage })
  const onSubmitted = (id: string, ahead: number) => {
    toast(ahead > 0 ? `已收到 · 前面还有 ${ahead} 条投递` : '已收到 · 下一次抽取就轮到它', 'success')
    setFlash(id)
    window.clearTimeout(flashTimer.current)
    flashTimer.current = window.setTimeout(() => setFlash(null), 6000)
    refresh()
    requestAnimationFrame(() => document.getElementById('fl-mine')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
  const decide = useMutation({
    mutationFn: ({ sig, action }: { sig: string; action: 'save' | 'discard' }) => api.factorLabDecide(page!.week!.week, sig, action),
    // 只在后端真的建出候选 (返回 candidate_id) 后提示「已保存」; 失败时 409 由请求层报错, 这里不再盖一个成功提示
    onSuccess: (d, v) => {
      if (v.action === 'save') toast(d.candidate_id ? '已保存 · 候选方案' : '已记录 · 候选方案未建出', d.candidate_id ? 'success' : 'error')
      else toast(d.removed_candidate_id ? '已放弃 · 候选方案已撤销' : '已放弃', 'success')
      refresh(); void qc.invalidateQueries({ queryKey: QK.researchCandidates })
    },
  })
  const intake = useMutation({
    mutationFn: ({ id, action, formula = 0 }: { id: string; action: 'merge' | 'ignore' | 'restore'; formula?: number }) =>
      action === 'merge' ? api.intakeMerge(id, formula) : action === 'ignore' ? api.intakeIgnore(id) : api.intakeRestore(id),
    onSuccess: (_d, v) => { toast(v.action === 'merge' ? '已并入因子库 · 观察' : v.action === 'ignore' ? '已忽略' : '已恢复', 'success'); refresh(); void qc.invalidateQueries({ queryKey: QK.factorsLibrary }); void qc.invalidateQueries({ queryKey: QK.factorColumns }) },
  })
  const runIntake = useMutation({ mutationFn: () => api.intakeRun(), onSuccess: () => toast('已开始 · 采集', 'success') })
  const retryIntake = useMutation({
    mutationFn: (id: string) => api.intakeRetry(id),
    onSuccess: (d) => { toast(`已重试 · 第 ${d.retry_count} 次`, 'success'); refresh() },
  })
  const processIntake = useMutation({
    mutationFn: () => api.intakeProcess(),
    onSuccess: () => { toast('已开始 · 处理投递', 'success'); setTimeout(refresh, 1500); refresh() },
  })

  // 选中项在新一次轮询里不存在 (换周 / 采集条目被裁剪) 时回落到默认项, 不渲染空详情
  const effectiveSel: Sel | null = useMemo(() => {
    const week = page?.week
    const exists = (s: Sel) => s.kind === 'engine' ? !!week?.engine
      : s.kind === 'cand' ? !!week?.candidates.some(c => c.signature === s.sig && !c.error && c.stats)
        : !!(page && intakeItem(page, s.id)?.formulas.some(f => f.screen?.backtest))
    if (sel && exists(sel)) return sel
    const first = week?.candidates.find(c => !c.error && c.stats)
    if (first) return { kind: 'cand', sig: first.signature }
    return week?.engine ? { kind: 'engine' } : null
  }, [sel, page])

  const openSel = (s: Sel) => { setSel(s); requestAnimationFrame(() => document.getElementById('fl-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' })) }

  if (q.isLoading) return <div className="p-8 text-center text-xs text-muted">加载中</div>
  if (!page) return <div className="p-8 text-center text-xs text-danger">读取失败</div>
  const w = page.week
  const it = page.intake
  const onStep = (i: number) => {
    if (i === 0) document.getElementById('fl-intake')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    else if (i === 1) onGo('inspect')
    else if (i < 4) onGo('mining')
    else document.getElementById('fl-cands')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const counts = w ? {
    week: w.candidates.filter(c => !c.error && !page.decisions[`${w.week}|${c.signature}`]).length,
    saved: w.candidates.filter(c => page.decisions[`${w.week}|${c.signature}`]?.action === 'save').length,
    discarded: w.candidates.filter(c => page.decisions[`${w.week}|${c.signature}`]?.action === 'discard').length,
    all: w.candidates.filter(c => !c.error).length,
  } : { week: 0, saved: 0, discarded: 0, all: 0 }
  const refs = w?.engine ? 1 : 0
  const ic = it?.cards ?? []
  const intakeCounts = { all: ic.length, pending: ic.filter(c => c.stage === 'pending').length, archived: ic.filter(c => c.stage !== 'pending').length }
  const funnel = it?.funnel
  return (
    <div className="flex flex-col gap-0 pb-6" data-discover="">
      <FlowBar page={page} onStep={onStep} />

      <div id="fl-cands" className="mb-2.5 mt-4 flex scroll-mt-24 flex-wrap items-center gap-2">
        <h2 className="text-[13.5px] font-semibold text-foreground">候选策略</h2>
        {w && <span className="text-xs text-secondary" data-ccount="">{w.week.replace(/^\d{4}-/, '')} · {counts.all} 候选{refs ? ` · ${refs} 对照` : ''} · 自选 {w.symbols} · {w.window[0].slice(0, 7)} → {w.window[1].slice(0, 7)}</span>}
        <div className="ml-auto"><Seg value={candFilter} onChange={setCandFilter} attr="cands" options={[['week', `本周 ${counts.week}`], ['saved', `已保存 ${counts.saved}`], ['discarded', `已放弃 ${counts.discarded}`], ['all', `全部 ${counts.all}`]]} /></div>
      </div>
      {w ? <CandidateCards page={page} sel={effectiveSel ?? { kind: 'engine' }} setSel={openSel} filter={candFilter} onDecide={(sig, action) => decide.mutate({ sig, action })} />
        : <Card className="p-6 text-center text-xs text-muted">本周未生成 · 周五 17:40 ET</Card>}

      {effectiveSel && (w || effectiveSel.kind === 'intake') && <Detail key={selectionKey(effectiveSel)} page={page} sel={effectiveSel} onDecide={(sig, action) => decide.mutate({ sig, action })} onIntake={(id, action, formula) => intake.mutate({ id, action, formula })} />}

      <div id="fl-mine" className="mb-2.5 mt-5 flex scroll-mt-24 flex-wrap items-center gap-2">
        <h2 className="text-[13.5px] font-semibold text-foreground">我的投递</h2>
        <span className="text-xs text-secondary">只做外部文章 / Pine / 公式摘录入口 · 失败可重试</span>
      </div>
      <SubmissionsBlock page={page} flash={flash} busy={processIntake.isPending || page.running}
        onProcess={() => processIntake.mutate()}
        onOpen={(id, formula) => openSel({ kind: 'intake', id, formula })}
        onRetry={id => retryIntake.mutate(id)} onIntake={(id, action, formula) => intake.mutate({ id, action, formula })} />

      <div id="fl-intake" className="mb-2.5 mt-5 flex scroll-mt-24 flex-wrap items-center gap-2">
        <h2 className="text-[13.5px] font-semibold text-foreground">采集</h2>
        <span className="text-xs text-secondary">{it ? `近 30 天 · ${it.sources.filter(s => s.enabled).length} 源 · ${funnel?.fetched ?? 0} 篇` : '读取失败'}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Seg value={intakeFilter} onChange={setIntakeFilter} attr="intake" options={[['all', `全部 ${intakeCounts.all}`], ['pending', `待审 ${intakeCounts.pending}`], ['archived', `已归档 ${intakeCounts.archived}`]]} />
          <Btn onClick={() => runIntake.mutate()} disabled={runIntake.isPending}>立即采集</Btn>
          <Btn onClick={() => setSheet('submit')}>投递</Btn>
          <Btn onClick={() => setSheet('sources')} attr={{ 'data-open': 'sources' }}>监控清单 {(it?.sources ?? []).length}</Btn>
        </div>
      </div>
      <Card>
        <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-3.5 py-2.5 text-xs text-secondary" data-funnel="">
          {!it && <span className="text-warning">采集读取失败{page.errors.intake ? ` · ${page.errors.intake}` : ''}</span>}
          {it && ([['抓取', funnel?.fetched], ['送模型', funnel?.llm], ['美股个股', funnel?.us_stock], ['定义', funnel?.defined], ['公式', funnel?.formulas], ['待审', funnel?.pending]] as [string, number | undefined][]).map(([k, v], i, arr) => (
            <span key={k} className="inline-flex items-center gap-1.5">
              <span className={i === arr.length - 1 ? 'text-accent' : ''}>{k}<b className={`ml-1 text-sm tabular-nums ${i === arr.length - 1 ? 'text-accent' : 'text-foreground'}`}>{v ?? 0}</b></span>
              {i < arr.length - 1 && <span className="text-muted">›</span>}
            </span>
          ))}
          {it?.last_error && <span className="ml-auto"><Pill kind="warn">模型不可用 · 排队 {it.queued}</Pill></span>}
        </div>
        <IntakeCards page={page} sel={effectiveSel ?? { kind: 'engine' }} setSel={openSel} filter={intakeFilter} onIntake={(id, action, formula) => intake.mutate({ id, action, formula })} />
      </Card>

      {page.weeks.length > 0 && <>
        <div className="mb-2.5 mt-5"><h2 className="text-[13.5px] font-semibold text-foreground">近 4 周</h2></div>
        <Card>
          <table className="w-full text-xs" data-weeks="">
            <thead><tr className="text-secondary"><th className="px-3 py-1.5" />{page.weeks.map(x => <th key={x.week} className="px-3 py-1.5 text-right font-medium">{x.week.replace(/^\d{4}-/, '')} · {x.data_as_of?.slice(5)}</th>)}</tr></thead>
            <tbody>
              {([['筛因子', (x: FactorLabPage['weeks'][number]) => `${x.funnel?.factors_selected ?? DASH} / ${x.funnel?.factors_total ?? DASH}`],
                 ['组合', (x: FactorLabPage['weeks'][number]) => String(x.funnel?.combos ?? DASH)],
                 ['有效折', (x: FactorLabPage['weeks'][number]) => String(x.funnel?.valid_folds ?? DASH)],
                 ['候选', (x: FactorLabPage['weeks'][number]) => String(x.candidates)]] as [string, (x: FactorLabPage['weeks'][number]) => string][]).map(([label, fn]) => (
                <tr key={label} className="border-t border-border/60"><td className="px-3 py-1.5 text-secondary">{label}</td>{page.weeks.map(x => { const v = fn(x); return <td key={x.week} className={`px-3 py-1.5 text-right tabular-nums ${v === '0' ? 'text-muted' : 'text-foreground'}`}>{v}</td> })}</tr>
              ))}
            </tbody>
          </table>
        </Card>
      </>}

      {sheet === 'sources' && it && <SourcesSheet page={page} onClose={() => setSheet(null)} />}
      {sheet === 'submit' && <SubmitSheet onClose={() => setSheet(null)} onDone={onSubmitted} />}
    </div>
  )
}
