/** 盘前判断 (0.51.0, 11-06 产品决策: 放在今日多空上面): 08:35 ET 大盘三档判断 (market_mood) 的机会页卡片。
 * 一行 = 结论 + 三档概率 + 星级 + 六项依据各自倾向 + 近 20 日命中; 只展示, 不进打分 / 名单 / 交易。
 * 数据 = /page 的 mood 段 (与情报页同一份); 非今日的判断标「非今日」, 没有判断显示下次生成时刻。 */
import type { MarketMoodItem, MarketMoodPayload, MoodSide } from '@/lib/api'

const STANCE_ZH: Record<MoodSide, string> = { up: '偏多', down: '偏空', range: '观望' }
const LEAN_ZH: Record<MoodSide, string> = { up: '涨', down: '跌', range: '震荡' }
const SHORT: Record<MarketMoodItem['key'], string> = { trend: '趋势', overnight: '隔夜', calendar: '日历', rates: '利率', sectors: '板块', panic: '恐慌' }
const leanCls = (v?: MoodSide | null) => (v === 'up' ? 'text-bull' : v === 'down' ? 'text-bear' : 'text-secondary')
const badgeCls = (v: MoodSide) => (v === 'up' ? 'bg-bull/15 text-bull' : v === 'down' ? 'bg-bear/15 text-bear' : 'bg-elevated text-foreground')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')

export function MoodBar({ mood }: { mood: MarketMoodPayload | null | undefined }) {
  const j = mood?.judgment
  if (!mood || !j) {
    return (
      <section data-mood-bar data-mood-state="none" className="mb-3 flex items-center gap-3 rounded-card border border-border bg-surface px-3 py-2.5">
        <h2 className="shrink-0 text-[15px] font-semibold text-foreground">盘前判断</h2>
        <span className="ml-auto whitespace-nowrap text-[12px] tabular-nums text-muted">{mood?.next_at ? `${md(mood.next_at)} ${etHm(mood.next_at)} ET 生成` : '—'}</span>
      </section>
    )
  }
  const p = j.probs
  const stars = '★'.repeat(Math.max(0, Math.min(5, j.stars))) + '☆'.repeat(Math.max(0, 5 - Math.min(5, j.stars)))
  const st = mood.stats
  const order: MoodSide[] = ['up', 'range', 'down']
  return (
    <section data-mood-bar data-mood-state={mood.is_today ? 'today' : 'old'}
      className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-card border border-border bg-surface px-3 py-2.5 tabular-nums">
      <h2 className="shrink-0 text-[15px] font-semibold text-foreground">盘前判断</h2>
      <span data-mood-stance className={`shrink-0 rounded-btn px-2.5 py-0.5 text-[14px] font-bold ${badgeCls(j.stance)}`}>{STANCE_ZH[j.stance]}</span>
      <span data-mood-probs className="shrink-0 whitespace-nowrap text-[13px] text-secondary">
        {order.map((k, i) => (
          <span key={k}>{i > 0 && <span className="text-muted">{' · '}</span>}{LEAN_ZH[k]}{' '}<b className={`font-semibold ${leanCls(k)}`}>{p[k] ?? '—'}</b></span>
        ))}
      </span>
      <span data-mood-stars className="shrink-0 whitespace-nowrap text-[13px] tracking-[1px] text-warning" title={`把握 ${j.stars}/5`}>{stars}</span>
      <span data-mood-items className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 text-[12.5px]">
        {j.items.map((it) => (
          <span key={it.key} data-mood-item={it.key} title={it.note || it.text} className="whitespace-nowrap">
            <span className="text-muted">{SHORT[it.key] ?? it.label}</span>{' '}
            <b className={`font-semibold ${it.status === 'ok' ? leanCls(it.lean) : 'text-muted'}`}>{it.status === 'ok' && it.lean ? LEAN_ZH[it.lean] : it.status === 'stale' ? '旧' : '缺'}</b>
          </span>
        ))}
      </span>
      <span data-mood-meta className="ml-auto flex shrink-0 items-center gap-2 whitespace-nowrap text-[12px] text-muted">
        {!mood.is_today && <span data-mood-old className="rounded-btn border border-warning/50 bg-warning/10 px-1.5 font-semibold leading-5 text-warning">非今日</span>}
        <span>{`${md(j.day)} ${etHm(j.generated_at)} ET`}</span>
        {st && st.n > 0 && <span data-mood-hit>{`近 ${st.n} 日中 ${st.hit}`}</span>}
      </span>
    </section>
  )
}
