/** 盘中异动 (2026-11-15 B 方案, 原「盘面雷达」): 高开低开 / 开盘量 / 盘中走强 + 多信号共振, 四组完整列出。
 * 三组顺序沿用大盘状态 (COL_ORDER, 去掉机会板); 共振固定最后且不含模型。只记录, 不下单, 不计入模型评分。
 * 每行 = 代码 · 读数 (不可断开的小段, 放不下整段换行, 不用固定窄列, 不截断) · 现在涨跌 (跟报价刷新)。
 * 文案只写结果 (名词 + 数字), 不写说明句。 */
import type { ReactNode } from 'react'
import type { DeskEngine, DeskRegimeKey, DeskResonance, GapBoardPayload, IntradayPayload, OpenRadarPayload, OppDesk, OppQuote, OppRadarView } from '@/lib/api'
import { dayTag, p2, SIDE_ARROW } from '@/components/opportunities/terms'
import { badge } from '@/components/ui/badge'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const sgn = (v?: number | null) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const pc = (v?: number | null, d = 2) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%` : '—')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')

export const REGIME_ZH: Record<DeskRegimeKey, string> = { up: '普涨', normal: '平常', down: '普跌' }
export const COL_ORDER: Record<DeskRegimeKey, DeskEngine[]> = {
  up: ['intraday', 'open', 'gap', 'board'],
  normal: ['board', 'intraday', 'open', 'gap'],
  down: ['board', 'open', 'gap', 'intraday'],
}
export const ENGINE_ZH: Record<DeskEngine, string> = { gap: '高开低开', open: '开盘量', intraday: '盘中走强', board: '今日机会' }

/** 「a · b · c」按分隔符拆成不可断开的小段: 放不下时整段落到下一行, 不在数字中间断开, 也不截成省略号 */
function segs(node: ReactNode): ReactNode {
  if (typeof node !== 'string' || !node.includes(' · ')) return node
  const parts = node.split(' · ')
  return (
    <span className="block overflow-hidden">
      <span className="-ml-3 flex flex-wrap">
        {parts.map((p, i) => (
          <span key={i} className="relative whitespace-nowrap pl-3">
            {i > 0 && <span aria-hidden className="absolute left-0 w-3 whitespace-pre text-center text-muted">{' · '}</span>}{p}
          </span>
        ))}
      </span>
    </span>
  )
}

function Row({ symbol, onOpen, mark, main, sub, right, rightCls, dim, ...rest }: {
  symbol: string; onOpen: (s: string) => void; mark?: boolean; main: ReactNode; sub?: ReactNode; right: ReactNode; rightCls?: string; dim?: boolean
} & Record<`data-${string}`, string | undefined>) {
  return (
    <div role="button" tabIndex={0} {...rest} onClick={() => onOpen(symbol)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(symbol) } }}
      className="grid min-h-[44px] cursor-pointer grid-cols-[60px_minmax(0,1fr)_auto] items-center gap-x-2 border-t border-border/70 px-3 py-1.5 tabular-nums first:border-t-0 hover:bg-elevated">
      <span className="flex flex-col leading-[18px]">
        <span className={`text-[14px] font-bold ${dim ? 'text-muted' : 'text-foreground'}`}>{symbol}</span>
        {mark && <span data-res-mark title="多信号共振" className="text-[11px] font-semibold leading-[14px] text-accent dark:text-blue-400">共振</span>}
      </span>
      <span data-row-main className={`min-w-0 text-[13px] leading-[18px] ${dim ? 'text-muted' : 'text-foreground'}`}>{segs(main)}</span>
      <span className={`whitespace-nowrap text-right text-[13px] font-semibold ${rightCls ?? 'text-foreground'}`}>{right}</span>
      {sub && <span data-row-sub className="col-span-2 col-start-2 min-w-0 text-[12px] leading-4 text-secondary">{segs(sub)}</span>}
    </div>
  )
}

const Empty = ({ t }: { t: string }) => <div data-col-empty className="px-3 pb-3 pt-1 text-[13px] text-muted">{t}</div>
const Err = ({ name }: { name: string }) => <div data-col-error className="px-3 pb-3 pt-1 text-[13px] text-danger">{`${name}读取失败`}</div>

export type RadarData = { gaps?: GapBoardPayload | null; radar?: OpenRadarPayload | null; intraday?: IntradayPayload | null; view: OppRadarView | null
  errors: Record<string, string>; chg: Record<string, number>; quotes: Record<string, OppQuote>; res: Set<string> }
type Col = { count: number; meta: string; body: ReactNode }

/** 现在涨跌: 报价优先, 其次 /page 的大盘条 */
const nowChg = (d: RadarData, s: string): number | null => (d.quotes[s] as OppQuote | undefined)?.chg ?? (d.chg[s] as number | undefined) ?? null
/** 非交易日的「下一交易日」前缀 */
const nextTag = (v: OppRadarView | null) => (v && !v.clock.trading_day ? `${dayTag(v.clock.session_day, v.clock.et.slice(0, 10))} ` : '')

function gapCol(d: RadarData, onOpen: (s: string) => void): Col {
  const g = d.gaps
  const rows = g?.rows ?? []
  const body = d.errors.gaps ? <Err name="高开低开" /> : !g ? <Empty t={`${nextTag(d.view)}09:00 ET 定稿`} /> : rows.length === 0 ? <Empty t={g.warming ? '取数中' : '无'} /> : rows.map((r) => (
    <Row key={r.symbol} data-gap-row={r.symbol} symbol={r.symbol} onOpen={onOpen} mark={d.res.has(r.symbol)}
      main={<>{r.gap_pct >= 0 ? '高开 ' : '低开 '}<b className={sgn(r.gap_pct)}>{pc(r.gap_pct, 1)}</b>{r.role ? <span className="text-secondary">{` · ${r.role}`}</span> : null}</>}
      right={pc(nowChg(d, r.symbol))} rightCls={sgn(nowChg(d, r.symbol))} />
  ))
  return { count: rows.length, meta: g ? `${etHm(g.at)} ET ${g.live ? '实时' : '定稿'} · 门槛 ${g.threshold_pct}%` : '', body }
}

function openCol(d: RadarData, onOpen: (s: string) => void): Col {
  const r = d.radar
  const live = !!r?.is_today
  const picks = live ? r!.picks : []
  const others = live ? r!.others : []
  const push = r?.push ? (r.push.enabled ? `推送 ≥${r.push.min_rvol} 倍` : '推送关') : ''
  const body = d.errors.radar ? <Err name="开盘量" /> : !live ? <Empty t={`${nextTag(d.view)}${r?.ready_at ?? '09:51'} ET 发布`} /> : picks.length + others.length === 0 ? <Empty t="无" /> : (
    <>
      {picks.map((x) => (
        <Row key={x.symbol} data-open-row={x.symbol} symbol={x.symbol} onOpen={onOpen} mark={d.res.has(x.symbol)}
          main={`第 ${x.rank} · 平时的 ${x.rvol.toFixed(1)} 倍 · 开盘 5 分钟 ${pc(x.or_chg_pct, 1)}`} sub={`开盘区间 ${p2(x.or_low)} ~ ${p2(x.or_high)}`}
          right={pc(nowChg(d, x.symbol) ?? x.chg_now_pct)} rightCls={sgn(nowChg(d, x.symbol) ?? x.chg_now_pct)} />
      ))}
      {others.length > 0 && <div data-open-others-head className="border-t border-border/70 px-3 pb-0.5 pt-1.5 text-[12px] text-muted">{`未达门槛 ${others.length}`}</div>}
      {others.map((x) => (
        <Row key={x.symbol} data-open-other={x.symbol} symbol={x.symbol} onOpen={onOpen} dim
          main={`平时的 ${x.rvol.toFixed(1)} 倍 · 开盘 5 分钟 ${pc(x.or_chg_pct, 1)}`}
          right={pc(nowChg(d, x.symbol) ?? x.chg_now_pct)} rightCls={sgn(nowChg(d, x.symbol) ?? x.chg_now_pct)} />
      ))}
    </>
  )
  return { count: picks.length, meta: [live ? `${etHm(r!.at)} ET` : '', push].filter(Boolean).join(' · '), body }
}

const EXIT_ZH: Record<string, string> = { target: '到目标', stop: '止损', time: '到期' }

function intradayCol(d: RadarData, onOpen: (s: string) => void): Col {
  const it = d.intraday
  const active = it?.active ?? []
  // 今日已结束 = 今天出场的卡 (含前几天出的卡今天止损 / 到期); exit_at 是美东墙钟 ISO, 前 10 位即美东日期
  const closedToday = (it?.history ?? []).filter((c) => (c.exit_at ? c.exit_at.slice(0, 10) : c.day) === it?.today)
  const first = d.view?.lanes.intraday.schedule[0] ?? '10:00'
  const body = d.errors.intraday ? <Err name="盘中走强" /> : !it ? <Empty t="—" /> : active.length + closedToday.length === 0
    ? <Empty t={it.next_check ? `${it.next_check} ET 检查` : `${nextTag(d.view)}${first} ET 首次检查`} /> : (
      <>
        {active.map((c) => {
          const now = fin(c.price) && c.entry > 0 ? (((d.quotes[c.symbol] as OppQuote | undefined)?.price ?? c.price) / c.entry - 1) * 100 : null
          return (
            <Row key={c.card_id} data-intraday-row={c.symbol} symbol={c.symbol} onOpen={onOpen} mark={d.res.has(c.symbol)}
              main={`${md(c.day)} ${c.check} 走强 · 第 ${c.day_n ?? 1}/${c.hold_days} 天`} sub={`入场 ${p2(c.entry)} · 止损 ${p2(c.stop)} · 目标 ${p2(c.target)}`}
              right={pc(now)} rightCls={sgn(now)} />
          )
        })}
        {closedToday.map((c) => {
          const res = fin(c.exit) && c.entry > 0 ? (c.exit / c.entry - 1) * 100 : null
          return (
            <Row key={c.card_id} data-intraday-closed={c.symbol} symbol={c.symbol} onOpen={onOpen} dim
              main={`${c.check} 走强 · ${etHm(c.exit_at ?? '') || '—'} ${EXIT_ZH[c.exit_reason ?? ''] ?? '结束'}`} right={pc(res)} rightCls={sgn(res)} />
          )
        })}
      </>
    )
  return { count: active.length, meta: it?.next_check ? `下次 ${it.next_check} ET` : '', body }
}

function chips(x: DeskResonance): string[] {
  const e = x.engines
  const out: string[] = []
  if (e.gap) out.push(`${e.gap.pct >= 0 ? '高开' : '低开'} ${pc(e.gap.pct, 1)}`)
  if (e.open) out.push(`开盘量第 ${e.open.rank ?? '—'}`)
  for (const c of e.intraday?.cards ?? []) out.push(`盘中走强 ${md(c.day)}`)
  if (e.board) out.push(e.board.state === 'in_zone' ? '机会 已到进场价' : '机会 等进场')
  return out
}

function resonanceCol(d: RadarData, desk: OppDesk | null, onOpen: (s: string) => void): Col {
  const xs = desk?.resonance ?? []
  const sh = desk?.shadow
  const closed = !!d.view && !d.view.clock.trading_day
  const body = d.errors.desk ? <Err name="多信号共振" /> : !desk ? <Empty t="—" /> : xs.length === 0 ? <Empty t={closed ? `${nextTag(d.view)}开盘后` : '无'} /> : xs.map((x) => (
    <Row key={x.symbol} data-res={x.symbol} symbol={x.symbol} onOpen={onOpen}
      main={`${x.n} 个信号同向 · ${SIDE_ARROW[x.side]}`}
      sub={`${chips(x).join(' · ')} · ${x.shadow_at ? `观察 ${etHm(x.shadow_at)}` : '观察'}`}
      right={pc(nowChg(d, x.symbol) ?? x.chg)} rightCls={sgn(nowChg(d, x.symbol) ?? x.chg)} />
  ))
  return { count: xs.length, meta: sh ? `观察 ${sh.n} · 已结算 ${sh.settled}` : '', body }
}

type RadarEngine = Exclude<DeskEngine, 'board'>

export function RadarFull({ order, d, desk, onOpen }: { order: DeskEngine[]; d: RadarData; desk: OppDesk | null; onOpen: (s: string) => void }) {
  const cols: [string, string, Col][] = order.filter((k): k is RadarEngine => k !== 'board').map((k) => [k, ENGINE_ZH[k],
    k === 'gap' ? gapCol(d, onOpen) : k === 'open' ? openCol(d, onOpen) : intradayCol(d, onOpen)] as [string, string, Col])
  cols.push(['resonance', '多信号共振', resonanceCol(d, desk, onOpen)])
  return (
    <section data-radar-section className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
        <h2 className="text-[14px] font-semibold text-foreground">盘中异动</h2>
        <span title="只记录 · 不下单 · 不计入模型评分" className={badge('limit')}>仅参考 · 不下单</span>
        <span className="ml-auto text-[12px] text-muted">右列 = 对昨收涨跌</span>
      </div>
      <div data-cols className="grid items-start gap-3 border-t border-border/70 bg-base p-3 md:grid-cols-2 2xl:grid-cols-4">
        {cols.map(([k, title, c]) => (
          <div key={k} data-col={k} className="flex min-w-0 flex-col rounded-card border border-border bg-surface">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 pb-1.5 pt-2.5">
              <h3 className="shrink-0 whitespace-nowrap text-[14px] font-semibold text-foreground">{title}</h3>
              <span className="shrink-0 text-[13px] font-semibold tabular-nums text-secondary">{c.count}</span>
              {c.meta && <span data-head-meta className="ml-auto min-w-0 text-right text-[12px] tabular-nums text-muted">{c.meta}</span>}
            </div>
            <div data-col-body className="pb-1">{c.body}</div>
          </div>
        ))}
      </div>
    </section>
  )
}
