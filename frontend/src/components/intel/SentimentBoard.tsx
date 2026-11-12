/**
 * 舆情异动 (情报页, 放在日历与新闻之后): 新闻、Reddit 讨论、维基浏览量明显多于该股平时水平时列出。
 * 卡片只放代码、利多 / 利空、事件、三类数量; 点开看采集到的新闻、帖子与浏览量。
 * 只读 GET /api/intel/today 的 sentiment 段; 仅观察, 不参与下单。
 */
import { useState } from 'react'
import { Flame, Info } from 'lucide-react'
import type { IntelSentiment, SentimentFamily, SentimentRow, SentimentSourceStat } from '@/lib/api'
import { SectionTitle } from '@/components/data/SectionTitle'
import { Sheet } from '@/components/opportunities/Sheet'
import { badge } from '@/components/ui/badge'

const CARD = 'rounded-card border border-border bg-surface'
const SHOW = 8
export const HINT = '新闻、Reddit 讨论、维基浏览量明显多于这只股票平时的水平时列出；卡片上的「几倍」是和平时比。「重点」表示至少两类同时变多，且有利多或利空的重要消息。仅观察，不参与下单。'
export const DIR_ZH: Record<string, string> = { bullish: '利多', bearish: '利空', mixed: '多空都有', none: '方向不明' }
export const EVENT_ZH: Record<string, string> = {
  regulatory: '监管', litigation: '诉讼', management: '管理层事件', guidance: '业绩指引', earnings: '财报', mna: '并购',
  dilution: '增发稀释', rating: '评级调整', product: '产品', contract: '合同订单', macro: '宏观',
}
const FAMILY_ZH: Record<SentimentFamily, string> = { news: '新闻', social: 'Reddit', attention: '维基浏览' }
const FAMILY_LONG: Record<SentimentFamily, string> = { news: '新闻', social: 'Reddit 讨论', attention: '维基浏览量' }
const ORDER: SentimentFamily[] = ['news', 'social', 'attention']
const SRC_OF: Record<SentimentFamily, string> = { news: 'news', social: 'reddit', attention: 'wiki' }

/** 一类来源的主读数 (新闻用总条数, 不用高重要度条数) */
function stat(r: SentimentRow, f: SentimentFamily): SentimentSourceStat | null {
  return r.families?.[f]?.sources?.[SRC_OF[f]] ?? null
}

export function fmtCount(n: number): string {
  if (n >= 10_000) return `${(n / 10_000).toFixed(n >= 100_000 ? 0 : 1).replace(/\.0$/, '')}万`
  return Math.round(n).toLocaleString('en-US')
}

/** 是平时的几倍 (平时为 0 按 1 算) */
export function fmtTimes(x: number, med: number): string {
  const m = x / Math.max(med, 1)
  return m >= 10 ? `${Math.round(m)}` : m.toFixed(1).replace(/\.0$/, '')
}

/** 卡片上的倍数: 每类取异常度最高的那个读数, 只列明显变多 (≥1.5 倍) 的类; 固定按 新闻 / Reddit / 维基 排, 方便上下对比 */
export function cardTimes(r: SentimentRow): { f: SentimentFamily; label: string; times: string }[] {
  const out: { f: SentimentFamily; label: string; times: string; m: number }[] = []
  for (const f of ORDER) {
    const fam = r.families?.[f]
    if (!fam) continue
    const [key, s] = Object.entries(fam.sources).sort((a, b) => b[1].z - a[1].z)[0] ?? []
    if (!s) continue
    const m = s.x / Math.max(s.med, 1)
    if (m < 1.5) continue
    out.push({ f, label: key === 'news_hi' ? '重要新闻' : FAMILY_ZH[f], times: fmtTimes(s.x, s.med), m })
  }
  return out.map(({ f, label, times }) => ({ f, label, times }))
}

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

/** 一句话说清为什么上榜 */
export function reason(r: SentimentRow): string {
  const up = ORDER.filter((f) => r.families?.[f]?.anom).map((f) => FAMILY_LONG[f])
  const head = up.length ? `${up.join('、')}明显多于平时` : '讨论量高于平时'
  const tail = r.direction === 'bullish' ? '；新闻以利多为主' : r.direction === 'bearish' ? '；新闻以利空为主'
    : r.direction === 'mixed' ? '；新闻利多利空都有' : ''
  return `${head}${tail}。`
}

const dirBadge = (d: string) => (d === 'bullish' ? badge('status', 'bull') : d === 'bearish' ? badge('status', 'bear') : badge('status', 'neutral'))

export function SentimentBoard({ data, failed, onSymbol }: {
  data: IntelSentiment | null | undefined; failed?: boolean; onSymbol?: (s: string) => void
}) {
  const [open, setOpen] = useState<SentimentRow | null>(null)
  const [all, setAll] = useState(false)
  const rows = data?.rows ?? []
  const shown = all ? rows : rows.slice(0, SHOW)
  return (
    <section data-intel="sentiment" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex items-center gap-1.5">
          <SectionTitle icon={Flame}>{`舆情异动${data?.as_of ? ` · ${data.total} 只` : ''}`}</SectionTitle>
          <span data-sentiment-hint title={HINT} aria-label={HINT} className="text-muted"><Info className="h-3.5 w-3.5" /></span>
        </div>
        {data?.as_of && <span data-sentiment-asof className="text-[11px] text-muted">{`倍数为与平时相比 · 更新于 ${fmtEt(data.as_of).slice(6)} ET`}</span>}
      </div>
      {failed || !data ? (
        <div data-sentiment-state="failed" className={`${CARD} p-4 text-[12px] text-warning`}>读取失败</div>
      ) : !data.as_of ? (
        <div data-sentiment-state="pending" className={`${CARD} p-4 text-[12px] text-muted`}>首次采集中</div>
      ) : rows.length === 0 ? (
        <div data-sentiment-state="empty" className={`${CARD} p-4 text-[12px] text-muted`}>近 24 小时没有明显异动</div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {shown.map((r) => <SentimentCard key={r.symbol} r={r} onOpen={() => setOpen(r)} />)}
          </div>
          {rows.length > SHOW && (
            <button type="button" data-sentiment-more onClick={() => setAll(!all)} className="text-[12px] text-accent hover:underline">
              {all ? '收起' : `展开全部 ${rows.length} 只`}
            </button>
          )}
        </>
      )}
      {open && <SentimentSheet r={open} onClose={() => setOpen(null)} onSymbol={onSymbol} />}
    </section>
  )
}

function SentimentCard({ r, onOpen }: { r: SentimentRow; onOpen: () => void }) {
  const times = cardTimes(r)
  return (
    <button type="button" data-sentiment-card={r.symbol} onClick={onOpen}
      className={`${CARD} flex min-w-0 flex-col gap-1.5 p-3 text-left hover:bg-elevated`}>
      <div className="flex items-center justify-between gap-1.5">
        <span className="font-mono text-[14px] font-semibold text-foreground">{r.symbol}</span>
        <span className="flex items-center gap-1">
          {r.role === '持仓' && <span className={badge('limit')}>持仓</span>}
          {r.grade === 'A' && <span data-focus className={badge('warn')}>重点</span>}
        </span>
      </div>
      <div className="flex min-w-0 items-center gap-1.5">
        <span className={dirBadge(r.direction)}>{DIR_ZH[r.direction] ?? '方向不明'}</span>
        <span className="truncate text-[12px] text-secondary">{r.event_type ? (EVENT_ZH[r.event_type] ?? '其他消息') : '无明确事件'}</span>
      </div>
      <div data-card-times className="flex flex-wrap gap-x-2.5 text-[12px] tabular-nums text-foreground">
        {times.length ? times.map((t) => <span key={t.f}>{`${t.label} ${t.times} 倍`}</span>)
          : <span className="text-secondary">略高于平时</span>}
      </div>
    </button>
  )
}

function Block({ f, r, children }: { f: SentimentFamily; r: SentimentRow; children: React.ReactNode }) {
  const s = stat(r, f)
  const unit = f === 'news' ? '条' : f === 'social' ? '帖' : '次'
  const span = f === 'attention' && s?.day ? `${s.day.slice(5)} 全天` : '近 24 小时'
  return (
    <div data-sentiment-block={f} className="space-y-1.5">
      <div className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
        <span className="font-medium text-foreground">{FAMILY_LONG[f]}</span>
        {s ? (
          <span className={`tabular-nums ${r.families?.[f]?.anom ? 'text-foreground' : 'text-secondary'}`}>
            {`${span} ${fmtCount(s.x)} ${unit}，平时约 ${fmtCount(s.med)} ${unit}`}
          </span>
        ) : <span className="text-muted">暂无数据</span>}
      </div>
      {children}
    </div>
  )
}

function SentimentSheet({ r, onClose, onSymbol }: { r: SentimentRow; onClose: () => void; onSymbol?: (s: string) => void }) {
  const wiki = stat(r, 'attention')
  return (
    <Sheet title={`${r.symbol} · 舆情异动`} onClose={onClose} attr={{ 'data-sentiment-sheet': r.symbol }}
      footer={(
        <>
          {onSymbol && (
            <button type="button" onClick={() => { onClose(); onSymbol(r.symbol) }}
              className="rounded-btn border border-border px-2.5 py-1 text-secondary hover:bg-elevated">打开个股</button>
          )}
          <span className="text-muted">仅观察，不参与下单</span>
        </>
      )}>
      <div className="space-y-5">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            {r.grade === 'A' && <span className={badge('warn')}>重点</span>}
            <span className={dirBadge(r.direction)}>{DIR_ZH[r.direction] ?? '方向不明'}</span>
            {r.event_type && <span className={badge('limit')}>{EVENT_ZH[r.event_type] ?? '其他消息'}</span>}
          </div>
          <p data-sentiment-reason className="text-[13px] text-foreground">{reason(r)}</p>
          <p className="text-[11px] text-muted">时间均为美东时间</p>
        </div>
        <Block f="news" r={r}>
          {r.families?.news?.sources?.news_hi && (
            <p data-news-important className="text-[12px] tabular-nums text-secondary">
              {`其中重要消息 ${fmtCount(r.families.news.sources.news_hi.x)} 条，平时约 ${fmtCount(r.families.news.sources.news_hi.med)} 条`}
            </p>
          )}
          {(r.headlines ?? []).map((h, i) => (
            <div key={`${h.at}-${i}`} className="flex gap-2 text-[12px]">
              <span className="shrink-0 tabular-nums text-muted">{fmtEt(h.at)}</span>
              <span className={h.direction === 'bearish' ? 'text-bear' : h.direction === 'bullish' ? 'text-bull' : 'text-foreground'}>
                {h.zh || h.en || '—'}
              </span>
            </div>
          ))}
        </Block>
        <Block f="social" r={r}>
          {(r.posts ?? []).map((p) => (
            <div key={p.id ?? p.at} className="flex gap-2 text-[12px]">
              <span className="shrink-0 tabular-nums text-muted">{fmtEt(p.at)}</span>
              <a href={p.id ? `https://www.reddit.com/comments/${p.id}` : undefined} target="_blank" rel="noopener noreferrer"
                className="min-w-0 text-foreground hover:underline">
                {p.title}<span className="ml-1.5 text-muted">{`r/${p.sub ?? ''}`}</span>
              </a>
            </div>
          ))}
        </Block>
        <Block f="attention" r={r}>
          {wiki?.article && <p className="text-[12px] text-muted">{`条目：${wiki.article.replace(/_/g, ' ')}`}</p>}
        </Block>
      </div>
    </Sheet>
  )
}
