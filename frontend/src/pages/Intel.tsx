/**
 * 盘面情报 (0.40.0, 10-26 产品决策 B 档): 当日市场概览 + 宏观日历 (含实际值) + 自选财报 + 自选新闻。
 * 只读展示, 不参与打分 / 裁决 / 下单。数据一次取齐 (GET /api/intel/today, 只读缓存);
 * 盘前 / 盘中 60 秒刷新, 其余 5 分钟。每格标来源与时点, 不是今天的数据标「非今日」。
 */
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, RefreshCw, X } from 'lucide-react'
import {
  api, type IntelBreadth, type IntelCalendar, type IntelEarningsRow, type IntelEvent, type IntelNewsItem,
  type IntelPayload, type IntelQuote,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { NewsRow } from '@/components/StockNews'
import { StockPreviewDialog } from '@/components/StockPreviewDialogLazy'
import { etToday } from '@/lib/etTime'
import { corpActionLabel } from '@/lib/reasonLabels'
import { eventTitleZh } from '@/lib/macroEvents'
import { DIR_ZH, EVENT_ZH, SentimentBoard } from '@/components/intel/SentimentBoard'
import { GridCard, Lead, LEAD_SUB, Missing, ModelBody, ModelSheet, challengerBadge } from '@/components/intel/IntelGrid'
import { Modal } from '@/components/Modal'
import { badge } from '@/components/ui/badge'

const ACTIVE = new Set(['premarket', 'open_wait', 'session'])
const WRAP = 'mx-auto max-w-[1440px] space-y-3 px-4 pb-24 pt-3 md:px-6 md:pb-6'
/** 六宫格里的日历 / 新闻: 不再自带边框和固定高, 由卡片给高度; 头部筛选不动, 只有列表在卡内滚 */
export const EMBED_CARD = 'flex min-h-0 flex-1 flex-col'
export const EMBED_BODY = 'min-h-0 flex-1 overflow-y-auto scroll-y-visible pr-1.5'
const CARD = 'rounded-card border border-border bg-surface'
/**
 * 日历 / 新闻并排两卡 (0.45.0): 宽屏同一固定高度, 头部筛选与底部按钮不动, 只有中间列表在卡内滚动;
 * 窄屏整页滚动, 列表本身封顶 520px (与新闻原样一致), 卡片随内容收缩。
 */
export const PAIR_CARD = `${CARD} flex flex-col px-3 py-2 lg:h-[640px]`
export const PAIR_BODY = 'max-h-[520px] min-h-0 overflow-y-auto scrollbar-gutter-stable pr-1.5 lg:max-h-none lg:flex-1'
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const IMPACT_ZH: Record<string, string> = { high: '高', medium: '中', low: '低' }
const TIMING_ZH: Record<string, string> = { bmo: '盘前', amc: '盘后', unknown: '时间未定' }
const STANCE: Record<string, string> = { up: '偏多', down: '偏空', range: '观望' }

export const sgnCls = (v?: number | null) => (v == null ? 'text-muted' : v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-secondary')
export const fmtPct = (v?: number | null, dp = 2) => {
  if (v == null) return '—'
  const t = Math.abs(v).toFixed(dp)
  return Number(t) === 0 ? `${(0).toFixed(dp)}%` : `${v > 0 ? '+' : '-'}${t}%`
}
export const fmtUsd = (v?: number | null) => (v == null ? '—' : `${v < 0 ? '-' : ''}$${Math.abs(v).toFixed(2)}`)
const md = (s?: string | null) => (s ? s.slice(5, 10) : '—')
const weekday = (day: string) => WEEK[new Date(`${day}T12:00:00Z`).getUTCDay()]

/** 盘中 60 秒, 其余 5 分钟 */
export const intelPollMs = (d?: IntelPayload) => (ACTIVE.has(d?.phase?.phase ?? '') ? 60_000 : 300_000)

export const NOT_ARCHIVED = 'not_archived'
type CardKey = 'tape' | 'breadth' | 'calendar' | 'news' | 'earnings'
const EXPAND_TITLE: Record<CardKey, string> = { tape: '市场概览', breadth: '市场概览', calendar: '宏观与事件日历', news: '新闻与舆情', earnings: '自选财报' }

/** 历史日下拉的一项: 「11-10 周二」 */
export const asofLabel = (day: string) => `${md(day)} ${weekday(day)}`

export function Intel() {
  const qc = useQueryClient()
  // 截至: null = 今天 (轮询); 日期 = 历史日 (只读当天冻结的那份, 不轮询)
  const [asof, setAsof] = useState<string | null>(null)
  const [showPost, setShowPost] = useState(false)
  const [expand, setExpand] = useState<CardKey | null>(null)
  const [modelOpen, setModelOpen] = useState(false)
  const q = useQuery<IntelPayload>({
    queryKey: QK.intelToday, queryFn: api.intelToday, enabled: asof === null,
    refetchInterval: (qq) => intelPollMs(qq.state.data as IntelPayload | undefined), refetchIntervalInBackground: false,
  })
  const days = useQuery({ queryKey: QK.intelAsofDays, queryFn: api.intelAsofDays, staleTime: 300_000 })
  const qa = useQuery<IntelPayload>({
    queryKey: QK.intelAsof(asof ?? ''), queryFn: () => api.intelAsof(asof!), enabled: asof !== null, staleTime: 600_000,
  })
  const refresh = useMutation({ mutationFn: api.intelRefresh, onSuccess: () => qc.invalidateQueries({ queryKey: QK.intelToday }) })
  const [preview, setPreview] = useState<string | null>(null)
  const cur = asof === null ? q : qa
  const d = cur.data
  const errs = d?.errors ?? {}
  const dayList = days.data?.days ?? []
  const hist = asof !== null
  const na = (k: string) => errs[k] === NOT_ARCHIVED
  const bad = (k: string) => k in errs && !na(k)
  /** 一张卡的明细: 未存档 / 读取失败 / 正常 */
  const body = (k: string, node: () => ReactNode) => (na(k) ? <Missing /> : bad(k) ? <Failed /> : node())
  /** 结论行: 未存档时不给数 (0 会被读成「确认没有」) */
  const lead = (k: string, node: ReactNode) => (na(k) ? <Lead big="—"><span data-lead-missing className={LEAD_SUB}>当天未存档</span></Lead> : node)
  const post = (k: 'tape' | 'news' | 'model') => (hist && showPost ? d?.post?.[k] ?? [] : null)
  const noPost = hist && showPost ? [] : null

  return (
    <div data-page="intel" data-intel-mode={hist ? 'asof' : 'today'} className="overflow-x-hidden">
      <PageHeader
        title="情报"
        subtitle={hist
          ? `历史日 · ${asofLabel(asof!)} 收盘 · 仅观察`
          : d ? `${d.phase?.label ?? ''} · ${(d.generated_at ?? '').slice(5, 16).replace('T', ' ')} ET · 自选 ${d.watch_count ?? '—'} 只 · 仅观察` : '加载中'}
        right={(
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-secondary">截至</span>
            <div data-asof-seg role="radiogroup" aria-label="截至" className="inline-flex rounded-btn border border-border p-0.5 text-xs">
              <button type="button" role="radio" aria-checked={!hist} data-asof-tab="today" onClick={() => setAsof(null)}
                className={`rounded px-2 py-0.5 ${!hist ? 'bg-accent/20 font-medium text-foreground ring-1 ring-accent/50' : 'text-secondary hover:text-foreground'}`}>今天</button>
              <button type="button" role="radio" aria-checked={hist} data-asof-tab="asof" disabled={dayList.length === 0}
                onClick={() => !hist && dayList.length > 0 && setAsof(dayList[0])}
                className={`rounded px-2 py-0.5 disabled:opacity-60 ${hist ? 'bg-accent/20 font-medium text-foreground ring-1 ring-accent/50' : 'text-secondary hover:text-foreground'}`}>历史日</button>
            </div>
            {hist ? (
              <>
                <select data-asof-day aria-label="历史日" value={asof!} onChange={(e) => setAsof(e.target.value)}
                  className="h-7 rounded-btn border border-border bg-surface px-1.5 text-xs tabular-nums text-foreground">
                  {dayList.map((x) => <option key={x} value={x}>{asofLabel(x)}</option>)}
                </select>
                <button type="button" data-post-toggle aria-pressed={showPost} onClick={() => setShowPost((x) => !x)}
                  className={`inline-flex items-center rounded-btn border px-2.5 py-1 text-xs ${showPost ? 'border-warning/60 text-warning' : 'border-border text-secondary hover:bg-elevated'}`}>
                  {showPost ? '事后 · 开' : '事后 · 关'}
                </button>
              </>
            ) : (
              <button type="button" data-intel-refresh disabled={refresh.isPending} onClick={() => refresh.mutate()}
                className="inline-flex items-center gap-1.5 rounded-btn border border-border px-2.5 py-1 text-xs text-secondary hover:bg-elevated disabled:opacity-60">
                <RefreshCw className={`h-3.5 w-3.5 ${refresh.isPending ? 'animate-spin' : ''}`} />
                {refresh.isPending ? '刷新中' : '立即刷新'}
              </button>
            )}
          </div>
        )}
      />
      <div data-intel-wrap className={WRAP}>
        {cur.isError && <div className={`${CARD} p-4 text-sm text-warning`}>读取失败 · {String((cur.error as Error)?.message ?? '')}</div>}
        {hist && (
          <div data-asof-banner className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-warning/55 px-3 py-1.5 text-xs text-secondary">
            <span className={badge('warn')}>历史日</span>
            <b className="font-semibold text-foreground">{`${asofLabel(asof!)} 收盘`}</b>
            <span>{d?.asof?.archived ? `各卡同一截止 · 存档于 ${(d.asof.archived_at ?? '').slice(5, 16).replace('T', ' ')} ET` : d ? '当天情报未存档 · 只有模型状态' : '读取中'}</span>
            <span>· 之后才知道的只放「事后」</span>
          </div>
        )}
        {d && (
          <>
            <div data-intel-grid className="intel-grid" style={{ '--intel-chrome': hist ? '192px' : '148px' } as React.CSSProperties}>
              <GridCard k="tape" title="大盘方向" lead={lead('tape', <TapeLead d={d} />)} post={post('tape')} onExpand={na('tape') ? undefined : () => setExpand('tape')}>
                {body('tape', () => <TapeBody d={d} />)}
              </GridCard>
              <GridCard k="breadth" title="宽度与板块" lead={lead('breadth', <BreadthLead d={d} />)} post={noPost} onExpand={na('tape') ? undefined : () => setExpand('breadth')}
                tags={d.breadth && <span data-breadth-asof className="truncate text-[11px] text-muted">{`宽度 ${md(d.breadth.as_of)} 收盘 · ${d.breadth.universe} 只`}</span>}>
                {na('breadth') && na('tape') ? <Missing /> : <BreadthBody d={d} />}
              </GridCard>
              <GridCard k="calendar" title="宏观日历" lead={lead('calendar', <CalendarLead cal={d.calendar} />)} post={noPost} scroll={false}
                onExpand={na('calendar') ? undefined : () => setExpand('calendar')}>
                {body('calendar', () => <Calendar cal={d.calendar} level={d.settings.macro_impact} onSymbol={setPreview} embedded readOnly={hist} />)}
              </GridCard>
              <GridCard k="news" title="新闻与舆情" lead={lead('news', <NewsLead d={d} onSymbol={setPreview} />)} post={post('news')} scroll={false}
                tags={<span className="text-[11px] text-muted">{`${d.news?.hours ?? 36} 小时`}</span>} onExpand={na('news') ? undefined : () => setExpand('news')}>
                {body('news', () => <News news={d.news} onSymbol={setPreview} embedded />)}
              </GridCard>
              <GridCard k="earnings" title="自选财报" lead={lead('earnings', <EarningsLead rows={d.earnings?.rows ?? []} />)} post={noPost}
                tags={hotEarnings(d.earnings?.rows ?? []) > 0 && <span data-earn-hot className={badge('warn')}>{`隐含 ≥ 1.25× 历史 · ${hotEarnings(d.earnings?.rows ?? [])} 只`}</span>}
                onExpand={na('earnings') ? undefined : () => setExpand('earnings')}>
                {body('earnings', () => (
                  <>
                    <Earnings rows={d.earnings?.rows ?? []} onSymbol={setPreview} compact />
                    {(d.actions?.length ?? 0) > 0 && <div data-intel-actions className="mt-auto border-t border-border/70 pt-2 text-xs text-secondary">{actionsText(d)}</div>}
                  </>
                ))}
              </GridCard>
              <GridCard k="model" title="模型状态" tags={<span className={badge('limit')}>仅参考</span>} post={post('model')}
                lead={d.model ? <Lead big={d.model.champion.name}><span className={LEAD_SUB}>冠军</span><span className="self-center">{challengerBadge(d.model)}</span></Lead> : <Lead big="—" />}
                onExpand={d.model ? () => setModelOpen(true) : undefined} onBodyClick={d.model ? () => setModelOpen(true) : undefined}>
                {bad('model') ? <Failed /> : d.model ? <ModelBody m={d.model} /> : <Missing label={hist ? '当天无名单' : '尚无名单'} />}
              </GridCard>
            </div>
            <SourceState d={d} />
          </>
        )}
      </div>
      {expand && d && (
        <Modal onClose={() => setExpand(null)} ariaLabel={EXPAND_TITLE[expand]}
          panelClassName="flex max-h-[90dvh] w-[96vw] max-w-5xl flex-col rounded-card border border-border bg-base shadow-xl">
          <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-2.5">
            <h2 className="text-[14px] font-semibold text-foreground">{EXPAND_TITLE[expand]}{hist ? ` · 历史日 ${md(asof)}` : ''}</h2>
            <button type="button" aria-label="关闭" data-expand-close onClick={() => setExpand(null)} className="flex h-8 w-8 items-center justify-center rounded-btn text-secondary hover:bg-elevated hover:text-foreground">
              <X className="h-4 w-4" />
            </button>
          </div>
          <div data-expand-body={expand} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {(expand === 'tape' || expand === 'breadth') && <Tape d={d} />}
            {expand === 'calendar' && <Calendar cal={d.calendar} level={d.settings.macro_impact} onSymbol={setPreview} readOnly={hist} />}
            {expand === 'news' && (
              <>
                <SentimentBoard data={d.sentiment} failed={bad('sentiment')} onSymbol={setPreview} />
                <News news={d.news} onSymbol={setPreview} />
              </>
            )}
            {expand === 'earnings' && (
              <>
                <Earnings rows={d.earnings?.rows ?? []} onSymbol={setPreview} />
                {(d.actions?.length ?? 0) > 0 && <div className="text-xs text-secondary">{actionsText(d)}</div>}
                <p className="text-[11px] text-muted">{d.earnings?.source}{d.earnings?.fetched_at ? ` · 预期取于 ${md(d.earnings.fetched_at)} ${d.earnings.fetched_at.slice(11, 16)} ET` : ''}</p>
              </>
            )}
          </div>
        </Modal>
      )}
      {modelOpen && d?.model && <ModelSheet m={d.model} onClose={() => setModelOpen(false)} />}
      <StockPreviewDialog symbol={preview} onClose={() => setPreview(null)} />
    </div>
  )
}

const actionsText = (d: IntelPayload) =>
  `公司行动 · ${(d.actions ?? []).map((a) => `${a.symbol} ${md(a.date)} ${a.type === 'cash_dividend' ? `除息 $${a.rate != null ? a.rate.toFixed(2) : ''}/股` : corpActionLabel(a.type)}`).join(' · ')}`

/** 隐含波幅 ÷ 历史反应中位数 */
export const impliedRatio = (r: IntelEarningsRow) => {
  const imp = r.implied?.expected_move_pct
  return imp != null && r.hist?.median_abs_move ? imp / r.hist.median_abs_move : null
}
const EARN_DETAIL_MAX = 4
export const hotEarnings = (rows: IntelEarningsRow[]) => rows.filter((r) => (impliedRatio(r) ?? 0) >= 1.25).length

// ── 六宫格: 结论行 ─────────────────────────────────────────────────

function TapeLead({ d }: { d: IntelPayload }) {
  const mood = d.mood?.judgment
  if (!d.mood || !mood) return <Lead big="—"><span className={LEAD_SUB}>无大盘判断</span></Lead>
  const stale = !d.mood.is_today && !d.asof
  return (
    <Lead big={STANCE[mood.stance] ?? '—'} cls={mood.stance === 'up' ? 'text-bull' : mood.stance === 'down' ? 'text-bear' : 'text-foreground'}>
      <span data-mood-at className={`${LEAD_SUB} ${stale ? 'text-warning' : ''}`}>{`08:35 判断${stale ? ` · ${md(mood.day)} 非当日` : ''}`}</span>
      <span className={LEAD_SUB}>{`近 ${d.mood.stats.n} 日判对 ${d.mood.stats.hit}`}</span>
    </Lead>
  )
}

function BreadthLead({ d }: { d: IntelPayload }) {
  const b = d.breadth
  if (!b) return <Lead big="—"><span className={LEAD_SUB}>无宽度数据</span></Lead>
  return (
    <Lead big={`${b.up_pct?.toFixed(0) ?? '—'}%`} cls={sgnCls((b.up_pct ?? 50) - 50) === 'text-secondary' ? 'text-foreground' : sgnCls((b.up_pct ?? 50) - 50)}>
      <span className={LEAD_SUB}>上涨家数</span>
      <span className={LEAD_SUB}>{`新高 ${b.new_highs} / 新低 ${b.new_lows}`}</span>
      {b.live && <span data-breadth-live className={LEAD_SUB}>{`盘中近似 ${b.live.up_pct.toFixed(0)}%`}</span>}
    </Lead>
  )
}

/** 当天高重要度宏观事件数 + 下一件还没发生的高重要度事件 */
export function calendarLead(cal: IntelCalendar | null) {
  const macro = (cal?.events ?? []).filter((e) => e.kind === 'macro' || e.kind == null)
  const high = macro.filter((e) => e.impact === 'high')
  return { today: high.filter((e) => e.day === cal?.today).length, next: high.find((e) => !e.past && e.day >= (cal?.today ?? '')) ?? null }
}

function CalendarLead({ cal }: { cal: IntelCalendar | null }) {
  if (!cal) return <Lead big="—"><span className={LEAD_SUB}>无日历数据</span></Lead>
  const { today, next } = calendarLead(cal)
  return (
    <Lead big={today}>
      <span className={LEAD_SUB}>件高重要度 · 当天</span>
      {next && <span data-cal-next className="min-w-0 basis-full truncate text-xs text-secondary" title={next.title}>{`下一件 ${md(next.day)} ${next.time || '全天'} ${next.title_zh ?? eventTitleZh(next.title)}`}</span>}
    </Lead>
  )
}

/** 结论行第二行列几只舆情异动 (其余记 +N, 全量在「展开」) */
const SENT_CHIPS = 3

function NewsLead({ d, onSymbol }: { d: IntelPayload; onSymbol: (s: string) => void }) {
  const rows = d.sentiment?.rows ?? []
  const focus = rows.filter((r) => r.grade === 'A').length
  return (
    <Lead big={d.sentiment ? d.sentiment.total : '—'}>
      <span className={LEAD_SUB}>只舆情异动</span>
      <span data-sent-focus className={`${LEAD_SUB} ${focus ? 'text-warning' : ''}`}>{`重点 ${focus}`}</span>
      <span data-sent-chips className="flex min-w-0 basis-full gap-x-3 overflow-hidden text-xs text-secondary">
        {rows.slice(0, SENT_CHIPS).map((r) => (
          <button key={r.symbol} type="button" data-sent-chip={r.symbol} onClick={() => onSymbol(r.symbol)}
            title={r.event_type ? (EVENT_ZH[r.event_type] ?? '其他消息') : undefined}
            className={`shrink-0 whitespace-nowrap hover:text-accent ${r.grade === 'A' ? 'text-warning' : ''}`}>
            <b className={r.grade === 'A' ? 'font-semibold' : 'font-semibold text-foreground'}>{r.symbol}</b>{` ${DIR_ZH[r.direction] ?? '方向不明'}`}
          </button>
        ))}
        {rows.length > SENT_CHIPS && <span data-sent-more className="shrink-0 whitespace-nowrap text-muted">{`+${rows.length - SENT_CHIPS}`}</span>}
      </span>
    </Lead>
  )
}

function EarningsLead({ rows }: { rows: IntelEarningsRow[] }) {
  const first = rows[0]
  return (
    <Lead big={rows.length}>
      <span className={LEAD_SUB}>只 · 10 个交易日内</span>
      {first && <span data-earn-next className="min-w-0 basis-full truncate text-xs text-secondary">{`最近 ${first.symbol} ${md(first.date)} ${TIMING_ZH[first.timing] ?? ''} · ${first.days === 0 ? '当天' : `T+${first.days}`}`}</span>}
    </Lead>
  )
}

// ── 六宫格: 明细 ───────────────────────────────────────────────────

function Gauge({ k, label, sub, children, warn }: { k: string; label: string; sub?: ReactNode; children: ReactNode; warn?: boolean }) {
  return (
    <div data-intel-cell={k} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-1.5 rounded-btn border border-border px-2 py-1">
      <span className="truncate text-[11px] text-secondary">{label}</span>
      <span className={`whitespace-nowrap text-right text-[11px] tabular-nums ${warn ? 'text-warning' : 'text-muted'}`}>{sub}</span>
      <span className="col-span-2 whitespace-nowrap text-[14px] font-bold tabular-nums text-foreground">{children}</span>
    </div>
  )
}

function TapeBody({ d }: { d: IntelPayload }) {
  const today = d.asof?.day ?? etToday()
  const v = d.vol
  const ratioWarn = (v?.ratio ?? 0) > 1.05
  const bp = (x?: number | null) => (x == null ? '' : ` ${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x)}bp`)
  return (
    <>
      <div data-intel-indexes className="flex min-h-0 flex-1 flex-col">
        {(d.tape?.indexes ?? []).map((r) => {
          const stale = !r.live && r.day !== today && ACTIVE.has(d.phase?.phase ?? '')
          return (
            <div key={r.symbol} data-intel-cell={r.symbol} className="grid min-h-7 flex-1 grid-cols-[auto_minmax(0,1fr)_4.25rem_3.75rem] items-center gap-x-2 border-t border-border/55 text-[13px] first:border-t-0 md:max-h-14">
              <span className="whitespace-nowrap text-foreground">{r.name} <span className="text-xs text-muted">{r.symbol}</span></span>
              <span data-quote-sub title={quoteSub(r, today)} className={`truncate text-[11px] ${stale ? 'text-warning' : 'text-muted'}`}>{quoteSubShort(r, today)}</span>
              <span data-price className="text-right font-semibold tabular-nums">{r.price != null ? r.price.toFixed(2) : '—'}</span>
              <span className={`text-right font-semibold tabular-nums ${sgnCls(r.chg_pct)}`}>{fmtPct(r.chg_pct)}</span>
            </div>
          )
        })}
      </div>
      <div data-intel-gauges className="grid shrink-0 grid-cols-2 gap-1.5 pt-2">
        <Gauge k="vix" label="VIX" warn={ratioWarn} sub={v?.ratio != null ? `比 VIX3M ${v.ratio.toFixed(2)}${ratioWarn ? ' 倒挂' : ''}` : '延迟 15 分'}>
          {v ? <>{v.vix.value.toFixed(2)} <span className={`text-xs font-medium ${sgnCls(v.vix.chg_pct)}`}>{fmtPct(v.vix.chg_pct)}</span></> : '—'}
        </Gauge>
        <Gauge k="rates" label="美债 10Y" sub={d.rates ? md(d.rates.as_of) : '—'}>
          {d.rates?.us10y != null ? <>{`${d.rates.us10y.toFixed(2)}%`}<span className="text-xs font-medium text-secondary">{bp(d.rates.us10y_chg_bp)}</span></> : '—'}
        </Gauge>
        <Gauge k="pcr" label="个股 Put/Call" sub={d.pcr ? md(d.pcr.day) : '—'}>
          {d.pcr?.equity != null ? <>{d.pcr.equity.toFixed(2)}<span className="text-xs font-medium text-secondary">{` 20日均 ${d.pcr.equity_avg20?.toFixed(2) ?? '—'}`}</span></> : '—'}
        </Gauge>
        <Gauge k="hy" label="高收益债利差" sub={d.rates?.hy_as_of ? md(d.rates.hy_as_of) : '—'}>
          {d.rates?.hy_oas != null ? <>{`${d.rates.hy_oas.toFixed(2)}%`}<span className="text-xs font-medium text-secondary">{bp(d.rates.hy_chg_bp)}</span></> : '—'}
        </Gauge>
      </div>
    </>
  )
}

/** 六宫格指数行的副文字: 只留时段与时刻 (「对官方前收」在悬停里); 不是当天的照旧写明 */
export function quoteSubShort(r: IntelQuote, today: string) {
  if (r.live) return r.session ? `${SESSION_ZH[r.session] ?? ''}${r.time_et ? ` ${r.time_et}` : ''}` : '实时'
  return quoteSub(r, today)
}

/** 六宫格里的色块: 一行 (名称 + 当日涨跌); 5 日与代码在悬停和「展开」里 */
function HeatMini({ r }: { r: IntelQuote }) {
  return (
    <div data-heat={r.symbol} title={`${r.symbol} ${r.live ? '盘中' : `${md(r.day)} 收盘`}${r.d5 != null ? ` · 5日 ${fmtPct(r.d5, 1)}` : ''}`}
      className={`flex h-5 min-w-0 items-center justify-between gap-1 rounded-btn px-1.5 text-[11px] tabular-nums ${heatCls(r.chg_pct)}`}>
      <span className="truncate">{r.name}</span>
      <span className="shrink-0 font-semibold">{fmtPct(r.chg_pct, 1)}</span>
    </div>
  )
}

function BreadthBody({ d }: { d: IntelPayload }) {
  const b = d.breadth
  const t = d.tape
  const bars: { k: string; label: string; v?: number | null }[] = b ? [
    { k: 'ma20', label: '站上 20 日线', v: b.above_ma20_pct }, { k: 'ma50', label: '站上 50 日线', v: b.above_ma50_pct }, { k: 'ma200', label: '站上 200 日线', v: b.above_ma200_pct },
  ] : []
  const when = t?.indexes?.[0] ? (t.indexes[0].live ? '盘中' : `${md(t.indexes[0].day)} 收盘`) : ''
  return (
    <>
      <div data-intel-breadth>
        {!b && <div className="py-1 text-xs text-muted">{d.errors.breadth === NOT_ARCHIVED ? '宽度未存档' : '无宽度数据'}</div>}
        {bars.map((x) => (
          <div key={x.k} data-breadth={x.k} className="grid h-5 grid-cols-[5.25rem_minmax(0,1fr)_2.5rem] items-center gap-2 text-xs">
            <span className="whitespace-nowrap text-secondary">{x.label}</span>
            <span className="block h-1.5 overflow-hidden rounded-full bg-muted/30"><i className="block h-full rounded-full bg-secondary" style={{ width: `${Math.max(0, Math.min(100, x.v ?? 0))}%` }} /></span>
            <span className="text-right font-semibold tabular-nums text-foreground">{x.v != null ? `${x.v.toFixed(0)}%` : '—'}</span>
          </div>
        ))}
      </div>
      {t ? (
        <>
          <div className="mb-1 mt-1.5 text-[11px] leading-4 text-secondary">{`板块 ETF${when ? ` · ${when}` : ''}`}</div>
          <div data-intel-sectors className="grid grid-cols-3 gap-1">{t.sectors.map((r) => <HeatMini key={r.symbol} r={r} />)}</div>
          <div className="mb-1 mt-1.5 text-[11px] leading-4 text-secondary">跨资产</div>
          <div data-intel-cross className="grid grid-cols-3 gap-1">{t.cross.map((r) => <HeatMini key={r.symbol} r={r} />)}</div>
        </>
      ) : (
        <>
          <div className="mb-1 mt-2 flex items-center gap-1.5 text-[11px] text-secondary">板块 ETF<span className={badge('limit')}>未存档</span></div>
          <Missing />
        </>
      )}
    </>
  )
}

function Failed() {
  return <div className={`${CARD} p-4 text-xs text-warning`}>这一块读取失败，其余照常</div>
}

// ── 温度带 ────────────────────────────────────────────────────────

function Cell({ label, children, sub, k, warn }: { label: string; children: React.ReactNode; sub?: React.ReactNode; k: string; warn?: boolean }) {
  return (
    <div data-intel-cell={k} className={`${CARD} min-w-0 px-3 py-2`}>
      <div className="text-[11px] text-secondary">{label}</div>
      <div className="mt-0.5 text-[14px] font-semibold tabular-nums text-foreground">{children}</div>
      {sub && <div className={`mt-0.5 text-[11px] leading-snug tabular-nums ${warn ? 'text-warning' : 'text-muted'}`}>{sub}</div>}
    </div>
  )
}

const SESSION_ZH: Record<string, string> = { pre: '盘前', regular: '盘中', post: '盘后' }

/** 指数格副行: 盘前 / 盘中 / 盘后标出时段与成交时刻 (0.42.4: 原来盘前整段只显示昨收, 看不到现价) */
export function quoteSub(r: IntelQuote, today: string) {
  if (r.live) {
    const seg = r.session ? `${SESSION_ZH[r.session] ?? ''}${r.time_et ? ` ${r.time_et}` : ''} · ` : ''
    return `${seg}对官方前收`
  }
  return r.day && r.day !== today ? `${md(r.day)} 收盘 · 非当日` : `${md(r.day)} 收盘`
}

function Tape({ d }: { d: IntelPayload }) {
  const today = etToday()
  const t = d.tape
  const v = d.vol
  const b = d.breadth
  const mood = d.mood?.judgment
  const ratioWarn = (v?.ratio ?? 0) > 1.05
  return (
    <div className="space-y-3">
      <div data-intel-indexes className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {(t?.indexes ?? []).map((r) => (
          <Cell key={r.symbol} k={r.symbol} label={`${r.name} ${r.symbol}`} sub={quoteSub(r, today)} warn={!r.live && r.day !== today && ACTIVE.has(d.phase?.phase ?? '')}>
            <span data-price>{r.price != null ? r.price.toFixed(2) : '—'}</span>{' '}
            <span className={`text-xs ${sgnCls(r.chg_pct)}`}>{fmtPct(r.chg_pct)}</span>
          </Cell>
        ))}
      </div>
      <div data-intel-gauges className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <Cell k="vix" label="VIX / VIX3M" warn={ratioWarn}
          sub={v ? `${v.ratio != null ? `比 ${v.ratio.toFixed(2)}${ratioWarn ? ' 倒挂' : ''} · ` : ''}延迟 15 分` : '—'}>
          {v ? <>{v.vix.value.toFixed(2)} <span className={`text-xs ${sgnCls(v.vix.chg_pct)}`}>{fmtPct(v.vix.chg_pct)}</span></> : '—'}
        </Cell>
        <Cell k="rates" label="美债 10Y"
          sub={d.rates ? `${md(d.rates.as_of)}${d.rates.us10y_chg_bp != null ? ` · ${d.rates.us10y_chg_bp > 0 ? '+' : ''}${d.rates.us10y_chg_bp}bp` : ''}` : '—'}>
          {d.rates?.us10y != null ? `${d.rates.us10y.toFixed(2)}%` : '—'}
        </Cell>
        <Cell k="hy" label="高收益债利差"
          sub={d.rates?.hy_as_of ? `${md(d.rates.hy_as_of)}${d.rates.hy_chg_bp != null ? ` · ${d.rates.hy_chg_bp > 0 ? '+' : ''}${d.rates.hy_chg_bp}bp` : ''}` : '—'}>
          {d.rates?.hy_oas != null ? `${d.rates.hy_oas.toFixed(2)}%` : '—'}
        </Cell>
        <Cell k="pcr" label="个股 Put/Call"
          sub={d.pcr ? `${md(d.pcr.day)} · 20日均 ${d.pcr.equity_avg20?.toFixed(2) ?? '—'}` : '—'}>
          {d.pcr?.equity != null ? d.pcr.equity.toFixed(2) : '—'}
        </Cell>
        <Cell k="mood" label="08:35 大盘判断"
          sub={d.mood ? `${mood ? md(mood.day) : ''}${d.mood.is_today ? '' : ' 非当日'} · 近 ${d.mood.stats.n} 日中 ${d.mood.stats.hit}` : '—'}
          warn={!!d.mood && !d.mood.is_today && ACTIVE.has(d.phase?.phase ?? '')}>
          {mood ? <span className={mood.stance === 'up' ? 'text-bull' : mood.stance === 'down' ? 'text-bear' : 'text-secondary'}>{STANCE[mood.stance]}</span> : '—'}
        </Cell>
      </div>
      {b && <Breadth b={b} />}
      <div className={`${CARD} px-3 py-2`}>
        <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2 text-[11px] text-secondary">
          <span>板块 ETF · 当日 / 5 日</span>
          <span className="text-muted">{t?.source}{t?.age_min != null ? ` · ${Math.round(t.age_min)} 分钟前` : ''}</span>
        </div>
        <div data-intel-sectors className="grid grid-cols-2 gap-1.5 sm:grid-cols-4 lg:grid-cols-6">
          {(t?.sectors ?? []).map((r) => <Heat key={r.symbol} r={r} />)}
        </div>
        <div className="mb-1.5 mt-4 text-[11px] text-secondary">跨资产</div>
        <div data-intel-cross className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6">
          {(t?.cross ?? []).map((r) => <Heat key={r.symbol} r={r} />)}
        </div>
      </div>
    </div>
  )
}

/** 色块深浅按幅度分 3 档 (|1%| / |2%|); 红涨绿跌。类名写全 (Tailwind 只收字面量) */
// 文字一律前景色 (同色系文字压在同色底上, 暗色主题对比度只有 2.5-3.9); 涨跌靠底色深浅 + 正负号
const HEAT = {
  bull: ['bg-bull/[0.08] text-foreground', 'bg-bull/20 text-foreground', 'bg-bull/35 text-foreground'],
  bear: ['bg-bear/[0.08] text-foreground', 'bg-bear/20 text-foreground', 'bg-bear/35 text-foreground'],
} as const
export function heatCls(v?: number | null) {
  if (v == null) return 'bg-elevated text-muted'
  if (v === 0) return 'bg-elevated text-foreground'
  const a = Math.abs(v)
  return HEAT[v > 0 ? 'bull' : 'bear'][a >= 2 ? 2 : a >= 1 ? 1 : 0]
}

function Heat({ r }: { r: IntelQuote }) {
  return (
    <div data-heat={r.symbol} title={`${r.symbol} ${r.live ? '盘中' : `${md(r.day)} 收盘`}`} className={`rounded-btn px-2.5 py-1.5 tabular-nums ${heatCls(r.chg_pct)}`}>
      <div className="flex items-baseline justify-between gap-1 text-[11px]">
        <span className="truncate font-medium">{r.name}</span>
        <span className="font-semibold">{fmtPct(r.chg_pct, 1)}</span>
      </div>
      <div className="mt-0.5 flex items-baseline justify-between gap-1 text-[11px]">
        <span>{r.symbol}</span>
        {r.d5 != null && <span data-d5>{`5日 ${fmtPct(r.d5, 1)}`}</span>}
      </div>
    </div>
  )
}

function Spark({ vals, k }: { vals: (number | null)[]; k: string }) {
  const xs = vals.map((v) => (v == null ? null : v))
  const ok = xs.filter((v): v is number => v != null)
  if (ok.length < 2) return null
  const lo = Math.min(...ok), hi = Math.max(...ok)
  const pts = xs.map((v, i) => (v == null ? null : `${(i / (xs.length - 1)) * 100},${hi === lo ? 10 : 20 - ((v - lo) / (hi - lo)) * 20}`)).filter(Boolean).join(' ')
  return (
    <svg data-spark={k} viewBox="0 0 100 20" preserveAspectRatio="none" className="h-5 w-full text-accent" aria-hidden>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

function Breadth({ b }: { b: IntelBreadth }) {
  const s = b.series
  const items: { k: string; label: string; val: string; series: (number | null)[]; cls?: string }[] = [
    { k: 'up', label: '上涨家数占比', val: `${b.up_pct?.toFixed(0) ?? '—'}%`, series: s.map((x) => x.up_pct), cls: sgnCls((b.up_pct ?? 50) - 50) },
    { k: 'ma20', label: '站上 20 日线', val: `${b.above_ma20_pct?.toFixed(0) ?? '—'}%`, series: s.map((x) => x.above_ma20_pct) },
    { k: 'ma50', label: '站上 50 日线', val: `${b.above_ma50_pct?.toFixed(0) ?? '—'}%`, series: s.map((x) => x.above_ma50_pct) },
    { k: 'ma200', label: '站上 200 日线', val: `${b.above_ma200_pct?.toFixed(0) ?? '—'}%`, series: s.map((x) => x.above_ma200_pct) },
    { k: 'hl', label: '52 周新高 / 新低', val: `${b.new_highs} / ${b.new_lows}`, series: s.map((x) => x.new_highs - x.new_lows) },
  ]
  return (
    <div data-intel-breadth className={`${CARD} px-3 py-2`}>
      <div className="mb-2 text-[11px] text-secondary">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span>{`全市场宽度 · ${md(b.as_of)} 收盘 · ${b.universe} 只`}</span>
          {b.live && <span data-breadth-live className="text-foreground">{`盘中近似 上涨 ${b.live.up_pct.toFixed(0)}% (${b.live.adv}/${b.live.n})`}</span>}
        </div>
        <div className="mt-0.5 text-muted">{`${b.rule} · 走势 = 近 ${s.length} 日`}</div>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-5 sm:divide-x sm:divide-border/60 [&>*]:sm:pl-3 [&>*:first-child]:sm:pl-0">
        {items.map((it) => (
          <div key={it.k} data-breadth={it.k} className="min-w-0">
            <div className="flex items-baseline justify-between gap-1">
              <span className="text-[11px] text-secondary">{it.label}</span>
              <span className={`text-sm font-semibold tabular-nums ${it.cls ?? 'text-foreground'}`}>{it.val}</span>
            </div>
            <div className="mt-1"><Spark vals={it.series} k={it.k} /></div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── 日历 ──────────────────────────────────────────────────────────

const LEVELS: { k: 'high' | 'high_medium' | 'all'; label: string }[] = [
  { k: 'high', label: '仅高' }, { k: 'high_medium', label: '高+中' }, { k: 'all', label: '全部' },
]

export function surpriseLabel(e: IntelEvent) {
  if (e.actual) return e.surprise === 'above' ? '高于预期' : e.surprise === 'below' ? '低于预期' : e.surprise === 'inline' ? '符合预期' : e.surprise === 'near' ? '接近预期' : ''
  if (e.actual_status === 'pending') return '实际待同步'
  if (e.actual_status === 'mismatch') return '已公布 · 推算口径未对齐，不显示'
  if (e.actual_status === 'unmapped') return '已公布'
  return ''
}

function Calendar({ cal, level, onSymbol, embedded, readOnly }: {
  cal: IntelCalendar | null; level: string; onSymbol: (s: string) => void; embedded?: boolean; readOnly?: boolean
}) {
  const qc = useQueryClient()
  const save = useMutation({ mutationFn: api.intelSettings, onSuccess: () => qc.invalidateQueries({ queryKey: QK.intelToday }) })
  const [showAll, setShowAll] = useState(false)
  const groups = useMemo(() => {
    const m = new Map<string, IntelEvent[]>()
    for (const e of cal?.events ?? []) m.set(e.day, [...(m.get(e.day) ?? []), e])
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [cal])
  // 展开全部时列表从本周一开始, 卡内滚到今天 (或今天之后第一天); 收起回到顶部。定时刷新不动滚动位置
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const b = bodyRef.current
    if (!b) return
    const t = showAll ? [...b.querySelectorAll<HTMLElement>('[data-cal-day]')].find((el) => (el.dataset.calDay ?? '') >= (cal?.today ?? '')) : null
    b.scrollTop = t ? b.scrollTop + t.getBoundingClientRect().top - b.getBoundingClientRect().top : 0
  }, [showAll, cal?.today])
  if (!cal) return <div className={`${CARD} p-4 text-xs text-muted`}>无日历数据</div>
  const today = cal.today
  // 前瞻 = FF 覆盖周之后的日子; FF 缺失 (ff_until 为空) 时 TradingView 顶替本周, 不算前瞻
  const ffUntil = cal.ff_until === undefined ? '' : cal.ff_until
  const isLook = (evs: IntelEvent[]) => ffUntil !== null && evs.length > 0 && evs.every((e) => e.source === 'tradingview' && e.day > ffUntil)
  // 收起时: 本周 (FF) 今天起 3 天 + 下周起的高重要度前瞻 (TradingView); 展开看全部
  const visible = showAll ? groups : groups.filter(([day, evs]) => day >= today && !isLook(evs)).slice(0, 3)
  const ahead = showAll || ffUntil === null ? [] : (cal.events ?? []).filter((e) => e.source === 'tradingview' && e.impact === 'high' && e.day >= today && e.day > ffUntil)
  return (
    <div data-pair-card="calendar" className={embedded ? EMBED_CARD : PAIR_CARD}>
      <div className="mb-2 flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div data-intel-levels role="radiogroup" aria-label="宏观事件重要度" className="inline-flex rounded-btn border border-border p-0.5 text-[11px]">
          {LEVELS.map((l) => (
            <button key={l.k} type="button" role="radio" aria-checked={level === l.k} disabled={save.isPending || readOnly}
              onClick={() => level !== l.k && !readOnly && save.mutate(l.k)}
              className={`rounded px-2 py-0.5 ${level === l.k ? 'bg-accent/20 font-medium text-foreground ring-1 ring-accent/50' : 'text-secondary hover:text-foreground'}`}>
              {l.label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-muted">ET · {cal.source}</span>
      </div>
      {cal.error && <div className="mb-2 shrink-0 text-[11px] text-warning">日历源上次刷新失败，显示的是上一份</div>}
      <div ref={bodyRef} data-pair-body="calendar" className={`${embedded ? EMBED_BODY : PAIR_BODY} space-y-3`}>
        {visible.map(([day, evs]) => (
          <div key={day} data-cal-day={day}>
            <div className={`mb-1 text-[11px] font-medium ${day === today ? 'text-accent' : 'text-secondary'}`}>
              {`${md(day)} ${weekday(day)}${day === today ? ' · 今天' : ''}${isLook(evs) ? ' · 前瞻' : ''}`}
            </div>
            <ul className="divide-y divide-border/60">
              {evs.map((e, i) => <EventRow key={`${e.at}-${e.title}-${i}`} e={e} onSymbol={onSymbol} />)}
            </ul>
          </div>
        ))}
        {visible.length === 0 && <div className="py-2 text-xs text-muted">本周无剩余事件</div>}
        {ahead.length > 0 && (
          <div data-cal-ahead>
            <div className="mb-1 text-[11px] font-medium text-secondary">下周起 · 高重要度</div>
            <ul className="divide-y divide-border/60">
              {ahead.map((e, i) => (
                <li key={`${e.at}-${e.title}-${i}`} data-cal-event="ahead" className="flex items-baseline gap-2 py-1 text-xs">
                  <span className="w-[4.75rem] shrink-0 whitespace-nowrap tabular-nums text-muted">{`${md(e.day)} ${weekday(e.day)}`}</span>
                  <span className="w-10 shrink-0 whitespace-nowrap tabular-nums text-muted">{e.time || '全天'}</span>
                  <span className="min-w-0 truncate font-medium text-foreground" title={e.title}>{e.title_zh ?? eventTitleZh(e.title)}</span>
                  {(e.forecast || e.previous) && (
                    <span className="ml-auto shrink-0 tabular-nums text-[11px] text-secondary">
                      {[e.forecast && `预期 ${e.forecast}`, e.previous && `前值 ${e.previous}`].filter(Boolean).join(' · ')}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {cal.lookahead_error && <div className="text-[11px] text-muted">前瞻日历上次刷新失败，显示的是上一份</div>}
      </div>
      {groups.length > visible.length || showAll ? (
        <button type="button" data-cal-more onClick={() => setShowAll((x) => !x)}
          className={`shrink-0 text-[11px] text-accent hover:underline ${embedded ? 'w-full border-t border-border/70 pt-1.5 text-left' : 'mt-2 self-start'}`}>
          {showAll ? '收起' : `全部 ${groups.length} 天（含已过与未来）`}
        </button>
      ) : null}
    </div>
  )
}

function EventRow({ e, onSymbol }: { e: IntelEvent; onSymbol: (s: string) => void }) {
  const dim = e.past && !e.actual ? 'text-muted' : 'text-foreground'
  if (e.kind === 'earnings') {
    return (
      <li data-cal-event="earnings" className="flex items-baseline gap-2 py-1 text-xs">
        <span className="w-10 shrink-0 tabular-nums text-muted">{e.time || '—'}</span>
        <span className="inline-block w-5 shrink-0 rounded border border-accent/40 text-center text-[11px] text-accent">财</span>
        <button type="button" onClick={() => e.symbol && onSymbol(e.symbol)} className="font-medium text-foreground hover:text-accent">{e.symbol}</button>
        <span className="text-secondary">{`财报 · ${TIMING_ZH[e.timing ?? 'unknown'] ?? '时间未定'}`}</span>
        {e.role === '持仓' && <span className="rounded bg-accent/10 px-1 text-[11px] text-accent dark:text-blue-400">持仓</span>}
      </li>
    )
  }
  if (e.kind === 'opex') {
    return (
      <li data-cal-event="opex" className="flex items-baseline gap-2 py-1 text-xs">
        <span className="w-10 shrink-0 tabular-nums text-muted">{e.time}</span>
        <span className="text-secondary">月度期权到期</span>
      </li>
    )
  }
  const sl = surpriseLabel(e)
  return (
    <li data-cal-event="macro" data-impact={e.impact} data-source={e.source ?? 'ff'} className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-x-2 py-1 text-xs">
      <span className="tabular-nums text-muted">{e.time || '全天'}</span>
      <div className="min-w-0">
        <div className="flex items-baseline gap-x-2">
          <span className={`inline-block w-5 shrink-0 rounded border text-center text-[11px] ${e.impact === 'high' ? 'border-warning/60 text-warning' : 'border-border text-secondary'}`}>{IMPACT_ZH[e.impact ?? 'low']}</span>
          <span className={`${dim} ${e.impact === 'high' ? 'font-medium' : ''}`} title={e.title}>{e.title_zh ?? eventTitleZh(e.title)}</span>
        </div>
        {(e.forecast || e.previous || e.actual || sl) && (
          <div className="mt-0.5 flex flex-wrap gap-x-3 tabular-nums text-[11px] text-secondary">
            {e.actual && <span data-actual className="font-semibold text-foreground" title={e.actual_derived ? '由 FRED 水平值推算，与官方公布可能差 0.1' : ''}>{`实际 ${e.actual_derived ? '≈' : ''}${e.actual}`}</span>}
            {e.forecast && <span>{`预期 ${e.forecast}`}</span>}
            {e.previous && <span>{`前值 ${e.previous}`}</span>}
            {sl && <span data-surprise={e.surprise ?? e.actual_status} className={e.actual ? 'text-foreground' : 'text-muted'} title={e.actual_source ?? ''}>{sl}</span>}
          </div>
        )}
      </div>
    </li>
  )
}

// ── 财报 ──────────────────────────────────────────────────────────

/** 30 日修正: 只在预期 EPS 为正时给百分比 (负基数上的百分比方向会反) */
export const rev30 = (r: IntelEarningsRow) => {
  const q = r.qveris
  return q?.eps_change_30d_pct != null && (q.eps_avg ?? 0) > 0 ? q.eps_change_30d_pct : null
}

function Earnings({ rows, onSymbol, compact }: { rows: IntelEarningsRow[]; onSymbol: (s: string) => void; compact?: boolean }) {
  if (!rows.length) return <div className={compact ? 'py-1 text-xs text-muted' : `${CARD} p-4 text-xs text-muted`}>未来 10 个交易日自选无财报</div>
  // 六宫格: 四列 (股票 / 公布 / 隐含波幅 × 历史 / 上次反应); EPS 预期、修正、超预期次数在「展开」里
  if (compact) {
    return (
      <table data-intel-earnings="compact" className="w-full text-xs tabular-nums">
        <thead className="text-[11px] text-secondary">
          <tr className="border-b border-border text-left">
            <th className="py-1 pr-1 font-normal">股票</th>
            <th className="px-1 py-1 font-normal">公布</th>
            <th className="px-1 py-1 text-right font-normal" title="财报后第一个到期日的平值跨式 / 现价；「×历史」= 隐含波幅 ÷ 历史反应中位数">隐含波幅</th>
            <th className="py-1 pl-1 text-right font-normal" title="上一次财报的反应日涨跌">上次反应</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const imp = r.implied?.expected_move_pct
            const cmp = impliedRatio(r)
            const main = (
              <tr key={r.symbol} data-earn={r.symbol} className={rows.length > EARN_DETAIL_MAX ? 'border-b border-border/60 last:border-0' : ''}>
                <td className="whitespace-nowrap py-1.5 pr-1">
                  <button type="button" onClick={() => onSymbol(r.symbol)} className="font-medium text-foreground hover:text-accent">{r.symbol}</button>
                  {r.role === '持仓' && <span className="ml-1 rounded bg-accent/10 px-1 text-[11px] text-accent dark:text-blue-400">持仓</span>}
                </td>
                <td className="whitespace-nowrap px-1 py-1.5 text-secondary">
                  {`${md(r.date)} ${TIMING_ZH[r.timing] ?? ''}`}
                  {r.date_conflict
                    ? <span data-date-conflict className="ml-1 text-warning" title={`日期待确认 · Finnhub 为 ${r.date_conflict}`}>待确认</span>
                    : <span className="ml-1 text-muted">{r.days === 0 ? '今天' : `T+${r.days}`}</span>}
                </td>
                <td data-implied className="whitespace-nowrap px-1 py-1.5 text-right"
                  title={imp == null ? '期权链未更新 · 09:35 ET 后可用' : `到期 ${r.implied?.expiration}${r.hist?.median_abs_move != null ? ` · 历史中位 ±${r.hist.median_abs_move.toFixed(1)}%` : ''}`}>
                  {imp != null ? `±${imp.toFixed(1)}%` : '—'}
                  <span className={`ml-1 ${cmp != null && cmp >= 1.25 ? 'text-warning' : 'text-muted'}`}>{cmp != null ? `${cmp.toFixed(1)}×历史` : ''}</span>
                </td>
                <td className={`whitespace-nowrap py-1.5 pl-1 text-right ${sgnCls(r.last?.move_pct)}`}>{r.last ? fmtPct(r.last.move_pct, 1) : '—'}</td>
              </tr>
            )
            // 行少 (≤ EARN_DETAIL_MAX) 时每只多给一行: EPS 预期 / 历史中位 / 超预期次数, 不留空卡
            return rows.length > EARN_DETAIL_MAX ? main : (
              <Fragment key={r.symbol}>
                {main}
                <tr data-earn-detail={r.symbol} className="border-b border-border/60 text-[11px] text-secondary last:border-0">
                  <td colSpan={4} className="whitespace-nowrap pb-1.5">
                    {[`EPS 预期 ${fmtUsd(r.eps_consensus ?? r.eps_forecast)}`,
                      r.hist?.median_abs_move != null ? `历史中位 ±${r.hist.median_abs_move.toFixed(1)}%` : '',
                      r.hist?.surprise_n ? `超预期 ${r.hist.beat_n}/${r.hist.surprise_n}` : ''].filter(Boolean).join(' · ')}
                  </td>
                </tr>
              </Fragment>
            )
          })}
        </tbody>
      </table>
    )
  }
  return (
    <div className={`${CARD} overflow-x-auto`}>
      <table data-intel-earnings className="w-full min-w-[760px] text-xs tabular-nums">
        <thead className="text-[11px] text-secondary">
          <tr className="border-b border-border text-left">
            <th className="px-3 py-2 font-normal">股票</th>
            <th className="px-2 py-2 font-normal">公布</th>
            <th className="px-2 py-2 text-right font-normal" title="华尔街调整后口径: QVeris > Finnhub > Nasdaq">EPS 预期</th>
            <th className="px-2 py-2 text-right font-normal" title="一致预期近 30 天变化">30 日修正</th>
            <th className="px-2 py-2 text-right font-normal" title="近几次财报反应日涨跌幅绝对值的中位数 / 最大">历史反应 中位 / 最大</th>
            <th className="px-2 py-2 text-right font-normal" title="财报后第一个到期日的平值跨式 / 现价；「×历史」= 隐含波幅 ÷ 历史反应中位数">期权隐含波幅</th>
            <th className="px-2 py-2 text-right font-normal" title="Nasdaq 历史库：实际高于预期的次数 / 有数据的次数">EPS 超预期</th>
            <th className="px-3 py-2 text-right font-normal" title="上一次财报的反应日涨跌">上次反应</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const h = r.hist
            const imp = r.implied?.expected_move_pct
            const cmp = imp != null && h?.median_abs_move ? imp / h.median_abs_move : null
            return (
              <tr key={r.symbol} data-earn={r.symbol} className="border-b border-border/60 last:border-0">
                <td className="px-3 py-1.5">
                  <button type="button" onClick={() => onSymbol(r.symbol)} className="font-medium text-foreground hover:text-accent">{r.symbol}</button>
                  {r.role === '持仓' && <span className="ml-1 rounded bg-accent/10 px-1 text-[11px] text-accent dark:text-blue-400">持仓</span>}
                </td>
                <td className="whitespace-nowrap px-2 py-1.5 text-secondary">
                  {`${md(r.date)} ${weekday(r.date)} ${TIMING_ZH[r.timing] ?? ''}`}
                  <span className="ml-1 text-muted">{r.days === 0 ? '今天' : `T+${r.days}`}</span>
                  {r.date_conflict && <span data-date-conflict className="ml-1 text-warning" title={`Finnhub 为 ${r.date_conflict}`}>日期待确认</span>}
                </td>
                <td className="px-2 py-1.5 text-right" title={r.eps_source ? `来源 ${r.eps_source}${r.eps_source !== 'Nasdaq' && r.eps_nasdaq != null ? `；Nasdaq ${fmtUsd(r.eps_nasdaq)}（部分公司为 GAAP 口径）` : ''}` : ''}>
                  {fmtUsd(r.eps_consensus ?? r.eps_forecast)}<span data-eps-src className="ml-1 inline-block w-12 text-left text-[11px] text-muted">{r.eps_source ?? ''}</span>
                </td>
                <td className={`px-2 py-1.5 text-right ${sgnCls(rev30(r))}`} title="QVeris 一致预期 EPS 近 30 天变化；预期为负时不算百分比">
                  {rev30(r) != null ? fmtPct(rev30(r)! * 100, 1) : '—'}
                </td>
                <td className="px-2 py-1.5 text-right">{h?.median_abs_move != null ? `±${h.median_abs_move.toFixed(1)}% / ±${h.max_abs_move?.toFixed(1)}%` : '—'}<span className="ml-1 inline-block w-7 text-left text-muted">{h ? `${h.n}次` : ''}</span></td>
                <td data-implied className="px-2 py-1.5 text-right" title={imp == null ? '期权链未更新 · 09:35 ET 后可用' : `到期 ${r.implied?.expiration} · 跨式价格 ÷ 现价`}>
                  {imp != null ? `±${imp.toFixed(1)}%` : '—'}
                  <span className={`ml-1 inline-block w-14 text-left ${cmp != null && cmp >= 1.25 ? 'text-warning' : 'text-muted'}`}>{cmp != null ? `${cmp.toFixed(1)}×历史` : ''}</span>
                </td>
                <td className="px-2 py-1.5 text-right">{h?.surprise_n ? `${h.beat_n}/${h.surprise_n}` : '—'}</td>
                <td className="px-3 py-1.5 text-right">
                  {r.last ? <span className={sgnCls(r.last.move_pct)}>{`${md(r.last.date)} ${fmtPct(r.last.move_pct, 1)}`}</span> : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── 新闻 ──────────────────────────────────────────────────────────

type NewsFilter = 'all' | 'hold' | 'bullish' | 'bearish' | 'macro'
const NEWS_FILTERS: { k: NewsFilter; label: string }[] = [
  { k: 'all', label: '全部' }, { k: 'hold', label: '持仓' }, { k: 'bullish', label: '利多' }, { k: 'bearish', label: '利空' }, { k: 'macro', label: '宏观' },
]

export function filterNews(items: IntelNewsItem[], f: NewsFilter) {
  if (f === 'hold') return items.filter((i) => i.role === '持仓')
  if (f === 'macro') return items.filter((i) => i.macro || i.event_type === 'macro')
  if (f === 'bullish' || f === 'bearish') return items.filter((i) => i.direction === f)
  return items
}

function News({ news, onSymbol, embedded }: { news: IntelPayload['news']; onSymbol: (s: string) => void; embedded?: boolean }) {
  const [f, setF] = useState<NewsFilter>('all')
  if (!news) return <div className={`${CARD} p-4 text-xs text-muted`}>无新闻数据</div>
  const limit = news.limit ?? 40
  const items = filterNews(news.items, f).slice(0, limit)
  const tone = Object.entries(news.tone).sort((a, b) => (b[1].bullish + b[1].bearish) - (a[1].bullish + a[1].bearish)).slice(0, 8)
  return (
    <div data-pair-card="news" className={embedded ? EMBED_CARD : PAIR_CARD}>
      <div className="mb-2 flex shrink-0 flex-wrap items-center gap-1.5">
        {NEWS_FILTERS.map((x) => (
          <button key={x.k} type="button" data-news-filter={x.k} aria-pressed={f === x.k} onClick={() => setF(x.k)}
            className={`rounded-btn border px-2 py-0.5 text-[11px] ${f === x.k ? 'border-accent/40 bg-accent/10 text-accent dark:text-blue-400' : 'border-border text-secondary hover:text-foreground'}`}>
            {x.label}{news.counts?.[x.k] != null ? ` ${news.counts[x.k]}` : ''}
          </button>
        ))}
      </div>
      {tone.length > 0 && !embedded && (
        <div data-news-tone className="mb-2 flex shrink-0 flex-wrap gap-x-3 gap-y-1 text-[11px] tabular-nums">
          {tone.map(([s, t]) => (
            <button key={s} type="button" onClick={() => onSymbol(s)} className="text-secondary hover:text-accent">
              {s} <span className="text-bull">{t.bullish ? `多${t.bullish}` : ''}</span>{t.bullish && t.bearish ? ' ' : ''}<span className="text-bear">{t.bearish ? `空${t.bearish}` : ''}</span>
            </button>
          ))}
        </div>
      )}
      <div data-news-list data-pair-body="news" className={embedded ? EMBED_BODY : PAIR_BODY}>
        {items.map((i) => (
          <div key={i.id} data-news-row={i.symbol} className="relative">
            <button type="button" onClick={() => onSymbol(i.symbol)}
              className="absolute right-0 top-2 z-[1] rounded bg-surface pl-1 text-[11px] font-medium text-secondary hover:text-accent">
              {i.macro ? '宏观' : i.symbol}
            </button>
            <ul><NewsRow item={i} /></ul>
          </div>
        ))}
        {items.length === 0 && <div className="py-3 text-xs text-muted">无</div>}
      </div>
      {!embedded && <p className="mt-1 shrink-0 text-[11px] text-muted">{news.disclaimer}</p>}
    </div>
  )
}

// ── 数据源状态 ────────────────────────────────────────────────────

const SRC_ZH: Record<string, string> = {
  etfs: 'ETF 快照', vol: '波动率', rates: '利率', macro_calendar: '宏观日历', macro_lookahead: '前瞻日历', macro_actuals: '宏观实际值', pcr: '期权 Put/Call',
  breadth: '全市场宽度', etf_daily: 'ETF 官方日K', earnings_upcoming: '财报预期', earnings_history: '财报历史', options_warm: '期权链预热',
  sentiment: '舆情采集',
}

function SourceState({ d }: { d: IntelPayload }) {
  const bad = Object.entries(d.state ?? {}).filter(([, v]) => v.error)
  const failed = Object.entries(d.errors).filter(([, v]) => v !== NOT_ARCHIVED).map(([k]) => k)
  return (
    <section data-intel="sources" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
      <Activity className="h-3.5 w-3.5" />
      {bad.length === 0
        ? <span>数据源正常</span>
        : bad.map(([k, v]) => <span key={k} className="text-warning" title={v.error ?? ''}>{`${SRC_ZH[k] ?? k} 上次刷新失败 (${(v.last_ok_at ?? '').slice(5, 16).replace('T', ' ') || '从未成功'})`}</span>)}
      {failed.length > 0 && <span className="text-warning">{`区块读取失败: ${failed.join('、')}`}</span>}
    </section>
  )
}
