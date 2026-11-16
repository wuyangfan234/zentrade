/** 大盘方向 (2026-11-15 B 方案置顶): 盘前判断 (08:35 ET 大盘三档, market_mood) + 盘中实况 (desk.regime)。
 * 两个来源并排, 各自标时点, 不合成分数; 只展示, 不进打分 / 名单 / 交易。六项依据放在结论悬停里。
 * 非当日的判断标「非当日」; 没有判断显示下次生成时刻。 */
import type { MarketMoodPayload, MoodSide, OppDesk } from '@/lib/api'
import { badge } from '@/components/ui/badge'
import { REGIME_ZH } from '@/components/opportunities/Desk'

const STANCE_ZH: Record<MoodSide, string> = { up: '偏多', down: '偏空', range: '观望' }
const LEAN_ZH: Record<MoodSide, string> = { up: '涨', down: '跌', range: '震荡' }
const PROB_ZH: Record<MoodSide, string> = { up: '上涨', range: '震荡', down: '下跌' }
const SHORT: Record<string, string> = { trend: '趋势', overnight: '隔夜', calendar: '日历', rates: '利率', sectors: '板块', panic: '恐慌' }
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v?: number | null) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%` : '—')
const sgn = (v?: number | null) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull dark:text-red-400' : 'text-bear')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')
const stanceCls = (v: MoodSide) => (v === 'up' ? 'bg-bull/15 text-bull dark:text-red-400' : v === 'down' ? 'bg-bear/15 text-emerald-800 dark:text-bear' : 'bg-elevated text-foreground')
const BAR: Record<MoodSide, string> = { up: 'bg-bull', range: 'bg-zinc-400 dark:bg-zinc-500', down: 'bg-bear' }
 /** 结论底色: 偏多淡红 / 偏空淡绿 / 观望不着色 (红涨绿跌) */
const TINT: Record<MoodSide, string> = { up: 'border-bull/40 bg-gradient-to-r from-bull/[0.07] to-transparent', down: 'border-bear/40 bg-gradient-to-r from-bear/[0.07] to-transparent', range: 'border-border' }
const TXT: Record<MoodSide, string> = { up: 'text-bull dark:text-red-400', range: 'text-foreground', down: 'text-bear' }

function Live({ desk, error }: { desk: OppDesk | null; error?: string }) {
  const r = desk?.regime
  const k = r?.key ?? null
  const cls = k === 'up' ? 'bg-bull/15 text-bull dark:text-red-400' : k === 'down' ? 'bg-bear/15 text-emerald-800 dark:text-bear' : k ? 'bg-elevated text-foreground' : 'bg-elevated text-muted'
  return (
    <div data-hero-live className="flex min-w-0 flex-col gap-1.5 md:items-end md:border-l md:border-border md:pl-6">
      <div className="flex items-center gap-2">
        <span className="text-[12px] text-secondary">盘中实况</span>
        <span data-regime={k ?? 'none'} className={`rounded-btn px-2 py-0.5 text-[13px] font-bold ${cls}`}>{error ? '读取失败' : k ? REGIME_ZH[k] : '—'}</span>
        {r?.at && k && <span className="text-[12px] tabular-nums text-muted">{`${etHm(r.at)} ET`}</span>}
      </div>
      {r && (fin(r.spy) || fin(r.qqq) || r.n > 0) && (
        <div data-regime-index className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[13px] tabular-nums text-secondary md:justify-end">
          <span className="whitespace-nowrap">{'标普 '}<b className={`font-semibold ${sgn(r.spy)}`}>{pc(r.spy)}</b></span>
          <span className="whitespace-nowrap">{'纳指 '}<b className={`font-semibold ${sgn(r.qqq)}`}>{pc(r.qqq)}</b></span>
          {r.n > 0 && <span data-regime-breadth className="whitespace-nowrap" title={`涨 ≥3% ${r.up3} · 跌 ≥1% ${r.down}`}>{'自选涨 ≥1% '}<b className="font-semibold text-foreground">{`${r.up}/${r.n}`}</b></span>}
        </div>
      )}
    </div>
  )
}

export function MarketHero({ mood, desk, moodError, deskError }: { mood: MarketMoodPayload | null | undefined; desk: OppDesk | null; moodError?: string; deskError?: string }) {
  const j = mood?.judgment
  const order: MoodSide[] = ['up', 'range', 'down']
  const items = j?.items.map((it) => `${SHORT[it.key] ?? it.label} ${it.status === 'ok' && it.lean ? LEAN_ZH[it.lean] : it.status === 'stale' ? '旧' : '缺'}`).join(' · ')
  const st = mood?.stats
  return (
    <section data-market-hero data-mood-state={!j ? 'none' : mood?.is_today ? 'today' : 'old'}
      className={`mb-3 rounded-card border bg-surface px-4 py-3.5 tabular-nums max-md:px-3 ${j ? TINT[j.stance] : 'border-border'}`}>
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
        <h2 className="shrink-0 text-[18px] font-semibold text-foreground">大盘方向</h2>
        <span className={badge('limit')} title="盘前判断与盘中实况各自独立 · 不合成分数 · 不下单">仅参考</span>
        <span data-mood-meta className="ml-auto flex flex-wrap items-center justify-end gap-x-2 gap-y-1 text-[12px] text-muted">
          {mood && j && !mood.is_today && <span data-mood-old className={badge('warn')}>非当日</span>}
          {j ? <span className="whitespace-nowrap">{`盘前判断 ${md(j.day)} ${etHm(j.generated_at)} ET`}</span>
            : <span className="whitespace-nowrap">{moodError ? '盘前判断读取失败' : mood?.next_at ? `盘前判断 ${md(mood.next_at)} ${etHm(mood.next_at)} ET 生成` : '盘前判断 —'}</span>}
          {st && st.n > 0 && <span data-mood-hit className="whitespace-nowrap">{`近 ${st.n} 日判对 ${st.hit} 次`}</span>}
        </span>
      </div>
      <div className="grid items-center gap-x-8 gap-y-3 md:grid-cols-[auto_minmax(0,1fr)_auto]">
        {j ? (
          <>
            <div className="flex items-center gap-3">
              <span data-mood-stance title={items} className={`rounded-btn px-3.5 py-1 text-[24px] font-bold leading-8 ${stanceCls(j.stance)}`}>{STANCE_ZH[j.stance]}</span>
              <span className="flex flex-col text-[12px] leading-4 text-secondary">
                <span>把握</span>
                <span data-mood-stars className="whitespace-nowrap text-[14px] tracking-[1px] text-foreground" title={`把握 ${j.stars}/5`}>
                  {'★'.repeat(Math.max(0, Math.min(5, j.stars)))}<span className="text-muted">{'☆'.repeat(Math.max(0, 5 - Math.min(5, j.stars)))}</span>
                  <span className="ml-1.5 text-[12px] tracking-normal text-secondary">{`${Math.max(0, Math.min(5, j.stars))}/5`}</span>
                </span>
              </span>
            </div>
            <div className="min-w-0">
              <div data-mood-bar className="flex h-3 gap-[2px] overflow-hidden rounded-full">
                {order.map((k) => <i key={k} className={`block h-full ${BAR[k]}`} style={{ width: `${Math.max(j.probs[k] ?? 0, 0)}%` }} />)}
              </div>
              <div data-mood-probs className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-secondary">
                {order.map((k) => (
                  <span key={k} className="inline-flex items-center gap-1.5 whitespace-nowrap">
                    <i className={`h-2 w-2 rounded-full ${BAR[k]}`} />{`${PROB_ZH[k]} `}<b className={`font-semibold ${TXT[k]}`}>{`${j.probs[k] ?? '—'}%`}</b>
                  </span>
                ))}
              </div>
            </div>
          </>
        ) : <div data-mood-none className="text-[13px] text-muted md:col-span-2">盘前判断 —</div>}
        <Live desk={desk} error={deskError} />
      </div>
    </section>
  )
}
