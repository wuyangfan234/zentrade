/**
 * 盘面情报 (0.40.0, 无限 10-26 选 B 档): 当日盘面温度 + 宏观日历 (含实际值) + 自选财报 + 自选新闻。
 * 只读展示, 不参与打分 / 裁决 / 下单。数据一次取齐 (GET /api/intel/today, 只读缓存);
 * 盘前 / 盘中 60 秒刷新, 其余 5 分钟。每格标来源与时点, 不是今天的数据标「非今日」。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, CalendarDays, FileBarChart2, Newspaper, RefreshCw, Thermometer } from 'lucide-react'
import {
  api, type IntelBreadth, type IntelCalendar, type IntelEarningsRow, type IntelEvent, type IntelNewsItem,
  type IntelPayload, type IntelQuote,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { SectionTitle } from '@/components/data/SectionTitle'
import { NewsRow } from '@/components/StockNews'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { etToday } from '@/lib/etTime'

const ACTIVE = new Set(['premarket', 'open_wait', 'session'])
const WRAP = 'mx-auto max-w-6xl space-y-6 px-4 pb-24 pt-4 md:px-8 md:pt-6'
const CARD = 'rounded-card border border-border bg-surface'
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

export function Intel() {
  const qc = useQueryClient()
  const q = useQuery<IntelPayload>({
    queryKey: QK.intelToday, queryFn: api.intelToday,
    refetchInterval: (qq) => intelPollMs(qq.state.data as IntelPayload | undefined), refetchIntervalInBackground: false,
  })
  const refresh = useMutation({ mutationFn: api.intelRefresh, onSuccess: () => qc.invalidateQueries({ queryKey: QK.intelToday }) })
  const [preview, setPreview] = useState<string | null>(null)
  const d = q.data
  const errs = d?.errors ?? {}

  return (
    <div className="overflow-x-hidden">
      <PageHeader
        title="盘面情报"
        subtitle={d ? `${d.phase?.label ?? ''} · 美东 ${d.generated_at.slice(5, 16).replace('T', ' ')} · 自选 ${d.watch_count} 只 · 只展示 · 不参与下单` : '加载中…'}
        right={(
          <button type="button" data-intel-refresh disabled={refresh.isPending} onClick={() => refresh.mutate()}
            className="inline-flex items-center gap-1.5 rounded-btn border border-border px-2.5 py-1 text-xs text-secondary hover:bg-elevated disabled:opacity-60">
            <RefreshCw className={`h-3.5 w-3.5 ${refresh.isPending ? 'animate-spin' : ''}`} />
            {refresh.isPending ? '刷新中' : '立即刷新'}
          </button>
        )}
      />
      <div data-intel-wrap className={WRAP}>
        {q.isError && <div className={`${CARD} p-4 text-sm text-warning`}>读取失败 · {String((q.error as Error)?.message ?? '')}</div>}
        {d && (
          <>
            <section data-intel="tape" className="space-y-3">
              <SectionTitle icon={Thermometer}>盘面温度</SectionTitle>
              <Tape d={d} />
            </section>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <section data-intel="calendar" className="min-w-0 space-y-3">
                <SectionTitle icon={CalendarDays}>宏观与事件日历</SectionTitle>
                {errs.calendar ? <Failed /> : <Calendar cal={d.calendar} level={d.settings.macro_impact} onSymbol={setPreview} />}
              </section>
              <section data-intel="news" className="min-w-0 space-y-3">
                <SectionTitle icon={Newspaper}>{`自选新闻 · ${d.news?.hours ?? 36} 小时`}</SectionTitle>
                {errs.news ? <Failed /> : <News news={d.news} onSymbol={setPreview} />}
              </section>
            </div>
            <section data-intel="earnings" className="space-y-3">
              <SectionTitle icon={FileBarChart2}>{`自选财报 · 未来 10 个交易日 · ${d.earnings?.rows.length ?? 0} 只`}</SectionTitle>
              {errs.earnings ? <Failed /> : <Earnings rows={d.earnings?.rows ?? []} onSymbol={setPreview} />}
              {(d.actions?.length ?? 0) > 0 && (
                <div data-intel-actions className="text-xs text-secondary">
                  公司行动 · {d.actions!.map((a) => `${a.symbol} ${md(a.date)} ${a.type === 'cash_dividend' ? `除息 $${a.rate != null ? a.rate.toFixed(2) : ''}/股` : a.type}`).join(' · ')}
                </div>
              )}
              <p className="text-[11px] text-muted">{d.earnings?.source}{d.earnings?.fetched_at ? ` · 预期取于 ${md(d.earnings.fetched_at)} ${d.earnings.fetched_at.slice(11, 16)} ET` : ''}</p>
            </section>
            <SourceState d={d} />
          </>
        )}
      </div>
      <StockPreviewDialog symbol={preview} onClose={() => setPreview(null)} />
    </div>
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
      <div className="mt-0.5 text-[15px] font-semibold tabular-nums text-foreground">{children}</div>
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
  return r.day && r.day !== today ? `${md(r.day)} 收盘 · 非今日` : `${md(r.day)} 收盘`
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
          sub={v ? `${v.ratio != null ? `比 ${v.ratio.toFixed(2)}${ratioWarn ? ' 倒挂' : ''} · ` : ''}延迟 15 分` : '缺数据'}>
          {v ? <>{v.vix.value.toFixed(2)} <span className={`text-xs ${sgnCls(v.vix.chg_pct)}`}>{fmtPct(v.vix.chg_pct)}</span></> : '—'}
        </Cell>
        <Cell k="rates" label="美债 10Y"
          sub={d.rates ? `${md(d.rates.as_of)}${d.rates.us10y_chg_bp != null ? ` · ${d.rates.us10y_chg_bp > 0 ? '+' : ''}${d.rates.us10y_chg_bp}bp` : ''}` : '缺数据'}>
          {d.rates?.us10y != null ? `${d.rates.us10y.toFixed(2)}%` : '—'}
        </Cell>
        <Cell k="hy" label="高收益债利差"
          sub={d.rates?.hy_as_of ? `${md(d.rates.hy_as_of)}${d.rates.hy_chg_bp != null ? ` · ${d.rates.hy_chg_bp > 0 ? '+' : ''}${d.rates.hy_chg_bp}bp` : ''}` : '缺数据'}>
          {d.rates?.hy_oas != null ? `${d.rates.hy_oas.toFixed(2)}%` : '—'}
        </Cell>
        <Cell k="pcr" label="个股 Put/Call"
          sub={d.pcr ? `${md(d.pcr.day)} · 20日均 ${d.pcr.equity_avg20?.toFixed(2) ?? '—'}` : '缺数据'}>
          {d.pcr?.equity != null ? d.pcr.equity.toFixed(2) : '—'}
        </Cell>
        <Cell k="mood" label="08:35 大盘判断"
          sub={d.mood ? `${mood ? md(mood.day) : ''}${d.mood.is_today ? '' : ' 非今日'} · 近 ${d.mood.stats.n} 日中 ${d.mood.stats.hit}` : '缺数据'}
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
      <div className="mt-0.5 flex items-baseline justify-between gap-1 text-[10px]">
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
  if (e.actual_status === 'unmapped') return '已公布 · 无实际值源'
  return ''
}

function Calendar({ cal, level, onSymbol }: { cal: IntelCalendar | null; level: string; onSymbol: (s: string) => void }) {
  const qc = useQueryClient()
  const save = useMutation({ mutationFn: api.intelSettings, onSuccess: () => qc.invalidateQueries({ queryKey: QK.intelToday }) })
  const [showAll, setShowAll] = useState(false)
  const groups = useMemo(() => {
    const m = new Map<string, IntelEvent[]>()
    for (const e of cal?.events ?? []) m.set(e.day, [...(m.get(e.day) ?? []), e])
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [cal])
  if (!cal) return <div className={`${CARD} p-4 text-xs text-muted`}>无日历数据</div>
  const today = cal.today
  const visible = showAll ? groups : groups.filter(([day]) => day >= today).slice(0, 3)
  return (
    <div className={`${CARD} px-3 py-2`}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div data-intel-levels role="radiogroup" aria-label="宏观事件重要度" className="inline-flex rounded-btn border border-border p-0.5 text-[11px]">
          {LEVELS.map((l) => (
            <button key={l.k} type="button" role="radio" aria-checked={level === l.k} disabled={save.isPending}
              onClick={() => level !== l.k && save.mutate(l.k)}
              className={`rounded px-2 py-0.5 ${level === l.k ? 'bg-accent/20 font-medium text-foreground ring-1 ring-accent/50' : 'text-secondary hover:text-foreground'}`}>
              {l.label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-muted">美东时间 · {cal.source}</span>
      </div>
      {cal.error && <div className="mb-2 text-[11px] text-warning">日历源上次刷新失败，显示的是上一份</div>}
      <div className="space-y-3">
        {visible.map(([day, evs]) => (
          <div key={day} data-cal-day={day}>
            <div className={`mb-1 text-[11px] font-medium ${day === today ? 'text-accent' : 'text-secondary'}`}>
              {`${md(day)} ${weekday(day)}${day === today ? ' · 今天' : ''}`}
            </div>
            <ul className="divide-y divide-border/60">
              {evs.map((e, i) => <EventRow key={`${e.at}-${e.title}-${i}`} e={e} onSymbol={onSymbol} />)}
            </ul>
          </div>
        ))}
        {visible.length === 0 && <div className="py-2 text-xs text-muted">本周无剩余事件</div>}
      </div>
      {groups.length > visible.length || showAll ? (
        <button type="button" data-cal-more onClick={() => setShowAll((x) => !x)} className="mt-2 text-[11px] text-accent hover:underline">
          {showAll ? '只看今天起 3 天' : `全部 ${groups.length} 天 (含已过)`}
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
        <span className="inline-block w-5 shrink-0 rounded border border-accent/40 text-center text-[10px] text-accent">财</span>
        <button type="button" onClick={() => e.symbol && onSymbol(e.symbol)} className="font-medium text-foreground hover:text-accent">{e.symbol}</button>
        <span className="text-secondary">{`财报 · ${TIMING_ZH[e.timing ?? 'unknown'] ?? '时间未定'}`}</span>
        {e.role === '持仓' && <span className="rounded bg-accent/10 px-1 text-[10px] text-accent">持仓</span>}
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
    <li data-cal-event="macro" data-impact={e.impact} className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-x-2 py-1 text-xs">
      <span className="tabular-nums text-muted">{e.time}</span>
      <div className="min-w-0">
        <div className="flex items-baseline gap-x-2">
          <span className={`inline-block w-5 shrink-0 rounded border text-center text-[10px] ${e.impact === 'high' ? 'border-warning/60 text-warning' : 'border-border text-secondary'}`}>{IMPACT_ZH[e.impact ?? 'low']}</span>
          <span className={`${dim} ${e.impact === 'high' ? 'font-medium' : ''}`} title={e.title}>{e.title_zh ?? e.title}</span>
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

function Earnings({ rows, onSymbol }: { rows: IntelEarningsRow[]; onSymbol: (s: string) => void }) {
  if (!rows.length) return <div className={`${CARD} p-4 text-xs text-muted`}>未来 10 个交易日自选无财报</div>
  return (
    <div className={`${CARD} overflow-x-auto`}>
      <table data-intel-earnings className="w-full min-w-[760px] text-xs tabular-nums">
        <thead className="text-[11px] text-secondary">
          <tr className="border-b border-border text-left">
            <th className="px-3 py-2 font-normal">股票</th>
            <th className="px-2 py-2 font-normal">公布</th>
            <th className="px-2 py-2 text-right font-normal" title="华尔街调整后口径: QVeris > Finnhub > Nasdaq">EPS 预期</th>
            <th className="px-2 py-2 text-right font-normal" title="QVeris 一致预期近 30 天变化 (只读缓存)">30 日修正</th>
            <th className="px-2 py-2 text-right font-normal" title="近几次财报反应日涨跌幅绝对值的中位数 / 最大">历史反应 中位 / 最大</th>
            <th className="px-2 py-2 text-right font-normal" title="财报后第一个到期日的平值跨式 / 现价；「×历史」= 隐含波幅 ÷ 历史反应中位数">期权隐含波幅</th>
            <th className="px-2 py-2 text-right font-normal" title="Nasdaq 历史库: 实际高于其预期的次数 / 有数据的次数 (与其自身口径对比)">EPS 超预期</th>
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
                  {r.role === '持仓' && <span className="ml-1 rounded bg-accent/10 px-1 text-[10px] text-accent">持仓</span>}
                </td>
                <td className="whitespace-nowrap px-2 py-1.5 text-secondary">
                  {`${md(r.date)} ${weekday(r.date)} ${TIMING_ZH[r.timing] ?? ''}`}
                  <span className="ml-1 text-muted">{r.days === 0 ? '今天' : `T+${r.days}`}</span>
                  {r.date_conflict && <span data-date-conflict className="ml-1 text-warning" title={`Finnhub 为 ${r.date_conflict}`}>日期待确认</span>}
                </td>
                <td className="px-2 py-1.5 text-right" title={r.eps_source ? `来源 ${r.eps_source}${r.eps_source !== 'Nasdaq' && r.eps_nasdaq != null ? `；Nasdaq ${fmtUsd(r.eps_nasdaq)}（部分公司为 GAAP 口径）` : ''}` : ''}>
                  {fmtUsd(r.eps_consensus ?? r.eps_forecast)}<span data-eps-src className="ml-1 inline-block w-12 text-left text-[10px] text-muted">{r.eps_source ?? ''}</span>
                </td>
                <td className={`px-2 py-1.5 text-right ${sgnCls(rev30(r))}`} title="QVeris 一致预期 EPS 近 30 天变化；预期为负时不算百分比">
                  {rev30(r) != null ? fmtPct(rev30(r)! * 100, 1) : '—'}
                </td>
                <td className="px-2 py-1.5 text-right">{h?.median_abs_move != null ? `±${h.median_abs_move.toFixed(1)}% / ±${h.max_abs_move?.toFixed(1)}%` : '—'}<span className="ml-1 inline-block w-7 text-left text-muted">{h ? `${h.n}次` : ''}</span></td>
                <td data-implied className="px-2 py-1.5 text-right" title={imp == null ? '今天的期权链还没缓存 (盘中 09:35 后自动预热)' : `到期 ${r.implied?.expiration}`}>
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

function News({ news, onSymbol }: { news: IntelPayload['news']; onSymbol: (s: string) => void }) {
  const [f, setF] = useState<NewsFilter>('all')
  if (!news) return <div className={`${CARD} p-4 text-xs text-muted`}>无新闻数据</div>
  const limit = news.limit ?? 40
  const items = filterNews(news.items, f).slice(0, limit)
  const tone = Object.entries(news.tone).sort((a, b) => (b[1].bullish + b[1].bearish) - (a[1].bullish + a[1].bearish)).slice(0, 8)
  return (
    <div className={`${CARD} px-3 py-2`}>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {NEWS_FILTERS.map((x) => (
          <button key={x.k} type="button" data-news-filter={x.k} aria-pressed={f === x.k} onClick={() => setF(x.k)}
            className={`rounded-btn border px-2 py-0.5 text-[11px] ${f === x.k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-secondary hover:text-foreground'}`}>
            {x.label}{news.counts?.[x.k] != null ? ` ${news.counts[x.k]}` : ''}
          </button>
        ))}
      </div>
      {tone.length > 0 && (
        <div data-news-tone className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] tabular-nums">
          {tone.map(([s, t]) => (
            <button key={s} type="button" onClick={() => onSymbol(s)} className="text-secondary hover:text-accent">
              {s} <span className="text-bull">{t.bullish ? `多${t.bullish}` : ''}</span>{t.bullish && t.bearish ? ' ' : ''}<span className="text-bear">{t.bearish ? `空${t.bearish}` : ''}</span>
            </button>
          ))}
        </div>
      )}
      <div data-news-list className="max-h-[520px] overflow-y-auto">
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
      <p className="mt-1 text-[10px] text-muted">{news.disclaimer}</p>
    </div>
  )
}

// ── 数据源状态 ────────────────────────────────────────────────────

const SRC_ZH: Record<string, string> = {
  etfs: 'ETF 快照', vol: '波动率', rates: '利率', macro_calendar: '宏观日历', macro_actuals: '宏观实际值', pcr: '期权 Put/Call',
  breadth: '全市场宽度', etf_daily: 'ETF 官方日K', earnings_upcoming: '财报预期', earnings_history: '财报历史', options_warm: '期权链预热',
}

function SourceState({ d }: { d: IntelPayload }) {
  const bad = Object.entries(d.state).filter(([, v]) => v.error)
  return (
    <section data-intel="sources" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
      <Activity className="h-3.5 w-3.5" />
      {bad.length === 0
        ? <span>数据源正常</span>
        : bad.map(([k, v]) => <span key={k} className="text-warning" title={v.error ?? ''}>{`${SRC_ZH[k] ?? k} 上次刷新失败 (${(v.last_ok_at ?? '').slice(5, 16).replace('T', ' ') || '从未成功'})`}</span>)}
      {Object.keys(d.errors).length > 0 && <span className="text-warning">{`区块读取失败: ${Object.keys(d.errors).join('、')}`}</span>}
    </section>
  )
}
