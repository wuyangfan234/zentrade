/** 机会页 A 方案 (0.52.2, 11-06 产品决策选定): 模型观察 → 独立策略机会 → 盘面雷达 → 持仓。
 * 雷达三栏顺序沿用大盘状态; 引擎共振固定第四栏且不含模型; 机会板不再与只记录引擎混排。
 * 共振只进影子账本。只展示: 交易环 / 准入 / 下单簿 / 台账不变。
 * 文案只写结果 (名词 + 数字), 不写说明句。 */
import type { ReactNode } from 'react'
import type { DeskEngine, DeskRegimeKey, DeskResonance, GapBoardPayload, IntradayPayload, LiveVsReplay, OpenRadarPayload, OppDesk, OppRadarView,
  RadarBlip, SwingBoardsPayload } from '@/lib/api'
import { cardVerdict, p2, rS, SIDE_ZH, usdS } from '@/components/opportunities/terms'

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

function Panel({ children, className = '', ...rest }: { children: ReactNode; className?: string } & Record<`data-${string}`, string | undefined>) {
  return <section {...rest} className={`min-w-0 rounded-card border border-border bg-surface ${className}`}>{children}</section>
}

function Head({ title, count, meta }: { title: string; count?: number; meta?: string }) {
  return (
    <div className="flex items-baseline gap-2 px-3 pb-1.5 pt-2.5">
      <h2 className="shrink-0 whitespace-nowrap text-[14px] font-semibold text-foreground">{title}</h2>
      {count !== undefined && <span className="shrink-0 text-[13px] font-semibold tabular-nums text-secondary">{count}</span>}
      {meta && <span title={meta} className="ml-auto min-w-0 truncate whitespace-nowrap text-[12px] tabular-nums text-muted">{meta}</span>}
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

/** 上线后实际成绩并排回放值: 「笔数 · 每笔 R · 区间」并排「回放 R」; 区间不含回放值标「背离」。只展示。
 * lead = 开头那一项 (默认「实际 n 笔」); leadRef = 它旁边的回放对照 (如回区率)。 */
export function LiveLine({ label, live, lead, leadRef, stale, ...rest }: { label?: string; live?: LiveVsReplay | null; lead?: string; leadRef?: string
  stale?: boolean } & Record<`data-${string}`, string | undefined>) {
  if (!live) return null
  const ciTxt = live.ci ? `${rS(live.ci[0])} ~ ${rS(live.ci[1])}` : ''
  const title = live.ci ? '区间 = 出卡日聚类 95%' : '出卡日不足 8 个，不给区间'
  return (
    <span {...rest} data-diverged={live.diverged ? '' : undefined} title={title} className="inline-flex flex-wrap items-baseline gap-x-1.5 whitespace-nowrap">
      {label && <b className="font-semibold text-foreground">{label}</b>}
      <span className="text-secondary">{lead ?? `实际 ${live.n} 笔`}</span>
      {leadRef && <span className="text-muted">{leadRef}</span>}
      {live.n > 0 && <span className="text-secondary">{'每笔 '}<b className={`font-semibold ${sgn(live.mean_r)}`}>{rS(live.mean_r)}</b></span>}
      {ciTxt && <span className="text-muted">{ciTxt}</span>}
      <span className="text-muted">{`回放 ${stale ? '—' : rS(live.replay_mean_r)}`}</span>
      {live.diverged && <span data-diverged-badge className="rounded-[4px] border border-warning/60 px-1 text-[11px] leading-4 text-warning">背离</span>}
    </span>
  )
}

const Empty = ({ t }: { t: string }) => <div data-col-empty className="px-3 pb-3 pt-1 text-[12.5px] text-muted">{t}</div>
const Err = ({ name }: { name: string }) => <div data-col-error className="px-3 pb-3 pt-1 text-[12.5px] text-danger">{`${name}读取失败`}</div>

// ── 大盘状态条 ──
/** 0.51.0: 盘前判断移到页面上方独立一行 (MoodBar), 大盘状态条只写盘中实况 */
export function RegimeBar({ desk }: { desk: OppDesk }) {
  const r = desk.regime
  const k = r.key
  const cls = k === 'up' ? 'bg-bull/15 text-bull' : k === 'down' ? 'bg-bear/15 text-bear' : 'bg-accent/10 text-accent'
  return (
    <Panel data-regime={k ?? 'none'} className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 px-3 py-2.5 text-[13px] tabular-nums">
      <span data-regime-badge className={`rounded-btn px-2.5 py-0.5 text-[14px] font-bold ${k ? cls : 'bg-elevated text-muted'}`}>{k ? REGIME_ZH[k] : '大盘 —'}</span>
      <span data-regime-breadth className="text-secondary">{'自选涨≥1% '}<b className="text-foreground">{`${r.up}/${r.n}`}</b>{' · 涨≥3% '}<b className="text-foreground">{r.up3}</b>{' · 跌≥1% '}<b className="text-foreground">{r.down}</b></span>
      <span data-regime-index className="text-secondary">{'SPY '}<b className={sgn(r.spy)}>{pc(r.spy)}</b>{' · QQQ '}<b className={sgn(r.qqq)}>{pc(r.qqq)}</b></span>
      <span className="ml-auto text-[12px] text-muted">{`${etHm(r.at)} ET`}</span>
    </Panel>
  )
}

// ── 引擎共振 (盘面雷达第四栏, 不含今日多空模型) ──
function chips(x: DeskResonance): string[] {
  const e = x.engines
  const out: string[] = []
  if (e.gap) out.push(`跳空 ${pc(e.gap.pct, 1)}`)
  if (e.open) out.push(`开盘放量 #${e.open.rank ?? '—'} ${x1(e.open.rvol)}`)
  for (const c of e.intraday?.cards ?? []) out.push(`盘中强势 ${md(c.day)} ${rS(c.r)}`)
  if (e.board) out.push(e.board.state === 'in_zone' ? '机会板 在区' : '机会板 挂单')
  return out
}

function ResonanceCol({ desk, error, onOpen }: { desk: OppDesk | null; error?: string; onOpen: (s: string) => void }) {
  const xs = desk?.resonance ?? []
  const sh = desk?.shadow
  const meta = ['不含模型', sh ? `影子 ${sh.n} · 已结算 ${sh.settled}${typeof sh.mean5 === 'number' ? ` · 5日 ${pc(sh.mean5)}` : ''}` : ''].filter(Boolean).join(' · ')
  return (
    <Panel data-resonance data-col="resonance" className="flex flex-col">
      <Head title="引擎共振" count={xs.length} meta={meta} />
      <div data-col-body className={COL_BODY}>
        {error ? <Err name="引擎共振" /> : !desk ? <Empty t="—" /> : xs.length === 0 ? <Empty t="无" /> : xs.map((x) => (
          <Row key={x.symbol} data-res={x.symbol} symbol={x.symbol} onOpen={onOpen}
            main={<>{`${x.n} 引擎 · `}<b className={x.side === 'long' ? 'text-bull' : 'text-bear'}>{SIDE_ZH[x.side]}</b></>}
            sub={<>{chips(x).join(' · ')}{' · '}<span data-res-shadow>{x.shadow_at ? `影子 ${etHm(x.shadow_at)}` : '影子'}</span></>}
            right={pc(x.chg)} rightCls={sgn(x.chg)} />
        ))}
      </div>
    </Panel>
  )
}

// ── 盘面雷达栏 ──
type ColData = { gaps?: GapBoardPayload | null; radar?: OpenRadarPayload | null; intraday?: IntradayPayload | null
  errors: Record<string, string>; chg: Record<string, number>; res: Set<string> }

function GapCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const g = d.gaps
  const rows = g?.rows ?? []
  const body = d.errors.gaps ? <Err name="盘前跳空" /> : !g ? <Empty t="09:00" /> : rows.length === 0 ? <Empty t={g.warming ? '取数中' : '无'} /> : rows.map((r) => (
    <Row key={r.symbol} data-gap-row={r.symbol} symbol={r.symbol} onOpen={onOpen} mark={d.res.has(r.symbol)}
      main={<>{'跳空 '}<b className={sgn(r.gap_pct)}>{pc(r.gap_pct, 1)}</b>{r.role ? ` · ${r.role}` : ''}</>}
      right={pc(d.chg[r.symbol])} rightCls={sgn(d.chg[r.symbol])} />
  ))
  return { count: rows.length, meta: g ? `${etHm(g.at)} ET${g.live ? ' 实时' : ''}` : '', body, top: null as ReactNode }
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
  return { count: picks.length, meta: live ? `${etHm(r!.at)} ET` : '', body, top: null as ReactNode }
}

function IntradayCol({ d, onOpen }: { d: ColData; onOpen: (s: string) => void }) {
  const it = d.intraday
  const active = it?.active ?? []
  // 今日已结束 = 今天出场的卡 (含前几天出的卡今天止损 / 到期); exit_at 是美东墙钟 ISO, 前 10 位即美东日期
  const closedToday = (it?.history ?? []).filter((c) => (c.exit_at ? c.exit_at.slice(0, 10) : c.day) === it?.today)
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
  const meta = [it?.next_check ? `下次 ${it.next_check}` : '', st && !it?.live ? `已结算 ${st.n} · ${rS(st.sum_r)}` : ''].filter(Boolean).join(' · ')
  const top = it?.live && it.live.n > 0
    ? <div data-engine-live="intraday" className="border-b border-border/70 px-3 py-1.5 text-[12px] tabular-nums"><LiveLine live={it.live} /></div> : null
  return { count: active.length, meta, body, top }
}

// ── 策略机会: 唯一进入现行纸面下单链的卡片来源 ──
const GATE_ZH: Record<string, string> = { event: '齐备', state: '同向' }
const GATE_TITLE: Record<string, string> = { event: '4h 窗口内条件齐备', state: '4h 方向同向' }
const pct0 = (v?: number | null) => (typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v * 100)}%` : '—')

/** 每张卡一个结论: 下单 / 不下单 + 原因 (后端裁决), 紧跟在票和方向后面 (手机宽度不用横滑就能看到);
 * 页头只写这两类各几张, 再并排上线后实际成绩与回放值。 */
export function StrategyBoard({ swing, error, res, onOpen }: { swing?: SwingBoardsPayload | null; error?: string; res: Set<string>; onOpen: (s: string) => void }) {
  const cards = swing ? [...swing.long, ...swing.short].filter((c) => c.state !== 'holding') : []
  const meta = swing ? [`信号日 ${md(swing.as_of)}`, swing.refresh?.last_at ? `刷新 ${etHm(swing.refresh.last_at)}` : ''].filter(Boolean).join(' · ') : ''
  const verdicts = cards.map((c) => cardVerdict(c))
  const nOrder = verdicts.filter((v) => v.order).length
  const nSkip = verdicts.length - nOrder
  const live = swing?.live
  return (
    <Panel data-strategy-board data-source="strategy" className="mb-3 overflow-hidden border-accent/60">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-3 py-2.5">
        <span className="shrink-0 text-[11px] font-medium tracking-wide text-muted">策略机会</span>
        <h2 className="shrink-0 text-[15px] font-semibold text-foreground">机会板</h2>
        <span className="shrink-0 rounded-[4px] border border-border bg-elevated px-1.5 text-[11px] leading-5 text-secondary">3+3 策略命中</span>
        <span className="shrink-0 rounded-[4px] border border-border bg-elevated px-1.5 text-[11px] leading-5 text-secondary">4h 方向门</span>
        <span data-board-orders className="shrink-0 rounded-[4px] bg-accent px-1.5 text-[11px] font-semibold leading-5 text-white dark:bg-blue-700">{`下单 ${nOrder}`}</span>
        {nSkip > 0 && <span data-board-skips className="shrink-0 rounded-[4px] border border-warning/60 px-1.5 text-[11px] font-semibold leading-5 text-warning">{`不下单 ${nSkip}`}</span>}
        <span className="ml-auto whitespace-nowrap text-[12px] tabular-nums text-muted">{meta}</span>
      </div>
      {live && (
        <div data-board-live className="flex flex-wrap items-baseline gap-x-5 gap-y-1 border-t border-border/70 px-3 py-1.5 text-[12px] tabular-nums">
          {(['long', 'short'] as const).map((k) => (
            <LiveLine key={k} data-board-live-side={k} label={SIDE_ZH[k]} live={live[k]} stale={live[k].replay_stale}
              lead={`实际回区 ${live[k].n}/${live[k].cards}`} leadRef={live[k].replay_stale ? undefined : `回放 ${pct0(live[k].replay_fill)}`} />
          ))}
        </div>
      )}
      {error ? <Err name="机会板" /> : !swing ? <Empty t="—" /> : cards.length === 0 ? <Empty t="无" /> : (
        <div className="overflow-x-auto border-t border-border/70">
          <table className="w-full min-w-[1120px] text-[12.5px] tabular-nums">
            <thead><tr className="h-8 bg-elevated text-[11.5px] text-muted">
              {['股票', '方向', '结论', '命中策略', '4h 门', '入场区', '止损', '目标', '股数', '回区', '现价'].map((h, i) => <th key={h} className={`px-3 font-medium ${i === 0 || i === 2 || i === 3 ? 'text-left' : 'text-right'}`}>{h}</th>)}
            </tr></thead>
            <tbody>
              {cards.map((c, i) => {
                const v = verdicts[i]
                const stale = !!c.odds?.stale
                return (
                  <tr key={c.card_id ?? `${c.symbol}${c.side}`} role="button" tabIndex={0} data-board-row={c.symbol} data-verdict={v.order ? 'order' : 'skip'}
                    onClick={() => onOpen(c.symbol)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(c.symbol) } }}
                    className="h-11 cursor-pointer border-t border-border/70 first:border-t-0 hover:bg-elevated">
                    <td className={`px-3 text-left text-[14px] font-bold ${v.order ? 'text-foreground' : 'text-muted'}`}>{c.symbol}{res.has(c.symbol) && <i data-res-mark className="ml-1 inline-block h-1.5 w-1.5 -translate-y-0.5 rounded-full bg-accent" />}</td>
                    <td className={`px-3 text-right font-semibold ${c.side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[c.side]}</td>
                    <td data-board-verdict className="whitespace-nowrap px-3 text-left" title={v.title}>
                      <span className={`rounded-[4px] border px-1.5 text-[11px] font-semibold leading-5 ${v.order ? 'border-accent/50 text-accent' : 'border-warning/60 text-warning'}`}>{v.label}</span>
                      <span className={`ml-1.5 text-[12px] ${v.order ? 'text-secondary' : 'text-warning'}`}>{v.detail}</span>
                    </td>
                    <td className="max-w-[220px] truncate px-3 text-left text-secondary" title={c.strategies.map((s) => s.name).join(' · ')}>{c.strategies.map((s) => s.name).join(' · ') || '—'}</td>
                    <td data-board-gate className="px-3 text-right text-secondary" title={GATE_TITLE[c.gate ?? '']}>{GATE_ZH[c.gate ?? ''] ?? '—'}</td>
                    <td className="whitespace-nowrap px-3 text-right text-foreground">{`${p2(c.entry_zone[0])}–${p2(c.entry_zone[1])}`}</td>
                    <td className="px-3 text-right text-foreground">{p2(c.stop)}</td>
                    <td data-board-target className="px-3 text-right text-foreground">{p2(c.target)}</td>
                    <td className="px-3 text-right text-foreground">{c.odds?.qty ?? c.qty ?? '—'}</td>
                    <td data-board-fill className="px-3 text-right text-secondary" title={stale ? '回放表不是这张卡的出卡规则' : undefined}>{typeof c.odds?.fill === 'number' ? `${(c.odds.fill * 100).toFixed(0)}%` : '—'}</td>
                    <td className="px-3 text-right text-secondary">{p2(c.price)}</td>
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

type RadarEngine = Exclude<DeskEngine, 'board'>
const COLS: Record<RadarEngine, (p: { d: ColData; onOpen: (s: string) => void }) => { count: number; meta: string; body: ReactNode; top: ReactNode }> = {
  gap: GapCol, open: OpenCol, intraday: IntradayCol,
}

// 雷达三栏同一高度, 引擎共振为第四栏; 机会板独立成区, 不与只记录引擎混排。
export const COL_BODY = 'max-h-[320px] overflow-y-auto overscroll-y-contain pb-1 md:h-[320px]'

export function EngineCols({ order, d, desk, onOpen }: { order: DeskEngine[]; d: ColData; desk: OppDesk | null; onOpen: (s: string) => void }) {
  const radarOrder = order.filter((k): k is RadarEngine => k !== 'board')
  return (
    <section data-radar-section className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
        <span className="text-[11px] font-medium tracking-wide text-muted">盘中参考</span>
        <h2 className="text-[15px] font-semibold text-foreground">盘面雷达</h2>
        <span className="rounded-[4px] border border-border bg-elevated px-1.5 text-[11px] leading-5 text-secondary">只记录</span>
        <span className="rounded-[4px] border border-border bg-elevated px-1.5 text-[11px] leading-5 text-secondary">不含模型</span>
      </div>
      <div data-cols className="grid gap-3 border-t border-border/70 bg-base p-3 md:grid-cols-2 xl:grid-cols-4">
        {radarOrder.map((k) => {
          const c = COLS[k]({ d, onOpen })
          return (
            <Panel key={k} data-col={k} className="flex flex-col">
              <Head title={ENGINE_ZH[k]} count={c.count} meta={c.meta} />
              {c.top}
              <div data-col-body className={COL_BODY}>{c.body}</div>
            </Panel>
          )
        })}
        <ResonanceCol desk={desk} error={d.errors.desk} onOpen={onOpen} />
      </div>
    </section>
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
