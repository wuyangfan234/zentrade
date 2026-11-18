/** 机会页 B 方案 (2026-11-15): 今日机会卡 + 持仓卡, 每张配一把价格尺 (左亏右赚)。
 * 今日机会 = 机会板策略命中且后端裁决可进场的卡 (唯一进入模拟下单链的来源); 后端裁决不做的卡折成一行写原因。
 * 持仓 = 台账为准 (数量 / 成本 / 当前止损 / 第几天 / 退出动作); 现价跟报价推送刷新, 浮动盈亏按现价重算。
 * 模型打分只作「模型反向」提示, 不改方向、不合成分数。只展示: 交易环 / 准入 / 下单簿 / 台账不变。 */
import type { ReactNode } from 'react'
import type { LsRankPayload, OppCardCtx, OppQuote, OppRadarView, RadarBlip, SwingBoardCard, SwingBoardsPayload } from '@/lib/api'
import { cardVerdict, dayTag, md, SIDE_ARROW, setupKind, usdS, zonePos } from '@/components/opportunities/terms'
import { PriceRuler, RulerLegend } from '@/components/opportunities/PriceRuler'
import { badge } from '@/components/ui/badge'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
/** 美元按整数取色 (四舍五入为 0 = 平); 百分比按原值取色 */
const sgnCls = (v?: number | null) => (!fin(v) || Math.round(v) === 0 ? 'text-foreground' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const sgnPct = (v?: number | null) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const pc = (v?: number | null) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%` : '')
const px = (v?: number | null) => (fin(v) ? v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—')
const SRC_ZH: Record<string, string> = { snapshot: '快照', premarket: '盘前', close: '收盘', cost: '成本' }

type Quotes = Record<string, OppQuote>
/** 取报价 (没有就是 undefined; Record 下标类型不含 undefined, 这里显式写出来) */
const qOf = (m: Quotes, s: string): OppQuote | undefined => m[s]
export type ModelSide = Map<string, 'long' | 'short'>

/** 今日多空名单 → 票: 看多 / 看空 (只作「模型反向」提示) */
export function modelSides(d: LsRankPayload | null | undefined): ModelSide {
  const m: ModelSide = new Map()
  for (const r of d?.long ?? []) m.set(r.symbol, 'long')
  for (const r of d?.short ?? []) m.set(r.symbol, 'short')
  return m
}

function Side({ side }: { side: string }) {
  return <span data-side-label className="whitespace-nowrap text-[13px] font-semibold text-foreground">{SIDE_ARROW[side] ?? side}</span>
}

function Conflict({ model, sym, side }: { model: ModelSide; sym: string; side: string }) {
  const m = model.get(sym)
  if (!m || m === side) return null
  return <span data-model-conflict className={badge('warn')} title={`模型打分 ${m === 'long' ? '看涨' : '看跌'} · 仅参考 · 不改策略方向`}>模型反向</span>
}

export function Section({ title, count, tags, meta, children, hero, ...rest }: { title: string; count?: number; tags?: ReactNode; meta?: ReactNode; children: ReactNode; hero?: boolean }
  & Record<`data-${string}`, string | undefined>) {
  return (
    <section {...rest} className={`mb-3 min-w-0 overflow-hidden rounded-card border bg-surface ${hero ? 'border-accent/60' : 'border-border'}`}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-3 py-2.5">
        <h2 className={`shrink-0 whitespace-nowrap font-semibold text-foreground ${hero ? 'text-[18px]' : 'text-[14px]'}`}>{title}</h2>
        {count !== undefined && <span className="shrink-0 text-[13px] font-semibold tabular-nums text-secondary">{count}</span>}
        {tags}
        {meta && <span data-section-meta className="ml-auto min-w-0 text-right text-[12px] tabular-nums text-muted">{meta}</span>}
      </div>
      {children}
    </section>
  )
}

/** 同一网格的卡等高 (auto-rows-fr: 不只同一行, 换行后的各行也一样高); 卡内最后一行贴底, 多出的高度留在价格尺下方 */
export const GRID = 'grid auto-rows-fr gap-2.5 px-3 pb-3 [grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr))]'
export const CARD = 'flex min-h-[186px] min-w-0 cursor-pointer flex-col rounded-card border px-3 pb-2 pt-2.5 hover:bg-elevated'
export const CARD_FOOT = 'mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] tabular-nums text-secondary'
const Empty = ({ t }: { t: string }) => <div data-col-empty className="px-3 pb-3 text-[13px] text-muted">{t}</div>

/** 现价时点: 报价有时写报价时刻; 没有时按页面价的来源写 (收盘价写哪天) */
export function priceStamp(at: string | null, srcs: string[], asof?: string | null, generated?: string | null): string {
  if (at) {
    const live = srcs.filter((s) => s === 'live').length
    const lbl = live && live === srcs.length ? '实时' : srcs.includes('premarket') ? '盘前' : live ? '实时 · 部分快照' : '快照'
    return `现价 ${at.slice(11, 19)} ET · ${lbl}`
  }
  if (srcs.length && srcs.every((s) => s === 'close')) return asof ? `收盘价 ${md(asof)}` : '收盘价'
  return generated ? `现价 ${generated.slice(11, 16)} ET` : ''
}

// ───────────────────────── 今日机会 ─────────────────────────

function BoardCard({ c, q, model, ctx, nav, onOpen }: { c: SwingBoardCard; q?: OppQuote; model: ModelSide; ctx?: OppCardCtx; nav: number | null; onOpen: (s: string) => void }) {
  const price = q?.price ?? c.price ?? null
  const src = q?.src ?? c.price_source ?? null
  const chg = q?.chg ?? null
  const zp = zonePos(c.side, price, c.entry_zone)
  const [lo, hi] = c.entry_zone
  const mid = (lo + hi) / 2
  const qty = c.odds?.qty ?? c.qty ?? null
  const loss = fin(c.odds?.risk_usd) ? c.odds!.risk_usd! : fin(qty) ? qty * Math.abs(mid - c.stop) : null
  const gain = fin(qty) ? qty * Math.abs(c.target - mid) : null
  const hot = zp.pos === 'in'
  const kind = setupKind(c.strategies, c.side)
  const earn = ctx?.earnings
  return (
    <div role="button" tabIndex={0} data-board-card={c.symbol} data-zone={zp.pos ?? 'none'} onClick={() => onOpen(c.symbol)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(c.symbol) } }}
      className={`${CARD} ${hot ? 'border-accent bg-accent/[0.06] shadow-[inset_0_0_0_1px_hsl(var(--accent))]' : 'border-border'}`}>
      <div className="flex items-center gap-2">
        <b className="text-[14px] font-bold text-foreground">{c.symbol}</b>
        <Side side={c.side} />
        <span data-card-price className="ml-auto whitespace-nowrap text-[14px] font-semibold tabular-nums text-foreground">
          {px(price)}
          {chg !== null && <span className={`ml-1.5 text-[12px] ${sgnPct(chg)}`}>{pc(chg)}</span>}
          {src && src !== 'live' && SRC_ZH[src] && <span className="ml-1.5 text-[11px] font-medium text-muted">{SRC_ZH[src]}</span>}
        </span>
      </div>
      <div data-card-state className="mt-1 flex min-h-[22px] flex-wrap items-center gap-1.5">
        {hot ? <span className={badge('action')}>已到进场价</span>
          : zp.pos === 'past' ? <><span className={badge('warn')}>已穿过进场区</span><span className="text-[12px] text-warning">{`偏离 ${zp.gap!.toFixed(1)}%`}</span></>
            : <><span className={badge('status')}>等进场</span>{fin(zp.gap) && <span className="text-[12px] text-secondary">{`还差 ${zp.gap.toFixed(1)}%`}</span>}</>}
        <Conflict model={model} sym={c.symbol} side={c.side} />
        {earn && <span data-card-earn className={badge('warn')}>{`财报 ${md(earn.date)}`}</span>}
      </div>
      <PriceRuler data-ruler="board" geo={{ stop: c.stop, target: c.target, price, zone: c.entry_zone, side: c.side }}
        lossText={`止损亏 ${usdS(loss === null ? null : -loss)}`} gainText={`目标赚 ${usdS(gain)}`} lossCls="text-bear"
        stopLabel={`止损 ${px(c.stop)}`} targetLabel={`目标 ${px(c.target)}`} />
      <div data-card-foot className={CARD_FOOT}>
        {kind && <span data-card-kind title={c.strategies.map((s) => s.name).join(' · ')} className="rounded-[4px] bg-elevated px-1.5 text-[11px] leading-[18px] text-secondary">{kind}</span>}
        <span className="whitespace-nowrap">{`进场 ${px(lo)} ~ ${px(hi)}`}</span>
        <span className="whitespace-nowrap">{fin(qty) ? `${qty} 股` : '股数 —'}</span>
        {fin(qty) && fin(nav) && nav > 0 && <span className="whitespace-nowrap text-muted">{`占资金 ${Math.round((qty * mid) / nav * 100)}%`}</span>}
      </div>
    </div>
  )
}

export function BoardCards({ swing, error, quotes, quotesAt, model, ctx, nav, onOpen }: {
  swing?: SwingBoardsPayload | null; error?: string; quotes: Quotes; quotesAt: string | null; model: ModelSide; ctx: Record<string, OppCardCtx>
  nav: number | null; onOpen: (s: string) => void
}) {
  const cards = swing ? [...swing.long, ...swing.short].filter((c) => c.state !== 'holding') : []
  const vs = cards.map((c) => ({ c, v: cardVerdict(c) }))
  const act = vs.filter((x) => x.v.order).map((x) => x.c)
  // 同票同原因的多张卡只写一次
  const seen = new Set<string>()
  const skip = vs.filter((x) => !x.v.order).filter(({ c, v }) => { const k = `${c.symbol}${c.side}${v.detail}`; return !seen.has(k) && !!seen.add(k) })
  const inZone = act.filter((c) => zonePos(c.side, qOf(quotes, c.symbol)?.price ?? c.price, c.entry_zone).pos === 'in').length
  const srcs: string[] = act.map((c) => qOf(quotes, c.symbol)?.src ?? c.price_source ?? '').filter(Boolean)
  const asof = act.find((c) => c.price_asof)?.price_asof
  const closedPx = srcs.length > 0 && srcs.every((s) => s === 'close')
  const tags = (
    <>
      <span className={badge('limit')} title="机会板策略命中 · 只有这里进入模拟下单">策略选出</span>
      {act.length > 0 && <RulerLegend kind="board" closed={closedPx} />}
    </>
  )
  return (
    <Section data-board title="今日机会" count={act.length} hero tags={tags} meta={swing ? priceStamp(quotesAt, srcs, asof, swing.generated_at) : ''}>
      {error ? <div data-col-error className="px-3 pb-3 text-[13px] text-danger">机会板读取失败</div> : !swing ? <Empty t="—" /> : (
        <>
          {act.length === 0 ? <Empty t="无可进场机会" /> : (
            <div data-board-grid data-in-zone={inZone} className={GRID}>
              {act.map((c) => <BoardCard key={c.card_id ?? `${c.symbol}${c.side}`} c={c} q={quotes[c.symbol]} model={model} ctx={ctx[c.symbol]} nav={nav} onOpen={onOpen} />)}
            </div>
          )}
          {skip.length > 0 && (
            <div data-board-skips className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-border/70 px-3 py-2 text-[13px]">
              <span className={badge('limit')}>{`不做 ${skip.length}`}</span>
              {skip.map(({ c, v }) => (
                <button key={`${c.symbol}${c.side}${v.detail}`} type="button" data-skip={c.symbol} title={v.title} onClick={() => onOpen(c.symbol)}
                  className="inline-flex items-baseline gap-1.5 whitespace-nowrap rounded-btn px-1 hover:bg-elevated">
                  <b className="text-foreground">{c.symbol}</b>
                  <span className="text-[12px] text-secondary">{SIDE_ARROW[c.side]}</span>
                  <span className="text-[12px] text-muted">{v.detail}</span>
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
  )
}

// ───────────────────────── 我的持仓 ─────────────────────────

type Hold = RadarBlip & { hold: NonNullable<RadarBlip['hold']> }

/** 持仓的处理提醒 (台账事实 + 现价): 到期平仓 / 开盘平仓 / 越过止损 / 近止损 / 财报减仓 / 保本 / 锁盈。
 * 近止损 = 距当前止损 <= 0.5 ATR (与雷达同口径); 现价变了按「ATR 距离 × 现价距止损 / 页面价距止损」换算; 止损已在成本或更好时不提示。 */
export function holdTags(b: Hold, price: number | null, today: string): { warn: string[]; ok: string[] } {
  const h = b.hold
  const sg = b.side === 'short' ? -1 : 1
  const warn: string[] = [], ok: string[] = []
  const tag = (day?: string | null) => (day ? dayTag(day, today) || '今天' : '次日')
  if (h.exit_code === 'exit_next_open') warn.push(`${tag(h.exit_open_day)}开盘平仓`)
  else if (h.exit_code === 'exit_watch_removed') warn.push(`移出自选 · ${tag(h.exit_open_day)}开盘平仓`)
  else if (b.kind === 'exit' && h.exit_day) warn.push(`${tag(h.exit_day)} ${h.exit_at ?? ''} 到期平仓`.replace(/\s+/g, ' ').trim())
  const even = fin(h.stop_now) && h.avg > 0 && Math.abs(h.stop_now - h.avg) / h.avg < 0.0005
  const locked = !even && fin(h.stop_now) && sg * (h.stop_now - h.avg) > 0
  let atr = h.stop_dist_atr
  if (fin(atr) && fin(price) && fin(h.price) && fin(h.stop_now) && Math.abs(h.price - h.stop_now) > 1e-9) atr = atr * Math.abs(price - h.stop_now) / Math.abs(h.price - h.stop_now)
  if (fin(price) && fin(h.stop_now) && sg * (price - h.stop_now) < 0) warn.push(b.side === 'short' ? '已涨破止损' : '已跌破止损')
  else if (fin(atr) && atr <= 0.5 && !even && !locked) warn.push('近止损')
  if (h.earnings) warn.push(`财报减仓 ${md(h.earnings.reduce_day)}`)
  if (even) ok.push('保本')
  else if (locked) ok.push('锁盈')
  return { warn, ok }
}

export function holdRows(view: OppRadarView, quotes: Quotes) {
  const today = view.clock.et.slice(0, 10)
  return view.blips.filter((b): b is Hold => !!b.hold).map((b) => {
    const h = b.hold
    const sg = b.side === 'short' ? -1 : 1
    const price = qOf(quotes, b.symbol)?.price ?? h.price ?? null
    const pnl = fin(price) ? sg * (price - h.avg) * h.qty : h.pnl_usd
    const t = holdTags(b, price, today)
    return { b, price, src: (qOf(quotes, b.symbol)?.src ?? h.price_source ?? null) as string | null, pnl, tags: t, urgent: t.warn.length > 0 }
  }).sort((x, y) => Number(y.urgent) - Number(x.urgent) || (x.pnl ?? 0) - (y.pnl ?? 0))
}

export function HoldCards({ view, swing, quotes, quotesAt, model, onOpen }: {
  view: OppRadarView; swing?: SwingBoardsPayload | null; quotes: Quotes; quotesAt: string | null; model: ModelSide; onOpen: (s: string) => void
}) {
  const rows = holdRows(view, quotes)
  const total = rows.reduce((s, r) => s + (r.pnl ?? 0), 0)
  const cardOf = new Map([...(swing?.long ?? []), ...(swing?.short ?? [])].filter((c) => c.state === 'holding').map((c) => [c.symbol, c]))
  const srcs: string[] = rows.map((r) => r.src ?? '').filter(Boolean)
  const asof = rows.find((r) => r.b.hold.price_asof)?.b.hold.price_asof
  const closedPx = srcs.length > 0 && srcs.every((s) => s === 'close')
  return (
    <Section data-holdings title="我的持仓" count={rows.length} tags={rows.length > 0 ? <RulerLegend kind="hold" closed={closedPx} /> : undefined}
      meta={rows.length ? <>{'浮动 '}<b data-hold-total className={`font-semibold ${sgnCls(total)}`}>{usdS(total)}</b>{` · ${priceStamp(quotesAt, srcs, asof)}`}</> : ''}>
      {rows.length === 0 ? <Empty t="无持仓" /> : (
        <div className={GRID}>
          {rows.map(({ b, price, src, pnl, tags }) => {
            const h = b.hold
            const sg = b.side === 'short' ? -1 : 1
            const stopPnl = fin(h.stop_now) ? sg * (h.stop_now - h.avg) * h.qty : null
            const tgtPnl = fin(h.target) ? sg * (h.target - h.avg) * h.qty : null
            const kind = setupKind(cardOf.get(b.symbol)?.strategies, b.side ?? 'long')
            const r0 = stopPnl === null ? null : Math.round(stopPnl)
            const lossText = r0 === null ? '止损 —' : r0 < 0 ? `止损亏 ${usdS(stopPnl)}` : r0 === 0 ? '止损亏 $0' : `止损赚 ${usdS(stopPnl)}`
            return (
              <div key={b.symbol} role="button" tabIndex={0} data-hold={b.symbol} data-urgent={tags.warn.length ? '' : undefined} onClick={() => onOpen(b.symbol)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(b.symbol) } }}
                className={`${CARD} ${tags.warn.length ? 'border-warning/50' : 'border-border'}`}>
                <div className="flex items-center gap-2">
                  <b className="text-[14px] font-bold text-foreground">{b.symbol}</b>
                  <Side side={b.side ?? 'long'} />
                  <b data-hold-pnl className={`ml-auto whitespace-nowrap text-[18px] font-semibold tabular-nums ${sgnCls(pnl)}`}>{usdS(pnl)}</b>
                </div>
                <div data-hold-tags className="mt-1 flex min-h-[22px] flex-wrap items-center gap-1.5">
                  {tags.warn.map((t) => <span key={t} className={badge('warn')}>{t}</span>)}
                  {tags.ok.map((t) => <span key={t} className={badge('status', 'neutral')}>{t}</span>)}
                  <Conflict model={model} sym={b.symbol} side={b.side ?? 'long'} />
                </div>
                {fin(h.stop_now) ? (
                  <PriceRuler data-ruler="hold" geo={{ stop: h.stop_now, target: h.target, price, cost: h.avg, side: b.side ?? 'long' }}
                    lossText={lossText} lossCls={r0 !== null && r0 < 0 ? 'text-bear' : 'text-muted'}
                    gainText={tgtPnl === null ? '目标 —' : `目标赚 ${usdS(tgtPnl)}`}
                    stopLabel={<>{`止损 ${px(h.stop_now)}`}{h.stop_moved && <span data-stop-initial className="text-muted">{` · 原 ${px(h.stop_initial)}`}</span>}</>}
                    targetLabel={`目标 ${px(h.target)}`} />
                ) : <div className="my-2 text-[12px] text-muted">止损 —</div>}
                <div data-card-foot className={CARD_FOOT}>
                  {kind && <span className="rounded-[4px] bg-elevated px-1.5 text-[11px] leading-[18px] text-secondary">{kind}</span>}
                  <span className="whitespace-nowrap">{`${h.qty} 股`}</span>
                  <span className="whitespace-nowrap">{`成本 ${px(h.avg)}`}</span>
                  <span className="whitespace-nowrap">{`${src === 'close' ? '收盘' : '现价'} ${px(price)}`}</span>
                  <span data-hold-days className="ml-auto inline-flex items-center gap-1.5 whitespace-nowrap">
                    {`第 ${h.day_n} 天 / 最多 ${h.max_days}`}
                    <span className="inline-flex gap-[3px]">
                      {Array.from({ length: Math.max(h.max_days, 0) }, (_, i) => <i key={i} className={`h-[7px] w-[7px] rounded-full border ${i < h.day_n ? 'border-secondary bg-secondary' : 'border-muted'}`} />)}
                    </span>
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Section>
  )
}
