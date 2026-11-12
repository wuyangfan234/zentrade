/**
 * 舆情作战中心第一期: 情报页顶部区块。只读 GET /api/intel/today 的 sentiment 段 (采集线程每轮写的看板),
 * 只展示评级, 不生成模拟单、不进交易链。每行: 代码、评级、方向、热度、类型、首发、来源数; 点开看热度线、来源分布、中文标题。
 */
import { useState } from 'react'
import { Flame } from 'lucide-react'
import type { IntelSentiment, SentimentFamily, SentimentRow } from '@/lib/api'
import { SectionTitle } from '@/components/data/SectionTitle'

const CARD = 'rounded-card border border-border bg-surface'
export const FAMILY_ZH: Record<SentimentFamily, string> = { news: '财经新闻', media: '全球媒体', social: '社交', attention: '关注' }
const FAMILY_ORDER: SentimentFamily[] = ['news', 'media', 'social', 'attention']
export const SOURCE_ZH: Record<string, string> = {
  news: '新闻条数', news_hi: '高重要度', gdelt: 'GDELT', stocktwits: 'StockTwits', apewisdom: 'ApeWisdom', reddit: 'Reddit', wiki: '维基',
}
const STATUS_ZH: Record<string, string> = { ok: '正常', missing: '缺源', breaker: '熔断', unconfigured: '未配置', pending: '待首轮' }
export const DIR_ZH: Record<string, string> = { bullish: '偏多', bearish: '偏空', mixed: '分歧', none: '无' }
export const EVENT_ZH: Record<string, string> = {
  regulatory: '监管', litigation: '诉讼', management: '管理层', guidance: '指引', earnings: '财报', mna: '并购', dilution: '增发',
  rating: '评级', product: '产品', contract: '合同', macro: '宏观', other: '其他',
}
const SRC_ORDER = ['gdelt', 'stocktwits', 'apewisdom', 'reddit', 'wiki']
const GRID = 'grid grid-cols-[4.5rem_2rem_3rem_minmax(0,1fr)] items-center gap-x-3 md:grid-cols-[5rem_2.5rem_3.5rem_7rem_5rem_7.5rem_3.5rem]'

const ET_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
})
/** UTC ISO → 「MM-DD HH:MM」美东 */
export function fmtEt(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const p = Object.fromEntries(ET_FMT.formatToParts(d).map((x) => [x.type, x.value]))
  return `${p.month}-${p.day} ${p.hour === '24' ? '00' : p.hour}:${p.minute}`
}

const dirCls = (d: string) => (d === 'bullish' ? 'text-bull' : d === 'bearish' ? 'text-bear' : 'text-secondary')
const gradeCls = (g: string) => (g === 'A'
  ? 'bg-rose-700 text-white'
  : 'border border-amber-700 text-amber-700 dark:border-amber-400 dark:text-amber-400')

export function SentimentBoard({ data, failed, onSymbol }: {
  data: IntelSentiment | null | undefined; failed?: boolean; onSymbol?: (s: string) => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const c = data?.counts ?? {}
  const rows = data?.rows ?? []
  return (
    <section data-intel="sentiment" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <SectionTitle icon={Flame}>{`舆情 · A ${c.A ?? 0} · B ${c.B ?? 0} · C ${c.C ?? 0}`}</SectionTitle>
        {data?.as_of && <span data-sentiment-asof className="text-[11px] text-muted">{`截至 ${fmtEt(data.as_of)} ET · ${data.symbols} 只`}</span>}
      </div>
      {failed || !data
        ? <div data-sentiment-state="failed" className={`${CARD} p-4 text-sm text-warning`}>读取失败</div>
        : (
          <div className={`${CARD} px-3 py-2`}>
            {!data.as_of && <div data-sentiment-state="pending" className="py-3 text-xs text-muted">待首轮</div>}
            {data.as_of && rows.length === 0 && (
              <div data-sentiment-state="empty" className="py-3 text-xs text-muted">{`无 A/B 级 · C 级 ${c.C ?? 0} 只`}</div>
            )}
            {rows.length > 0 && (
              <div role="table" className="text-sm">
                <div role="row" className={`${GRID} hidden border-b border-border pb-1.5 text-[11px] text-muted md:grid`}>
                  <span>代码</span><span>评级</span><span>方向</span><span>热度</span><span>类型</span><span>首发</span><span>来源数</span>
                </div>
                {rows.map((r) => (
                  <div key={r.symbol} className="border-b border-border last:border-b-0">
                    <button type="button" data-sentiment-row={r.symbol} aria-expanded={open === r.symbol}
                      onClick={() => setOpen(open === r.symbol ? null : r.symbol)}
                      className={`${GRID} w-full py-2 text-left hover:bg-elevated`}>
                      <span className="font-mono font-semibold text-foreground">{r.symbol}</span>
                      <span><span data-grade={r.grade} className={`inline-flex h-5 w-5 items-center justify-center rounded text-[11px] font-semibold ${gradeCls(r.grade)}`}>{r.grade}</span></span>
                      <span className={dirCls(r.direction)}>{DIR_ZH[r.direction] ?? '无'}</span>
                      <span className="flex items-center gap-2">
                        <span className="tabular-nums text-foreground">{r.heat}</span>
                        <span className="hidden h-1.5 w-14 overflow-hidden rounded bg-elevated md:block">
                          <span className="block h-full bg-amber-700 dark:bg-amber-400" style={{ width: `${Math.min(100, Math.max(0, r.heat))}%` }} />
                        </span>
                      </span>
                      <span className="hidden text-secondary md:block">{r.event_type ? (EVENT_ZH[r.event_type] ?? '其他') : '—'}</span>
                      <span className="hidden tabular-nums text-secondary md:block">{fmtEt(r.first_at)}</span>
                      <span className="hidden tabular-nums text-secondary md:block">{r.n_sources}</span>
                      <span className="col-span-4 text-[11px] text-muted md:hidden">
                        {`${r.event_type ? (EVENT_ZH[r.event_type] ?? '其他') : '—'} · 首发 ${fmtEt(r.first_at)} · 来源 ${r.n_sources}`}
                      </span>
                    </button>
                    {open === r.symbol && <SentimentDetail r={r} line={data.heat_line ?? null} onSymbol={onSymbol} />}
                  </div>
                ))}
              </div>
            )}
            <div data-sentiment-sources className="flex flex-wrap gap-x-3 gap-y-0.5 border-t border-border pt-1.5 text-[11px] text-muted">
              {SRC_ORDER.map((k) => {
                const st = data.sources?.[k]?.status ?? 'pending'
                return (
                  <span key={k} className={st === 'missing' || st === 'breaker' ? 'text-warning' : ''}>
                    {`${SOURCE_ZH[k]} ${STATUS_ZH[st] ?? '待首轮'}`}
                  </span>
                )
              })}
              {data.board_error && <span className="text-warning">评级计算失败</span>}
            </div>
          </div>
        )}
    </section>
  )
}

function HeatLine({ pts, line }: { pts: [string, number][]; line: number | null }) {
  const W = 300
  const H = 60
  if (pts.length < 2) return <div className="text-xs text-muted">—</div>
  const x = (i: number) => (i / (pts.length - 1)) * W
  const y = (v: number) => H - (Math.min(100, Math.max(0, v)) / 100) * H
  return (
    <div>
      <svg data-sentiment-line viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-16 w-full">
        {line != null && <line x1={0} x2={W} y1={y(line)} y2={y(line)} className="stroke-amber-700 dark:stroke-amber-400" strokeDasharray="4 3" strokeWidth={1} />}
        <polyline fill="none" className="stroke-rose-700 dark:stroke-rose-400" strokeWidth={1.5}
          points={pts.map(([, v], i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />
      </svg>
      <div className="flex justify-between text-[10px] text-muted">
        <span>{fmtEt(pts[0][0])}</span>
        {line != null && <span>{`过线 ${line}`}</span>}
        <span>{fmtEt(pts[pts.length - 1][0])}</span>
      </div>
    </div>
  )
}

function SentimentDetail({ r, line, onSymbol }: { r: SentimentRow; line: number | null; onSymbol?: (s: string) => void }) {
  return (
    <div data-sentiment-detail={r.symbol} className="grid gap-4 bg-elevated px-3 py-3 text-xs md:grid-cols-3">
      <div className="space-y-1">
        <div className="text-[11px] text-muted">热度线 · 48 小时</div>
        <HeatLine pts={r.line ?? []} line={line} />
      </div>
      <div className="space-y-1">
        <div className="text-[11px] text-muted">来源分布</div>
        {FAMILY_ORDER.map((f) => {
          const fam = r.families?.[f]
          // 没有数据或基线不足 (如社交源刚开始积累): 不计入热度分母
          if (!fam) return <div key={f} data-family-missing={f} className="flex justify-between text-muted"><span>{FAMILY_ZH[f]}</span><span>未计入</span></div>
          return (
            <div key={f} data-family={f}>
              <div className="flex justify-between">
                <span className={fam.anom ? 'font-medium text-foreground' : 'text-secondary'}>{FAMILY_ZH[f]}</span>
                <span className="tabular-nums text-foreground">{fam.heat}</span>
              </div>
              {Object.entries(fam.sources).map(([k, s]) => (
                <div key={k} className="flex justify-between pl-2 text-[11px] text-muted">
                  <span>{SOURCE_ZH[k] ?? k}</span>
                  <span className={`tabular-nums ${s.anom ? 'text-warning' : ''}`}>{`${s.x} · 基线 ${s.med} · ${s.z >= 0 ? '+' : ''}${s.z}σ`}</span>
                </div>
              ))}
            </div>
          )
        })}
      </div>
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span>标题</span>
          {onSymbol && <button type="button" onClick={() => onSymbol(r.symbol)} className="text-accent hover:underline">个股</button>}
        </div>
        {(r.headlines ?? []).length === 0 && <div className="text-muted">—</div>}
        {(r.headlines ?? []).map((h, i) => (
          <div key={`${h.at}-${i}`} className="flex gap-2">
            <span className="shrink-0 tabular-nums text-muted">{fmtEt(h.at)}</span>
            <span className={h.direction === 'bearish' ? 'text-bear' : h.direction === 'bullish' ? 'text-bull' : 'text-foreground'}>{h.zh || h.en || '—'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
