/** 今日多空 (0.51.0, 11-05 产品决策「显然档 · 多空都做」): 盘前自选横截面排名 → 看多 5 / 看空 5 + 前向结算。
 * 只展示: 不进交易环 / 不写台账 / 不推送。后端段 ls_rank (契约 ls-rank-v1, services/ls_rank.py)。
 * 版式: 两侧同列模板 + 固定行高 (5 行等高); 每侧 >= 330px 并排, 不够上下叠; 列数随每侧宽度分档; 结算条 6 格整行不留空轨。
 * 文案只写结果 (名词 + 数字), 不写说明句。 */
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import type { LsRankPayload, LsRankRow, LsSettleSeries } from '@/lib/api'
import { Sheet } from '@/components/opportunities/Sheet'
import { p2, weekday } from '@/components/opportunities/terms'

type Side = 'long' | 'short'
const SIDE_ZH: Record<Side, string> = { long: '看多', short: '看空' }
const TIMING_ZH: Record<string, string> = { bmo: '盘前', amc: '盘后' }
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const sg = (v: number) => (v > 0 ? '+' : v < 0 ? '−' : '')
const sgnCls = (v?: number | null) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')
const num2 = (v?: number | null) => (fin(v) ? `${sg(v)}${Math.abs(v).toFixed(2)}` : '—')
const atrS = (v?: number | null) => (fin(v) ? `${num2(v)} ATR` : '—')
const usdS = (v?: number | null) => (fin(v) ? `${sg(Math.round(v))}$${Math.abs(Math.round(v)).toLocaleString('en-US')}` : '—')
const usd0 = (v: number) => `$${Math.round(v).toLocaleString('en-US')}`
const r1 = (v: number) => Math.round(v * 10) / 10
const pc1 = (v?: number | null) => (fin(v) ? `${sg(r1(v))}${Math.abs(r1(v)).toFixed(1)}%` : '—')
const sgnCls1 = (v?: number | null) => sgnCls(fin(v) ? r1(v) : v)
const pctW = (v?: number | null) => (fin(v) ? `${Math.round(v * 100)}%` : '—')
/** 1 ATR 折多少美元 (= 单笔风险 ÷ 止损 ATR 数); 风险读不到 → null (只显示 ATR) */
const atrUsd = (d: Pick<LsRankPayload, 'risk_usd' | 'stop_atr'>) => (fin(d.risk_usd) && d.stop_atr > 0 ? d.risk_usd / d.stop_atr : null)
const usdOf = (r: number | null | undefined, u: number | null) => (fin(r) && u !== null ? usdS(r * u) : '—')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')

/** 名次 → 「前 1%」/「后 14%」(pct = 分数百分位, 100 = 最强) */
export function rankTag(r: Pick<LsRankRow, 'pct'>, side: Side | null): string {
  const s = side ?? (r.pct >= 50 ? 'long' : 'short')
  return s === 'long' ? `前 ${Math.max(1, Math.round(100 - r.pct))}%` : `后 ${Math.max(1, Math.round(r.pct))}%`
}

/** 持有期内的财报 → 「财报 今日盘后」/「财报 周五盘前」/「财报 11-20 盘后」; 持有期外不标 */
export function earnTag(e: LsRankRow['earnings'], day: string, until: string): string | null {
  if (!e || e.date < day || e.date > until) return null
  const t = TIMING_ZH[e.timing ?? ''] ?? ''
  if (e.date === day) return `财报 今日${t}`
  const diff = (Date.parse(`${e.date}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86_400_000
  return diff <= 6 ? `财报 ${weekday(e.date)}${t}` : `财报 ${md(e.date)} ${t}`.trim()
}

function Spark({ xs }: { xs?: number[] | null }) {
  if (!xs || xs.length < 2) return <span className="text-[12px] text-muted">—</span>
  const w = 46, h = 20, lo = Math.min(...xs), hi = Math.max(...xs), k = hi > lo ? (h - 4) / (hi - lo) : 0
  const pts = xs.map((v, i) => `${((i / (xs.length - 1)) * (w - 2) + 1).toFixed(1)},${(h - 2 - (v - lo) * k).toFixed(1)}`).join(' ')
  const chg = (xs[xs.length - 1] / xs[0] - 1) * 100
  return (
    <svg data-ls-spark width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`20日 ${pc1(chg)}`} className={`block ${chg >= 0 ? 'text-bull' : 'text-bear'}`}>
      <title>{`20日 ${pc1(chg)}`}</title>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function Chip({ children, warn, ...rest }: { children: ReactNode; warn?: boolean } & Record<`data-${string}`, string | undefined>) {
  return (
    <span {...rest} className={`shrink-0 whitespace-nowrap rounded-[4px] border px-[5px] text-[11px] font-medium leading-[18px] ${warn
      ? 'border-warning/50 bg-warning/10 text-warning'
      : 'border-border bg-elevated text-secondary'}`}>{children}</span>
  )
}

const BTN = 'whitespace-nowrap rounded-btn border border-border px-2.5 py-1 text-[12.5px] font-medium text-secondary hover:bg-elevated hover:text-foreground max-md:py-1.5'

// 列随每侧实际宽度分档 (两侧同宽 → 同档 → 列对齐)。文本在左 (票 + 标记同一格), 数字列右对齐贴行右缘:
// 9 = 序 · 票标记 · 20日 · 排名 · 盘前 · 参考价 · 止损 · 股数; 8 去 20日; 7 标记收成「财 / +n」; 6 再去参考价。
// 门槛 = 该档最小列宽之和 + 列间距 4px × (列数 − 1) + 内边距 24px; 富余按 fr 摊开, 票标记格权重最大。
type Tier = 6 | 7 | 8 | 9
const TIER_MIN: [number, Tier][] = [[538, 9], [486, 8], [416, 7]]
const TPL: Record<Tier, string> = {
  6: '14px minmax(96px,1.6fr) minmax(50px,1fr) minmax(52px,1fr) minmax(58px,1.1fr) minmax(36px,0.8fr)',
  7: '14px minmax(100px,1.6fr) minmax(50px,1fr) minmax(52px,1fr) minmax(58px,1.1fr) minmax(58px,1.1fr) minmax(36px,0.8fr)',
  8: '14px minmax(170px,2.4fr) minmax(50px,0.8fr) minmax(52px,0.9fr) minmax(58px,1fr) minmax(58px,1fr) minmax(36px,0.7fr)',
  9: '14px minmax(170px,2.4fr) 48px minmax(50px,0.8fr) minmax(52px,0.9fr) minmax(58px,1fr) minmax(58px,1fr) minmax(36px,0.7fr)',
}
const tierOf = (w: number): Tier => TIER_MIN.find(([m]) => w >= m)?.[1] ?? 6

function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setW(el.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

const ROW = 'grid items-center gap-x-1 px-3'

/** 标记: 持有期内财报在前, 再是策略标签; 放得下几个放几个 (按隐形量尺的自然宽度算), 其余记 +n;
 * 一个完整标签都放不下 → 紧凑形「财」+「+n」。不截断, 完整列表在 title。 */
const MORE_W = 24
function Marks({ earn, tags }: { earn: string | null; tags: string[] }) {
  const all = [...(earn ? [earn] : []), ...tags]
  const box = useRef<HTMLSpanElement>(null)
  const ruler = useRef<HTMLSpanElement>(null)
  const [fit, setFit] = useState(all.length)
  const key = all.join('|')
  useLayoutEffect(() => {
    const el = box.current, rl = ruler.current
    if (!el || !rl) return
    const measure = () => {
      const W = el.clientWidth
      const ws = [...rl.children].map((c) => (c as HTMLElement).offsetWidth)
      let n = 0
      for (let k = ws.length; k >= 1; k--) {
        const need = ws.slice(0, k).reduce((x, y) => x + y, 0) + 4 * (k - 1) + (k < ws.length ? 4 + MORE_W : 0)
        if (need <= W) { n = k; break }
      }
      setFit(n)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [key])
  if (all.length === 0) return null
  const isEarn = (i: number) => !!earn && i === 0
  const rest = fit === 0 ? all.length - (earn ? 1 : 0) : all.length - fit
  return (
    <span ref={box} data-ls-marks title={all.join(' · ')} className="relative ml-1 mr-2.5 flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
      <span ref={ruler} aria-hidden className="pointer-events-none invisible absolute left-0 top-0 flex gap-1">
        {all.map((t, i) => <Chip key={t} warn={isEarn(i)}>{t}</Chip>)}
      </span>
      {fit === 0
        ? earn && <i data-ls-mark="earn-dot" className={TAG_EARN}>财</i>
        : all.slice(0, fit).map((t, i) => <Chip key={t} warn={isEarn(i)} data-ls-mark={isEarn(i) ? 'earn' : 'tag'}>{t}</Chip>)}
      {rest > 0 && <span data-ls-more className="shrink-0 rounded-[4px] border border-border px-1 text-[11px] font-medium leading-4 tabular-nums text-secondary">{`+${rest}`}</span>}
    </span>
  )
}

/** 排名: 方向写在列头 (排名前 / 排名后), 格内只放百分数 */
const rankPct = (r: Pick<LsRankRow, 'pct'>, side: Side) => rankTag(r, side).slice(2)

const TAG_EARN = 'shrink-0 rounded-[3px] bg-warning/15 px-[3px] text-[10.5px] font-semibold not-italic leading-4 text-warning'

function LsRow({ r, i, side, d, tier, onOpen }: { r: LsRankRow; i: number; side: Side; d: LsRankPayload; tier: Tier; onOpen: (s: string) => void }) {
  const earn = earnTag(r.earnings, d.day, d.hold_until)
  return (
    <div role="button" tabIndex={0} data-ls-row={r.symbol} data-side={side} onClick={() => onOpen(r.symbol)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(r.symbol) } }}
      style={{ gridTemplateColumns: TPL[tier] }}
      className={`${ROW} h-9 cursor-pointer border-t border-border/70 text-[13px] leading-5 tabular-nums hover:bg-elevated max-md:h-11`}>
      <span className="text-[12px] text-muted">{i + 1}</span>
      <span data-ls-name className="flex min-w-0 items-center gap-1">
        <b className={`relative shrink-0 text-[14px] font-bold text-foreground ${r.is_new ? 'mr-2.5' : 'mr-1'}`}>
          {r.symbol}
          {r.is_new && <i data-ls-new title="较昨日新进" className="absolute -right-[8px] top-[1px] h-[6px] w-[6px] rounded-full bg-accent dark:bg-blue-400" />}
        </b>
        <Marks earn={earn} tags={tier >= 8 ? r.tags : []} />
      </span>
      {tier >= 9 && <span className="flex justify-end"><Spark xs={r.spark} /></span>}
      <span className="text-right text-secondary">{rankPct(r, side)}</span>
      <span className={`whitespace-nowrap text-right font-semibold ${sgnCls1(r.pre_pct)}`}>{pc1(r.pre_pct)}</span>
      {tier !== 6 && <span className="text-right text-foreground">{p2(r.ref)}</span>}
      <span className="text-right text-foreground">{p2(r.stop)}</span>
      <span className="text-right text-foreground">{r.qty ?? '—'}</span>
    </div>
  )
}

function SidePanel({ side, rows, d, onOpen }: { side: Side; rows: LsRankRow[]; d: LsRankPayload; onOpen: (s: string) => void }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const tier = tierOf(w)
  const bt = d.backtest[side]
  const notional = rows.reduce((s, r) => s + (fin(r.qty) && fin(r.ref) ? r.qty * r.ref : 0), 0)
  return (
    <div ref={ref} data-ls-side={side} data-tier={tier} className="min-w-0 bg-surface pb-1">
      <div className="flex items-baseline gap-2 px-3 pb-1.5 pt-2.5">
        <h3 className={`text-[14px] font-semibold ${side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[side]}</h3>
        <span className="text-[13px] font-semibold tabular-nums text-secondary">{rows.length}</span>
        {tier >= 7 && <span data-ls-notional className="whitespace-nowrap text-[12px] tabular-nums text-muted">{`名义 ${usd0(notional)}`}</span>}
        <span className="ml-auto min-w-0 truncate whitespace-nowrap text-[12px] tabular-nums text-muted">{`回放 ${atrS(bt.mean_atr)}/笔 · 随机 ${num2(bt.rand)}`}</span>
      </div>
      <div data-ls-colhead style={{ gridTemplateColumns: TPL[tier] }} className={`${ROW} h-7 border-t border-border/70 text-[11.5px] text-muted`}>
        <span>#</span><span>{tier >= 8 ? '票 · 标记' : '票'}</span>{tier >= 9 && <span className="text-right">20日</span>}
        <span className="whitespace-nowrap text-right">{side === 'long' ? '排名前' : '排名后'}</span><span className="text-right">盘前</span>
        {tier !== 6 && <span className="text-right" title="盘前最新价 · 开盘后为开盘价">参考价</span>}
        <span className="text-right" title={`参考价 ${side === 'long' ? '−' : '+'} ${d.stop_atr} ATR`}>止损</span>
        <span className="text-right" title={fin(d.risk_usd) ? `$${Math.round(d.risk_usd)} ÷ ${d.stop_atr} ATR` : undefined}>股数</span>
      </div>
      {rows.map((r, i) => <LsRow key={r.symbol} r={r} i={i} side={side} d={d} tier={tier} onOpen={onOpen} />)}
    </div>
  )
}

function Stat({ k, v, unit, sub, cls, bar, ...rest }: { k: string; v: string; unit?: string; sub?: string; cls?: string; bar?: number }
  & Record<`data-${string}`, string | undefined>) {
  return (
    <div {...rest} className="min-w-0 bg-surface px-3 py-1.5">
      <div className="h-4 truncate text-[11.5px] leading-4 text-secondary">{k}</div>
      <div className="flex h-6 items-baseline gap-1 whitespace-nowrap leading-6">
        <b className={`text-[16px] font-semibold tabular-nums ${cls ?? 'text-foreground'}`}>{v}</b>
        {unit && <span className="text-[11.5px] text-muted">{unit}</span>}
      </div>
      {bar !== undefined
        ? <div className="flex h-4 items-center"><div className="h-1 w-full overflow-hidden rounded-sm bg-zinc-300 dark:bg-zinc-700"><i className="block h-full bg-accent" style={{ width: `${Math.round(Math.min(Math.max(bar, 0), 1) * 100)}%` }} /></div></div>
        : <div className="h-4 truncate text-[11.5px] leading-4 tabular-nums text-muted">{sub ?? '—'}</div>}
    </div>
  )
}

function settleCells(d: LsRankPayload): { k: string; v: string; unit?: string; sub?: string; cls?: string; bar?: number; key: string }[] {
  const s = d.settle
  const t = s.days > 0 ? s.total : null
  const u = atrUsd(d)
  const a = (v?: number | null) => ({ v: num2(v), unit: fin(v) ? 'ATR' : undefined, cls: sgnCls(v) })
  return [
    { key: 'progress', k: '前向结算', v: `${s.days}/${s.check_days}`, unit: '日', bar: s.days / s.check_days },
    { key: 'long', k: '看多每笔', ...a(t?.long), sub: t ? `${usdOf(t.long, u)} · 胜率 ${pctW(t.win_long)}` : undefined },
    { key: 'edge', k: '看多超额', ...a(t?.long_minus_all), sub: t ? `自选全体 ${num2(t.all)} · 判据 ≥0` : undefined },
    { key: 'short', k: '看空每笔', ...a(t?.short), sub: t ? `${usdOf(t.short, u)} · 胜率 ${pctW(t.win_short)}` : undefined },
    { key: 'pair', k: '多空合计', ...a(t?.pair), sub: t ? `${usdOf(t.pair, u)} · 日胜率 ${pctW(t.win_pair)}` : undefined },
    { key: 'rand', k: '随机多空', ...a(t?.rand_pair), sub: t ? `多 ${num2(t.rand_long)} · 空 ${num2(t.rand_short)}` : undefined },
  ]
}

// ── 全部 47 ──
// 跟榜口径: 前 5 进 · 掉出前 10 换 → 6-10 名 = 保留区
const BOOK_ZH: Record<string, string> = { keep_long: '保留多', keep_short: '保留空', out_long: '换出多', out_short: '换出空' }
function AllSheet({ d, onOpen, onClose }: { d: LsRankPayload; onOpen: (s: string) => void; onClose: () => void }) {
  const head: [string, string][] = [['#', 'w-7 text-right'], ['票', 'pl-3 text-left'], ['排名', 'text-right'], ['较昨日', 'text-right'], ['盘前', 'text-right'],
    ['收盘', 'text-right'], ['ATR', 'text-right'], ['财报', 'text-right'], ['名单', 'text-right']]
  return (
    <Sheet attr={{ 'data-ls-all': '' }} onClose={onClose} title={`全部 ${d.all.length} · ${md(d.day)} 名单`}>
      <table className="mb-2 w-full text-[13px] tabular-nums">
        <thead className="sticky -top-4 z-[1]"><tr className="h-8 text-[12px] text-muted">
          {head.map(([h, c]) => <th key={h} className={`bg-surface px-1.5 font-medium shadow-[inset_0_-1px_0_0_hsl(var(--border))] ${c}`}>{h}</th>)}
        </tr></thead>
        <tbody>
          {d.all.map((r) => {
            const dl = fin(r.prev_rank) ? r.prev_rank - r.rank : null
            const earn = earnTag(r.earnings, d.day, d.hold_until)
            const dim = r.young
            const tone = r.side === 'long' ? 'bg-bull/[0.05] shadow-[inset_3px_0_0_0_hsl(var(--bull))]' : r.side === 'short' ? 'bg-bear/[0.05] shadow-[inset_3px_0_0_0_hsl(var(--bear))]' : ''
            const book = r.side ? SIDE_ZH[r.side] : r.young ? '上市不足' : BOOK_ZH[r.book ?? ''] ?? '—'
            const bookCls = r.side === 'long' ? 'text-bull' : r.side === 'short' ? 'text-bear' : BOOK_ZH[r.book ?? ''] && !r.young ? 'text-secondary' : 'text-muted'
            return (
              <tr key={r.symbol} data-ls-all-row={r.symbol} onClick={() => onOpen(r.symbol)}
                className={`h-8 cursor-pointer border-t border-border/70 first:border-t-0 hover:bg-elevated ${tone}`}>
                <td className="w-7 px-1.5 text-right text-[12px] text-muted">{r.rank}</td>
                <td className={`pl-3 pr-1.5 text-left font-bold ${dim ? 'text-muted' : 'text-foreground'}`}>{r.symbol}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-secondary'}`}>{rankTag(r, r.side)}</td>
                <td className={`px-1.5 text-right ${dl ? 'text-secondary' : 'text-muted'}`}>{dl === null ? '—' : dl > 0 ? `↑${dl}` : dl < 0 ? `↓${-dl}` : '—'}</td>
                <td className={`px-1.5 text-right font-semibold ${dim ? 'text-muted' : sgnCls1(r.pre_pct)}`}>{pc1(r.pre_pct)}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-foreground'}`}>{p2(r.close)}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-foreground'}`}>{p2(r.atr)}</td>
                <td className={`whitespace-nowrap px-1.5 text-right text-[12px] ${earn ? 'font-semibold text-warning' : 'text-muted'}`}>{earn ? earn.replace('财报 ', '') : '—'}</td>
                <td className={`whitespace-nowrap px-1.5 text-right text-[12px] font-semibold ${bookCls}`}>{book}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Sheet>
  )
}

// ── 结算明细 ──
const WEEK_ROWS: [string, (w: LsSettleSeries) => number | null][] = [
  ['看多', (w) => w.long], ['看空', (w) => w.short], ['多空合计', (w) => w.pair], ['随机多空', (w) => w.rand_pair],
  ['自选全体', (w) => w.all], ['看多−全体', (w) => w.long_minus_all],
]

function SettleSheet({ d, onOpen, onClose }: { d: LsRankPayload; onOpen: (s: string) => void; onClose: () => void }) {
  const s = d.settle
  const u = atrUsd(d)
  const cols = [...s.weeks.map((w) => ({ key: w.week, label: w.week, w })), ...(s.total ? [{ key: 'total', label: '合计', w: s.total }] : [])]
  const last = s.last
  const H3 = ({ children }: { children: ReactNode }) => <h3 className="mb-2 mt-5 text-[13px] font-semibold text-foreground first:mt-0">{children}</h3>
  return (
    <Sheet attr={{ 'data-ls-settle': '' }} onClose={onClose} title={`结算 · 前向 ${s.days} 份${s.first ? ` · ${md(s.first)} 起` : ''}`}>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border sm:grid-cols-3">
        {settleCells(d).map(({ key, ...c }) => <Stat key={key} data-ls-sheet-stat={key} {...c} />)}
      </div>
      {cols.length > 0 && (
        <>
          <H3>周度 · ATR</H3>
          <div className="overflow-x-auto rounded-card border border-border">
            <table data-ls-weeks className="w-full min-w-[480px] text-[13px] tabular-nums">
              <thead><tr className="h-8 bg-elevated text-[12px] text-muted">
                <th className="px-3 text-left font-medium">周</th>
                {cols.map((c) => <th key={c.key} className={`px-3 text-right font-medium ${c.key === 'total' ? 'border-l border-border/70 text-secondary' : ''}`}>{c.label}</th>)}
              </tr></thead>
              <tbody>
                <tr className="h-8 border-t border-border/70">
                  <td className="px-3 text-left text-secondary">名单数</td>
                  {cols.map((c) => <td key={c.key} className={`px-3 text-right text-foreground ${c.key === 'total' ? 'border-l border-border/70' : ''}`}>{c.w.n_lists}</td>)}
                </tr>
                {WEEK_ROWS.map(([lbl, f]) => (
                  <tr key={lbl} className="h-8 border-t border-border/70">
                    <td className="whitespace-nowrap px-3 text-left text-secondary">{lbl}</td>
                    {cols.map((c) => <td key={c.key} className={`px-3 text-right font-semibold ${sgnCls(f(c.w))} ${c.key === 'total' ? 'border-l border-border/70' : ''}`}>{num2(f(c.w))}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {last && (
        <>
          <H3>{`最近结算 · ${md(last.made)} 名单 · ${md(last.settled)} 收盘`}</H3>
          <div className="grid gap-3 sm:grid-cols-2">
            {(['long', 'short'] as const).map((side) => {
              const xs = last[side]
              const sum = side === 'long' ? last.sum_long : last.sum_short
              return (
                <div key={side} data-ls-last={side} className="overflow-hidden rounded-card border border-border">
                  <div className="flex items-baseline gap-2 bg-elevated px-3 py-1.5">
                    <b className={`text-[13px] ${side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[side]}</b>
                    <span className="text-[12px] text-secondary">{xs.length}</span>
                    <span className={`ml-auto text-[12.5px] font-semibold tabular-nums ${sgnCls(sum)}`}>{u === null ? `${num2(sum)} ATR` : `${num2(sum)} ATR · ${usdS(sum * u)}`}</span>
                  </div>
                  {xs.map((x) => (
                    <div key={x.symbol} role="button" tabIndex={0} onClick={() => onOpen(x.symbol)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(x.symbol) } }}
                      className="grid h-8 cursor-pointer grid-cols-[64px_minmax(0,1fr)_56px_60px] items-center gap-2 border-t border-border/70 px-3 text-[13px] tabular-nums hover:bg-elevated">
                      <b className="text-foreground">{x.symbol}</b>
                      <span className={`text-[11.5px] ${x.exit === 'stop' ? 'font-semibold text-warning' : 'text-muted'}`}>{x.exit === 'stop' ? '止损' : '到期'}</span>
                      <span className={`text-right font-semibold ${sgnCls(x.r)}`}>{num2(x.r)}</span>
                      <span className={`text-right ${sgnCls(x.r)}`}>{usdOf(x.r, u)}</span>
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </>
      )}
      <H3>判据</H3>
      <div data-ls-rules className="mb-2 overflow-hidden rounded-card border border-border text-[13px] tabular-nums">
        {[
          ['失败判据', `${s.check_days} 日 · 看多−自选全体 < 0 ATR`],
          ['当前', `${s.days}/${s.check_days} 日 · ${atrS(s.days > 0 ? s.total?.long_minus_all : null)}`, sgnCls(s.days > 0 ? s.total?.long_minus_all : null)],
          ['对照', '随机 5 多 5 空 · 同日结算'],
          ...(s.late ? [['不计入', `开盘后补出的名单 ${s.late} 份`]] : []),
          ['回放', `${d.backtest.period} · 看多 ${atrS(d.backtest.long.mean_atr)} · 看空 ${atrS(d.backtest.short.mean_atr)}`],
          ['单位', u === null ? `止损 ${d.stop_atr} ATR · 持有 ${d.hold_days} 日` : `1 ATR = $${Math.round(u)} · 每笔 $${Math.round(d.risk_usd ?? 0)} · 止损 ${d.stop_atr} ATR · 持有 ${d.hold_days} 日`],
          ['模型', `${d.model} · 训练截至 ${md(d.trained_to)}${d.made_at ? ` · 名单 ${md(d.made_at)} ${etHm(d.made_at)} ET 生成` : ''}`],
        ].map(([k, v, cls]) => (
          <div key={k} className="grid min-h-8 grid-cols-[84px_1fr] items-center gap-2 border-t border-border/70 px-3 py-1 first:border-t-0">
            <span className="text-secondary">{k}</span><span className={cls ?? 'text-foreground'}>{v}</span>
          </div>
        ))}
      </div>
    </Sheet>
  )
}

export function LsRank({ d, onOpen }: { d: LsRankPayload; onOpen: (s: string) => void }) {
  const [sheet, setSheet] = useState<'all' | 'settle' | null>(null)
  const nNew = [...d.long, ...d.short].filter((r) => r.is_new).length
  const meta = [`${md(d.day)} ${weekday(d.day)}`, `名单 ${md(d.as_of)} 收盘`, d.pre?.at ? `盘前 ${etHm(d.pre.at)} ET` : '', `换入 ${nNew}`,
    `持有 ${d.hold_days} 日至 ${md(d.hold_until)}`, fin(d.risk_usd) ? `每笔 $${Math.round(d.risk_usd)}` : '', `止损 ${d.stop_atr} ATR`].filter(Boolean)
  const open = (s: string) => { setSheet(null); onOpen(s) }
  return (
    <section data-ls-rank className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5">
        <h2 className="shrink-0 text-[15px] font-semibold text-foreground">今日多空</h2>
        {d.demo && <span data-ls-demo className="shrink-0 rounded-btn bg-warning px-1.5 text-[11px] font-semibold leading-5 text-white dark:text-zinc-950">虚拟数据</span>}
        {d.late && <span data-ls-late title="名单日开盘后才生成 · 照常结算 · 不计入前向成绩" className="shrink-0 rounded-btn border border-warning/60 bg-warning/10 px-1.5 text-[11px] font-semibold leading-5 text-warning">盘中补出</span>}
        <span data-ls-meta className="min-w-0 text-[12px] tabular-nums text-muted xl:ml-auto xl:truncate max-xl:order-last max-xl:basis-full">
          {meta.map((t, i) => <span key={t}>{i > 0 && ' '}<span className="whitespace-nowrap">{i < meta.length - 1 ? `${t} ·` : t}</span></span>)}
        </span>
        <div className="ml-auto flex shrink-0 gap-1.5 xl:ml-0">
          <button type="button" data-ls-open="all" onClick={() => setSheet('all')} className={BTN}>{`全部 ${d.all.length}`}</button>
          <button type="button" data-ls-open="settle" onClick={() => setSheet('settle')} className={BTN}>结算明细</button>
        </div>
      </div>
      <div data-ls-sides className="grid gap-px border-t border-border bg-border [grid-template-columns:repeat(auto-fit,minmax(min(100%,330px),1fr))]">
        <SidePanel side="long" rows={d.long} d={d} onOpen={onOpen} />
        <SidePanel side="short" rows={d.short} d={d} onOpen={onOpen} />
      </div>
      <div data-ls-strip className="grid grid-cols-2 gap-px border-t border-border bg-border sm:grid-cols-3 xl:grid-cols-6">
        {settleCells(d).map(({ key, ...c }) => <Stat key={key} data-ls-stat={key} {...c} />)}
      </div>
      {sheet === 'all' && <AllSheet d={d} onOpen={open} onClose={() => setSheet(null)} />}
      {sheet === 'settle' && <SettleSheet d={d} onOpen={open} onClose={() => setSheet(null)} />}
    </section>
  )
}

/** 名单尚未生成 (上线后第一个盘后管道之前) */
export function LsRankEmpty() {
  return (
    <section data-ls-rank-empty className="mb-3 flex items-center gap-2 rounded-card border border-border bg-surface px-3 py-2.5">
      <h2 className="text-[15px] font-semibold text-foreground">今日多空</h2>
      <span className="ml-auto text-[12px] tabular-nums text-muted">16:35 ET 出名单</span>
    </section>
  )
}
