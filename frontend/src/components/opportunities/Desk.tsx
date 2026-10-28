/** 机会页一页作战台 (0.41.0, 无限 10-28 选「非显然档」): 大盘状态条 + 共振 + 四栏同屏 + 持仓。
 * 四栏顺序按大盘状态 (后端 desk.regime, 与 10-22 回测同口径): 普涨 = 盘中强势 → 开盘放量 → 盘前跳空 → 机会板;
 * 平常 = 机会板 → 盘中强势 → 开盘放量 → 盘前跳空; 普跌 = 机会板 → 开盘放量 → 盘前跳空 → 盘中强势。
 * 共振 = 同票 >= 2 个引擎同向, 只进影子账本 (不下单)。只展示: 交易环 / 准入 / 下单簿 / 台账不变。
 * 文案只写结果 (名词 + 数字), 不写说明句。 */
import type { ReactNode } from 'react'
import type { DeskEngine, DeskRegimeKey, DeskResonance, GapBoardPayload, IntradayPayload, OpenRadarPayload, OppDesk, OppRadarView,
  RadarBlip, SwingBoardsPayload } from '@/lib/api'
import { p2, rS, SIDE_ZH, usdS } from '@/components/opportunities/terms'

const sgn = (v?: number | null) => (typeof v !== 'number' || !Number.isFinite(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')
const pc = (v?: number | null, d = 2) => (typeof v === 'number' && Number.isFinite(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%` : '—')
const x1 = (v?: number | null) => (typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(1)}×` : '—')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')

export const REGIME_ZH: Record<DeskRegimeKey, string> = { up: '普涨', normal: '平常', down: '普跌' }
export const COL_ORDER: Record<DeskRegimeKey, DeskEngine[]> = {
  up: ['intraday', 'open', 'gap', 'board'],
  normal: ['board', 'intraday', 'open', 'gap'],
  down: ['board', 'open', 'gap', 'intraday'],
}
export const ENGINE_ZH: Record<DeskEngine, string> = { gap: '盘前跳空', open: '开盘放量', intraday: '盘中强势', board: '机会板' }
const MOOD_ZH: Record<string, string> = { up: '偏多', down: '偏空', range: '震荡' }

function Panel({ children, className = '', ...rest }: { children: ReactNode; className?: string } & Record<`data-${string}`, string | undefined>) {
  return <section {...rest} className={`min-w-0 rounded-card border border-border bg-surface ${className}`}>{children}</section>
}

function Head({ title, count, meta, rank }: { title: string; count?: number; meta?: string; rank?: number }) {
  return (
    <div className="flex items-baseline gap-2 px-3 pb-1.5 pt-2.5">
      {rank !== undefined && <span data-col-rank className="rounded-[4px] bg-accent px-1.5 text-[11px] font-semibold leading-[18px] text-white dark:bg-blue-700">{rank}</span>}
      <h2 className="text-[14px] font-semibold text-foreground">{title}</h2>
      {count !== undefined && <span className="text-[13px] font-semibold tabular-nums text-secondary">{count}</span>}
      {meta && <span className="ml-auto whitespace-nowrap text-[12px] tabular-nums text-muted">{meta}</span>}
    </div>
  )
}

function Row({ symbol, onOpen, mark, main, sub, right, rightCls, dim, ...rest }: {
  symbol: string; onOpen: (s: string) => void; mark?: boolean; main: ReactNode; sub?: ReactNode; right: ReactNode; rightCls?: string; dim?: boolean
} & Record<`data-${string}`, string | undefined>) {
  return (
    <div role="button" tabIndex={0} {...rest} onClick={() => onOpen(symbol)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(symbol) } }}
      className="grid min-h-[44px] cursor-pointer grid-cols-[64px_minmax(0,1fr)_auto] items-center gap-2 border-t border-border/70 px-3 py-1.5 tabular-nums first:border-t-0 hover:bg-elevated">
      <span className={`text-[14px] font-bold ${dim ? 'text-muted' : 'text-foreground'}`}>
        {symbol}{mark && <i data-res-mark className="ml-1 inline-block h-1.5 w-1.5 -translate-y-0.5 rounded-full bg-accent" />}
      </span>
      <span className="min-w-0">
        <span className={`block truncate text-[12.5px] ${dim ? 'text-muted' : 'text-foreground'}`}>{main}</span>
        {sub && <span className="block truncate text-[11.5px] text-secondary">{sub}</span>}
      </span>
      <span className={`whitespace-nowrap text-right text-[13px] font-semibold ${rightCls ?? 'text-foreground'}`}>{right}</span>
    </div>
  )
}

const Empty = ({ t }: { t: string }) => <div data-col-empty className="px-3 pb-3 pt-1 text-[12.5px] text-muted">{t}</div>
const Err = ({ name }: { name: string }) => <div data-col-error className="px-3 pb-3 pt-1 text-[12.5px] text-danger">{`${name}读取失败`}</div>

// ── 大盘状态条 ──
export function RegimeBar({ desk, mood }: { desk: OppDesk; mood?: { stance?: string; probs?: Record<string, number> } | null }) {
  const r = desk.regime
  const k = r.key
  const cls = k === 'up' ? 'bg-bull/15 text-bull' : k === 'down' ? 'bg-bear/15 text-bear' : 'bg-accent/10 text-accent'
  return (
    <Panel data-regime={k ?? 'none'} className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 px-3 py-2.5 text-[13px] tabular-nums">
      <span data-regime-badge className={`rounded-btn px-2.5 py-0.5 text-[14px] font-bold ${k ? cls : 'bg-elevated text-muted'}`}>{k ? REGIME_ZH[k] : '大盘 —'}</span>
      <span data-regime-breadth className="text-secondary">{'自选涨≥1% '}<b className="text-foreground">{`${r.up}/${r.n}`}</b>{' · 涨≥3% '}<b className="text-foreground">{r.up3}</b>{' · 跌≥1% '}<b className="text-foreground">{r.down}</b></span>
      <span data-regime-index className="text-secondary">{'SPY '}<b className={sgn(r.spy)}>{pc(r.spy)}</b>{' · QQQ '}<b className={sgn(r.qqq)}>{pc(r.qqq)}</b></span>
      {mood?.stance && <span data-regime-mood className="text-secondary">{'盘前判断 '}<b className="text-foreground">{MOOD_ZH[mood.stance] ?? mood.stance}</b></span>}
      <span className="ml-auto text-[12px] text-muted">{`${etHm(r.at)} ET`}</span>
    </Panel>
  )
}

// ── 共振 ──
function chips(x: DeskResonance): string[] {
  const e = x.engines
  const out: string[] = []
  if (e.gap) out.push(`跳空 ${pc(e.gap.pct, 1)}`)
  if (e.open) out.push(`开盘放量 #${e.open.rank ?? '—'} ${x1(e.open.rvol)}`)
  for (const c of e.intraday?.cards ?? []) out.push(`盘中强势 ${md(c.day)} ${rS(c.r)}`)
  if (e.board) out.push(e.board.state === 'in_zone' ? '机会板 在区' : '机会板 挂单')
  return out
}

export function Resonance({ desk, onOpen }: { desk: OppDesk; onOpen: (s: string) => void }) {
  const xs = desk.resonance
  const sh = desk.shadow
  const meta = `影子 ${sh.n} · 已结算 ${sh.settled}${typeof sh.mean5 === 'number' ? ` · 5日 ${pc(sh.mean5)}` : ''}`
  return (
    <Panel data-resonance className="mb-3">
      <Head title="共振" count={xs.length} meta={meta} />
      {xs.length === 0 ? <Empty t="无" /> : (
        <div className="grid gap-2 px-3 pb-3 sm:grid-cols-2 lg:grid-cols-3">
          {xs.map((x) => (
            <div key={x.symbol} role="button" tabIndex={0} data-res={x.symbol} onClick={() => onOpen(x.symbol)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(x.symbol) } }}
              className="cursor-pointer rounded-card border border-border bg-elevated px-3 py-2 tabular-nums hover:border-accent/60">
              <div className="flex items-baseline gap-2">
                <b className="text-[15px] text-foreground">{x.symbol}</b>
                <span className={`text-[12.5px] font-semibold ${x.side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[x.side]}</span>
                <span className="text-[12px] text-secondary">{`${x.n} 引擎`}</span>
                <span className="ml-auto text-[13px] text-foreground">{p2(x.price)}</span>
                <span className={`text-[13px] font-semibold ${sgn(x.chg)}`}>{pc(x.chg)}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {chips(x).map((c) => <span key={c} className="rounded-[4px] border border-border bg-surface px-1.5 text-[11.5px] leading-5 text-secondary">{c}</span>)}
                <span data-res-shadow className="rounded-[4px] border border-dashed border-border px-1.5 text-[11.5px] leading-5 text-muted">{x.shadow_at ? `影子 ${etHm(x.shadow_at)}` : '影子'}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

// ── 四栏 ──
type ColData = { gaps?: GapBoardPayload | null; radar?: OpenRadarPayload | null; intraday?: IntradayPayload | null; swing?: SwingBoardsPayload | null
  errors: Record<string, string>; chg: Record<string, number>; res: Set<string> }

function GapCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const g = d.gaps
  const rows = g?.rows ?? []
  const body = d.errors.gaps ? <Err name="盘前跳空" /> : !g ? <Empty t="09:00" /> : rows.length === 0 ? <Empty t={g.warming ? '取数中' : '无'} /> : rows.map((r) => (
    <Row key={r.symbol} data-gap-row={r.symbol} symbol={r.symbol} onOpen={onOpen} mark={d.res.has(r.symbol)}
      main={<>{'跳空 '}<b className={sgn(r.gap_pct)}>{pc(r.gap_pct, 1)}</b>{r.role ? ` · ${r.role}` : ''}</>}
      right={pc(d.chg[r.symbol])} rightCls={sgn(d.chg[r.symbol])} />
  ))
  return { count: rows.length, meta: g ? `${etHm(g.at)} ET${g.live ? ' 实时' : ''}` : '', body }
}

function OpenCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const r = d.radar
  const live = !!r?.is_today
  const picks = live ? r!.picks : []
  const others = live ? r!.others : []
  const body = d.errors.radar ? <Err name="开盘放量" /> : !live ? <Empty t={`${r?.ready_at ?? '09:51'} ET`} /> : picks.length + others.length === 0 ? <Empty t="无" /> : (
    <>
      {picks.map((x) => (
        <Row key={x.symbol} data-open-row={x.symbol} symbol={x.symbol} onOpen={onOpen} mark={d.res.has(x.symbol)}
          main={`#${x.rank} · 相对量 ${x1(x.rvol)} · 开盘5分 ${pc(x.or_chg_pct, 1)}`} sub={`区间 ${p2(x.or_low)}–${p2(x.or_high)}`}
          right={pc(x.chg_now_pct)} rightCls={sgn(x.chg_now_pct)} />
      ))}
      {others.map((x) => (
        <Row key={x.symbol} data-open-other={x.symbol} symbol={x.symbol} onOpen={onOpen} dim
          main={`相对量 ${x1(x.rvol)} · 开盘5分 ${pc(x.or_chg_pct, 1)}`} right={pc(x.chg_now_pct)} rightCls={sgn(x.chg_now_pct)} />
      ))}
    </>
  )
  return { count: picks.length, meta: live ? `${etHm(r!.at)} ET` : '', body }
}

function IntradayCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const it = d.intraday
  const active = it?.active ?? []
  const closedToday = (it?.history ?? []).filter((c) => c.day === it?.today)
  const st = it?.stats
  const body = d.errors.intraday ? <Err name="盘中强势" /> : !it ? <Empty t="—" /> : active.length + closedToday.length === 0 ? <Empty t={it.next_check ? `${it.next_check} ET` : '无'} /> : (
    <>
      {active.map((c) => (
        <Row key={c.card_id} data-intraday-row={c.symbol} symbol={c.symbol} onOpen={onOpen} mark={d.res.has(c.symbol)}
          main={`${md(c.day)} ${c.check} · 第 ${c.day_n ?? 1}/${c.hold_days} 天`} sub={`入 ${p2(c.entry)} · 止 ${p2(c.stop)} · 目 ${p2(c.target)}`}
          right={rS(c.r_now)} rightCls={sgn(c.r_now)} />
      ))}
      {closedToday.map((c) => (
        <Row key={c.card_id} data-intraday-closed={c.symbol} symbol={c.symbol} onOpen={onOpen} dim
          main={`${c.check} 出 · ${etHm(c.exit_at ?? '') || '—'} ${c.exit_reason === 'target' ? '目标' : c.exit_reason === 'stop' ? '止损' : '结束'}`}
          right={rS(c.r)} rightCls={sgn(c.r)} />
      ))}
    </>
  )
  const meta = [it?.next_check ? `下次 ${it.next_check}` : '', st ? `已结算 ${st.n} · ${rS(st.sum_r)}` : ''].filter(Boolean).join(' · ')
  return { count: active.length, meta, body }
}

function BoardCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const sw = d.swing
  const cards = sw ? [...sw.long, ...sw.short].filter((c) => c.state !== 'holding') : []
  const body = d.errors.swing ? <Err name="机会板" /> : !sw ? <Empty t="—" /> : cards.length === 0 ? <Empty t="无" /> : cards.map((c) => (
    <Row key={c.card_id ?? `${c.symbol}${c.side}`} data-board-row={c.symbol} symbol={c.symbol} onOpen={onOpen} mark={d.res.has(c.symbol)}
      main={<><b className={c.side === 'long' ? 'text-bull' : 'text-bear'}>{SIDE_ZH[c.side]}</b>{` · 区 ${p2(c.entry_zone[0])}–${p2(c.entry_zone[1])} · 止 ${p2(c.stop)}`}</>}
      sub={[c.state === 'in_zone' ? '在区' : typeof c.dist_pct === 'number' ? `距区 ${Math.abs(c.dist_pct).toFixed(1)}%` : '',
        typeof c.odds?.fill === 'number' ? `回区 ${(c.odds.fill * 100).toFixed(0)}%` : '', c.blocked ? '受限' : ''].filter(Boolean).join(' · ')}
      right={p2(c.price)} />
  ))
  return { count: cards.length, meta: sw?.refresh?.last_at ? `刷新 ${etHm(sw.refresh.last_at)}` : '', body }
}

const COLS: Record<DeskEngine, (p: { d: ColData; onOpen: (s: string) => void }) => { count: number; meta: string; body: ReactNode }> = {
  gap: GapCol, open: OpenCol, intraday: IntradayCol, board: BoardCol,
}

export function EngineCols({ order, d, onOpen }: { order: DeskEngine[]; d: ColData; onOpen: (s: string) => void }) {
  return (
    <div data-cols className="mb-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {order.map((k, i) => {
        const c = COLS[k]({ d, onOpen })
        return (
          <Panel key={k} data-col={k} className={i === 0 ? 'ring-2 ring-inset ring-accent' : ''}>
            <Head title={ENGINE_ZH[k]} count={c.count} meta={c.meta} rank={i + 1} />
            <div className="pb-1">{c.body}</div>
          </Panel>
        )
      })}
    </div>
  )
}

// ── 持仓 (台账为准: 当前止损 / 浮动) ──
export function Holdings({ view, onOpen }: { view: OppRadarView; onOpen: (s: string) => void }) {
  const hs = view.blips.filter((b): b is RadarBlip & { hold: NonNullable<RadarBlip['hold']> } => !!b.hold)
    .sort((a, b) => (a.hold.r_now ?? 0) - (b.hold.r_now ?? 0))
  const pnl = hs.reduce((s, b) => s + (b.hold.pnl_usd ?? 0), 0)
  return (
    <Panel data-holdings>
      <Head title="持仓" count={hs.length} meta={hs.length ? `浮动 ${usdS(pnl)}` : ''} />
      {hs.length === 0 ? <Empty t="无" /> : (
        <div className="overflow-x-auto pb-1">
          <table className="w-full min-w-[560px] text-[13px] tabular-nums">
            <thead><tr className="text-[12px] text-muted">
              {['票', '方向', '股数', '成本', '现价', '止损', '浮动', '第几天'].map((h, i) => <th key={h} className={`px-3 py-1.5 font-medium ${i ? 'text-right' : 'text-left'}`}>{h}</th>)}
            </tr></thead>
            <tbody>
              {hs.map((b) => {
                const h = b.hold
                return (
                  <tr key={b.symbol} data-hold={b.symbol} onClick={() => onOpen(b.symbol)} className="cursor-pointer border-t border-border/70 hover:bg-elevated">
                    <td className="px-3 py-2 text-left font-bold text-foreground">{b.symbol}</td>
                    <td className={`px-3 py-2 text-right font-semibold ${b.side === 'short' ? 'text-bear' : 'text-bull'}`}>{SIDE_ZH[b.side ?? 'long']}</td>
                    <td className="px-3 py-2 text-right">{h.qty}</td>
                    <td className="px-3 py-2 text-right">{p2(h.avg)}</td>
                    <td className="px-3 py-2 text-right">{p2(h.price)}</td>
                    <td className="px-3 py-2 text-right">{p2(h.stop_now)}{h.stop_moved && <span className="text-muted">{` (${p2(h.stop_initial)})`}</span>}</td>
                    <td className={`px-3 py-2 text-right font-semibold ${sgn(h.r_now)}`}>{`${rS(h.r_now)} · ${usdS(h.pnl_usd)}`}</td>
                    <td className="px-3 py-2 text-right">{`${h.day_n}/${h.max_days}`}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
