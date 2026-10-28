import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Loader2, Newspaper } from 'lucide-react'
import { api, type NewsItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 方向色与全站一致: 红=利好, 绿=利空, 灰=中性 (红涨绿跌口径)。 */
const DIRECTION: Record<string, { label: string; cls: string }> = {
  bullish: { label: '偏利好', cls: 'text-bull border-bull/40 bg-bull/10' },
  bearish: { label: '偏利空', cls: 'text-bear border-bear/40 bg-bear/10' },
  neutral: { label: '中性', cls: 'text-secondary border-border bg-elevated' },
}
const IMPORTANCE: Record<string, string> = { high: '重要', medium: '一般', low: '次要' }

function ageLabel(h: number | null): string {
  if (h == null) return ''
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} 分钟前`
  if (h < 48) return `${Math.round(h)} 小时前`
  return `${Math.round(h / 24)} 天前`
}

/** 发布时间统一按美东显示 (与全站时间口径一致)。 */
function etLabel(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'America/New_York', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(d) + ' 美东'
}

export function NewsTag({ item }: { item: NewsItem }) {
  if (!item.tagged) {
    return <span className="rounded border border-border px-1.5 py-px text-[10px] text-muted">未打标</span>
  }
  const dir = DIRECTION[item.direction ?? 'neutral'] ?? DIRECTION.neutral
  return (
    <>
      {item.event_label && (
        <span className="rounded border border-border bg-elevated px-1.5 py-px text-[10px] text-secondary">{item.event_label}</span>
      )}
      <span data-news-direction={item.direction} className={`rounded border px-1.5 py-px text-[10px] ${dir.cls}`}>{dir.label}</span>
      {item.importance && item.importance !== 'low' && (
        <span className={`rounded px-1.5 py-px text-[10px] ${item.importance === 'high' ? 'bg-warning/10 text-warning' : 'text-muted'}`}>
          {IMPORTANCE[item.importance]}
        </span>
      )}
      {item.rehash && <span className="text-[10px] text-muted">旧闻</span>}
    </>
  )
}

export function NewsRow({ item, compact = false }: { item: NewsItem; compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const hasDetail = !!item.summary || (!item.translated && !!item.original_headline)
  return (
    <li data-news-item={item.id} className="border-b border-border/60 py-2 last:border-0">
      <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-muted tabular-nums">
        <span title={etLabel(item.published_at)}>{ageLabel(item.age_hours)}</span>
        <span>·</span>
        <span>{item.sources.join('、')}</span>
        <NewsTag item={item} />
      </div>
      <button
        type="button"
        onClick={() => hasDetail && setOpen(v => !v)}
        className={`mt-1 block w-full text-left text-[13px] leading-snug text-foreground ${hasDetail ? 'cursor-pointer hover:text-accent' : 'cursor-default'}`}
      >
        {item.headline}
        {!item.translated && <span className="ml-1.5 text-[10px] text-muted">（未翻译）</span>}
      </button>
      {open && !compact && (
        <div className="mt-1 space-y-1 text-[12px] leading-relaxed text-secondary">
          {item.summary && <p>{item.summary}</p>}
          {item.translated && <p className="text-[11px] text-muted">原文：{item.original_headline}</p>}
          {item.url && (
            <a href={item.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline">
              原文链接 <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      )}
    </li>
  )
}

/** 个股新闻面板 (个股弹窗「新闻」视图)。默认只看主角新闻, 可展开全部。 */
export function StockNewsPanel({ symbol, height }: { symbol: string; height?: number }) {
  const [showAll, setShowAll] = useState(false)
  const q = useQuery({
    queryKey: QK.stockNews(symbol),
    queryFn: () => api.stockNews(symbol, 7),
    enabled: !!symbol,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  })
  const items = q.data?.items ?? []
  const main = useMemo(
    () => items.filter(v => (v.tagged ? v.relevance === 'primary' && !v.rehash : v.mention === 'headline')),
    [items],
  )
  const shown = showAll ? items : main

  return (
    <div data-stock-news className="flex flex-col overflow-hidden" style={height ? { height } : undefined}>
      <div className="flex shrink-0 items-center justify-between gap-2 px-1 pb-2 text-[11px] text-secondary">
        <span className="inline-flex items-center gap-1.5">
          <Newspaper className="h-3.5 w-3.5" />
          近 7 日 · {showAll ? `全部 ${items.length} 条` : `主角新闻 ${main.length} 条`}
        </span>
        {items.length > main.length && (
          <button type="button" onClick={() => setShowAll(v => !v)} className="text-accent hover:underline">
            {showAll ? '只看主角新闻' : `显示全部 ${items.length} 条`}
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        {q.isLoading && (
          <div className="flex items-center gap-2 py-6 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" />新闻加载中…</div>
        )}
        {q.isError && <div className="py-6 text-xs text-danger">新闻读取失败，请稍后重试</div>}
        {!q.isLoading && !q.isError && shown.length === 0 && (
          <div className="py-6 text-xs text-muted">
            {items.length === 0 ? '近 7 日没有点名该股的新闻（已过滤只挂标签、正文未提及的条目）' : '近 7 日没有以该股为主角的新闻'}
          </div>
        )}
        <ul>{shown.map(v => <NewsRow key={v.id} item={v} />)}</ul>
      </div>
      {q.data?.disclaimer && <div className="shrink-0 pt-1.5 text-[10px] text-muted">{q.data.disclaimer}</div>}
    </div>
  )
}

/** 列表/卡片上的小角标: 24 小时主角新闻条数; 48 小时内有高重要度利空时变色。 */
export function NewsBadge({ count, highBearish }: { count: number; highBearish?: boolean }) {
  if (!count && !highBearish) return null
  return (
    <span
      data-news-badge
      title={highBearish ? '48 小时内有重要利空新闻（AI 判读）' : '24 小时内主角新闻条数'}
      className={`inline-flex items-center gap-0.5 rounded px-1 py-px text-[9px] leading-tight ${
        highBearish ? 'border border-bear/50 bg-bear/10 text-bear' : 'bg-elevated text-secondary'
      }`}
    >
      <Newspaper className="h-2.5 w-2.5" />
      {count || '!'}
    </span>
  )
}
