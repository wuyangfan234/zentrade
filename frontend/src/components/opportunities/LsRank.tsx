/** 模型打分 (今日多空, 2026-11-15 B 方案收进右栏): 看涨 5 / 看跌 5 只列代码与排名, 全部名单与前向结算在抽屉。
 * 模型独立打分, 只作参考: 不据此生成策略机会、不进交易环 / 不写台账 / 不推送。后端段 ls_rank (契约 ls-rank-v1)。
 * 文案只写结果 (名词 + 数字), 不写说明句。 */
import { useState, type ReactNode } from 'react'
import type { LsRankPayload, LsRankRow, LsSettleSeries } from '@/lib/api'
import { Sheet } from '@/components/opportunities/Sheet'
import { p2, weekday } from '@/components/opportunities/terms'
import { badge } from '@/components/ui/badge'
import { rulerTitle } from '@/components/opportunities/RulerTag'

type Side = 'long' | 'short'
const SIDE_ZH: Record<Side, string> = { long: '看多', short: '看空' }
const TIMING_ZH: Record<string, string> = { bmo: '盘前', amc: '盘后' }
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const sg = (v: number) => (v > 0 ? '+' : v < 0 ? '−' : '')
const sgnCls = (v?: number | null) => (!fin(v) || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')
const num2 = (v?: number | null) => (fin(v) ? `${sg(v)}${Math.abs(v).toFixed(2)}` : '—')
const atrS = (v?: number | null) => (fin(v) ? `${num2(v)} ATR` : '—')
const usdS = (v?: number | null) => (fin(v) ? `${sg(Math.round(v))}$${Math.abs(Math.round(v)).toLocaleString('en-US')}` : '—')
const r1 = (v: number) => Math.round(v * 10) / 10
const pc1 = (v?: number | null) => (fin(v) ? `${sg(r1(v))}${Math.abs(r1(v)).toFixed(1)}%` : '—')
const sgnCls1 = (v?: number | null) => sgnCls(fin(v) ? r1(v) : v)
const pctW = (v?: number | null) => (fin(v) ? `${Math.round(v * 100)}%` : '—')
/** 1 ATR 折多少美元 (= 单笔风险 ÷ 止损 ATR 数); 风险读不到 → null (只显示 ATR) */
const atrUsd = (d: Pick<LsRankPayload, 'risk_usd' | 'stop_atr'>) => (fin(d.risk_usd) && d.stop_atr > 0 ? d.risk_usd / d.stop_atr : null)
const usdOf = (r: number | null | undefined, u: number | null) => (fin(r) && u !== null ? usdS(r * u) : '—')
const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
const etHm = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')

/** 名次 → 「前 1%」/「后 14%」(pct = 分数百分位, 100 = 最强) */
export function rankTag(r: Pick<LsRankRow, 'pct'>, side: Side | null): string {
  const s = side ?? (r.pct >= 50 ? 'long' : 'short')
  return s === 'long' ? `前 ${Math.max(1, Math.round(100 - r.pct))}%` : `后 ${Math.max(1, Math.round(r.pct))}%`
}

/** 持有期内的财报 → 「财报 今日盘后」/「财报 周五盘前」/「财报 11-20 盘后」; 持有期外不标 */
export function earnTag(e: LsRankRow['earnings'], day: string, until: string): string | null {
  if (!e || e.date < day || e.date > until) return null
  const t = TIMING_ZH[e.timing ?? ''] ?? ''
  if (e.date === day) return `财报 今日${t}`
  const diff = (Date.parse(`${e.date}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86_400_000
  return diff <= 6 ? `财报 ${weekday(e.date)}${t}` : `财报 ${md(e.date)} ${t}`.trim()
}

const BTN = 'whitespace-nowrap rounded-btn border border-border px-2.5 py-1 text-[13px] font-medium text-secondary hover:bg-elevated hover:text-foreground max-md:py-1.5'

function Stat({ k, v, unit, sub, cls, bar, ...rest }: { k: string; v: string; unit?: string; sub?: string; cls?: string; bar?: number }
  & Record<`data-${string}`, string | undefined>) {
  return (
    <div {...rest} className="min-w-0 bg-surface px-3 py-1.5">
      <div className="h-4 truncate text-[12px] leading-4 text-secondary">{k}</div>
      <div className="flex h-6 items-baseline gap-1 whitespace-nowrap leading-6">
        <b className={`text-[14px] font-semibold tabular-nums ${cls ?? 'text-foreground'}`}>{v}</b>
        {unit && <span className="text-[12px] text-muted">{unit}</span>}
      </div>
      {bar !== undefined
        ? <div className="flex h-4 items-center"><div className="h-1 w-full overflow-hidden rounded-sm bg-zinc-300 dark:bg-zinc-700"><i className="block h-full bg-accent" style={{ width: `${Math.round(Math.min(Math.max(bar, 0), 1) * 100)}%` }} /></div></div>
        : <div className="h-4 truncate text-[12px] leading-4 tabular-nums text-muted">{sub ?? (v === '—' ? '' : '—')}</div>}
    </div>
  )
}

function settleCells(d: LsRankPayload): { k: string; v: string; unit?: string; sub?: string; cls?: string; bar?: number; key: string }[] {
  const s = d.settle
  const t = s.days > 0 ? s.total : null
  const u = atrUsd(d)
  const a = (v?: number | null) => ({ v: num2(v), unit: fin(v) ? 'ATR' : undefined, cls: sgnCls(v) })
  return [
    { key: 'progress', k: '前向验证', v: `${s.days}/${s.check_days}`, unit: '日', bar: s.days / s.check_days },
    { key: 'long', k: '看多每笔', ...a(t?.long), sub: t ? `${usdOf(t.long, u)} · 胜率 ${pctW(t.win_long)}` : undefined },
    { key: 'edge', k: '看多超额', ...a(t?.long_minus_all), sub: t ? `自选全体 ${num2(t.all)} · 判据 ≥0` : undefined },
    { key: 'short', k: '看空每笔', ...a(t?.short), sub: t ? `${usdOf(t.short, u)} · 胜率 ${pctW(t.win_short)}` : undefined },
    { key: 'pair', k: '多空合计', ...a(t?.pair), sub: t ? `${usdOf(t.pair, u)} · 日胜率 ${pctW(t.win_pair)}` : undefined },
    { key: 'rand', k: '随机多空', ...a(t?.rand_pair), sub: t ? `多 ${num2(t.rand_long)} · 空 ${num2(t.rand_short)}` : undefined },
  ]
}

// ── 全部 47 ──
// 跟榜口径: 前 5 进 · 掉出前 10 换 → 6-10 名 = 保留区
const BOOK_ZH: Record<string, string> = { keep_long: '保留多', keep_short: '保留空', out_long: '换出多', out_short: '换出空' }
function AllSheet({ d, onOpen, onClose }: { d: LsRankPayload; onOpen: (s: string) => void; onClose: () => void }) {
  const head: [string, string][] = [['#', 'w-7 text-right'], ['代码', 'pl-3 text-left'], ['排名', 'text-right'], ['较昨日', 'text-right'], [d.pre?.open ? '开盘' : '盘前', 'text-right'],
    ['收盘', 'text-right'], ['ATR', 'text-right'], ['财报', 'text-right'], ['名单', 'text-right']]
  return (
    <Sheet attr={{ 'data-ls-all': '' }} onClose={onClose} title={`全部 ${d.all.length} · ${md(d.day)} 名单`}>
      <table className="mb-2 w-full text-[13px] tabular-nums">
        <thead className="sticky -top-4 z-[1]"><tr className="h-8 text-[12px] text-muted">
          {head.map(([h, c]) => <th key={h} className={`bg-surface px-1.5 font-medium shadow-[inset_0_-1px_0_0_hsl(var(--border))] ${c}`}>{h}</th>)}
        </tr></thead>
        <tbody>
          {d.all.map((r) => {
            const dl = fin(r.prev_rank) ? r.prev_rank - r.rank : null
            const earn = earnTag(r.earnings, d.day, d.hold_until)
            const dim = r.young
            const tone = r.side === 'long' ? 'bg-bull/[0.05] shadow-[inset_3px_0_0_0_hsl(var(--bull))]' : r.side === 'short' ? 'bg-bear/[0.05] shadow-[inset_3px_0_0_0_hsl(var(--bear))]' : ''
            const book = r.side ? SIDE_ZH[r.side] : r.young ? '上市不足' : BOOK_ZH[r.book ?? ''] ?? '—'
            const bookCls = r.side === 'long' ? 'text-bull' : r.side === 'short' ? 'text-bear' : BOOK_ZH[r.book ?? ''] && !r.young ? 'text-secondary' : 'text-muted'
            return (
              <tr key={r.symbol} data-ls-all-row={r.symbol} onClick={() => onOpen(r.symbol)}
                className={`h-8 cursor-pointer border-t border-border/70 first:border-t-0 hover:bg-elevated ${tone}`}>
                <td className="w-7 px-1.5 text-right text-[12px] text-muted">{r.rank}</td>
                <td className={`pl-3 pr-1.5 text-left font-bold ${dim ? 'text-muted' : 'text-foreground'}`}>{r.symbol}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-secondary'}`}>{rankTag(r, r.side)}</td>
                <td className={`px-1.5 text-right ${dl ? 'text-secondary' : 'text-muted'}`}>{dl === null ? '—' : dl > 0 ? `↑${dl}` : dl < 0 ? `↓${-dl}` : '—'}</td>
                <td className={`px-1.5 text-right font-semibold ${dim ? 'text-muted' : sgnCls1(r.pre_pct)}`}>{pc1(r.pre_pct)}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-foreground'}`}>{p2(r.close)}</td>
                <td className={`px-1.5 text-right ${dim ? 'text-muted' : 'text-foreground'}`}>{p2(r.atr)}</td>
                <td className={`whitespace-nowrap px-1.5 text-right text-[12px] ${earn ? 'font-semibold text-warning' : 'text-muted'}`}>{earn ? earn.replace('财报 ', '') : '—'}</td>
                <td className={`whitespace-nowrap px-1.5 text-right text-[12px] font-semibold ${bookCls}`}>{book}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Sheet>
  )
}

// ── 结算明细 ──
const WEEK_ROWS: [string, (w: LsSettleSeries) => number | null][] = [
  ['看多', (w) => w.long], ['看空', (w) => w.short], ['多空合计', (w) => w.pair], ['随机多空', (w) => w.rand_pair],
  ['自选全体', (w) => w.all], ['看多−全体', (w) => w.long_minus_all],
]

function SettleSheet({ d, onOpen, onClose }: { d: LsRankPayload; onOpen: (s: string) => void; onClose: () => void }) {
  const s = d.settle
  const u = atrUsd(d)
  const cols = [...s.weeks.map((w) => ({ key: w.week, label: w.week, w })), ...(s.total ? [{ key: 'total', label: '合计', w: s.total }] : [])]
  const last = s.last
  const H3 = ({ children }: { children: ReactNode }) => <h3 className="mb-2 mt-5 text-[13px] font-semibold text-foreground first:mt-0">{children}</h3>
  return (
    <Sheet attr={{ 'data-ls-settle': '' }} onClose={onClose} title={`结算 · 前向 ${s.days} 份${s.first ? ` · ${md(s.first)} 起` : ''}`}>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-border bg-border sm:grid-cols-3">
        {settleCells(d).map(({ key, ...c }) => <Stat key={key} data-ls-sheet-stat={key} {...c} />)}
      </div>
      {cols.length > 0 && (
        <>
          <H3>周度 · ATR</H3>
          <div className="overflow-x-auto rounded-card border border-border">
            <table data-ls-weeks className="w-full min-w-[480px] text-[13px] tabular-nums">
              <thead><tr className="h-8 bg-elevated text-[12px] text-muted">
                <th className="px-3 text-left font-medium">周</th>
                {cols.map((c) => <th key={c.key} className={`px-3 text-right font-medium ${c.key === 'total' ? 'border-l border-border/70 text-secondary' : ''}`}>{c.label}</th>)}
              </tr></thead>
              <tbody>
                <tr className="h-8 border-t border-border/70">
                  <td className="px-3 text-left text-secondary">名单数</td>
                  {cols.map((c) => <td key={c.key} className={`px-3 text-right text-foreground ${c.key === 'total' ? 'border-l border-border/70' : ''}`}>{c.w.n_lists}</td>)}
                </tr>
                {WEEK_ROWS.map(([lbl, f]) => (
                  <tr key={lbl} className="h-8 border-t border-border/70">
                    <td className="whitespace-nowrap px-3 text-left text-secondary">{lbl}</td>
                    {cols.map((c) => <td key={c.key} className={`px-3 text-right font-semibold ${sgnCls(f(c.w))} ${c.key === 'total' ? 'border-l border-border/70' : ''}`}>{num2(f(c.w))}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {last && (
        <>
          <H3>{`最近结算 · ${md(last.made)} 名单 · ${md(last.settled)} 收盘`}</H3>
          <div className="grid gap-3 sm:grid-cols-2">
            {(['long', 'short'] as const).map((side) => {
              const xs = last[side]
              const miss = last.missing?.[side] ?? []
              const sum = side === 'long' ? last.sum_long : last.sum_short
              return (
                <div key={side} data-ls-last={side} className="overflow-hidden rounded-card border border-border">
                  <div className="flex items-baseline gap-2 bg-elevated px-3 py-1.5">
                    <b className={`text-[13px] ${side === 'long' ? 'text-bull' : 'text-bear'}`}>{SIDE_ZH[side]}</b>
                    <span className="text-[12px] text-secondary">{xs.length}</span>
                    {miss.length > 0 && <span data-ls-missing={side} title="持有期内缺 K 线（停牌 / 退市）· 不计入成绩" className="text-[12px] font-semibold text-warning">{`缺 ${miss.join(' ')}`}</span>}
                    <span className={`ml-auto text-[13px] font-semibold tabular-nums ${sgnCls(sum)}`}>{u === null ? `${num2(sum)} ATR` : `${num2(sum)} ATR · ${usdS(sum * u)}`}</span>
                  </div>
                  {xs.map((x) => (
                    <div key={x.symbol} role="button" tabIndex={0} onClick={() => onOpen(x.symbol)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(x.symbol) } }}
                      className="grid h-8 cursor-pointer grid-cols-[64px_minmax(0,1fr)_56px_60px] items-center gap-2 border-t border-border/70 px-3 text-[13px] tabular-nums hover:bg-elevated">
                      <b className="text-foreground">{x.symbol}</b>
                      <span className={`text-[12px] ${x.exit === 'stop' ? 'font-semibold text-warning' : 'text-muted'}`}>{x.exit === 'stop' ? '止损' : '到期'}</span>
                      <span className={`text-right font-semibold ${sgnCls(x.r)}`}>{num2(x.r)}</span>
                      <span className={`text-right ${sgnCls(x.r)}`}>{usdOf(x.r, u)}</span>
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </>
      )}
      <H3>判据</H3>
      <div data-ls-rules className="mb-2 overflow-hidden rounded-card border border-border text-[13px] tabular-nums">
        {[
          ['失败判据', `${s.check_days} 日 · 看多−自选全体 < 0 ATR`],
          ['当前', `${s.days}/${s.check_days} 日 · ${atrS(s.days > 0 ? s.total?.long_minus_all : null)}`, sgnCls(s.days > 0 ? s.total?.long_minus_all : null)],
          ['对照', '随机 5 多 5 空 · 同日结算'],
          ...(s.late ? [['不计入', `开盘后补出的名单 ${s.late} 份`]] : []),
          ['回放', `${d.backtest.period} · 看多 ${atrS(d.backtest.long.mean_atr)} · 看空 ${atrS(d.backtest.short.mean_atr)}`],
          ['单位', u === null ? `止损 ${d.stop_atr} ATR · 持有 ${d.hold_days} 日` : `1 ATR = $${Math.round(u)} · 每笔 $${Math.round(d.risk_usd ?? 0)} · 止损 ${d.stop_atr} ATR · 持有 ${d.hold_days} 日`],
          ['模型', `${d.model} · 训练截至 ${md(d.trained_to)}${d.made_at ? ` · 名单 ${md(d.made_at)} ${etHm(d.made_at)} ET 生成` : ''}`],
          ...(d.next_retrain ? [['下次重训', `${d.next_retrain.quarter} · 只用 ${md(d.next_retrain.train_end)} 之前的入场日${d.next_retrain.rows_now ? ` · 本版样本 ${d.next_retrain.rows_now.toLocaleString()} 行` : ''}`]] : []),
          ...(d.settle.prev ?? []).map((p) => [`上一版 ${p.quarter}`, `前向 ${p.days} 份 · 看多−全体 ${atrS(p.total?.long_minus_all ?? null)}`]),
          ...(d.ruler ? [['回放口径', rulerTitle(d.ruler)]] : []),
        ].map(([k, v, cls]) => (
          <div key={k} className="grid min-h-8 grid-cols-[84px_1fr] items-center gap-2 border-t border-border/70 px-3 py-1 first:border-t-0">
            <span className="text-secondary">{k}</span><span className={cls ?? 'text-foreground'}>{v}</span>
          </div>
        ))}
      </div>
    </Sheet>
  )
}

/** 名次 → 「前 1%」/「后 3%」 */
const rankPct = (r: Pick<LsRankRow, 'pct'>, side: Side) => rankTag(r, side)

export function ModelCard({ d, onOpen }: { d: LsRankPayload; onOpen: (s: string) => void }) {
  const [sheet, setSheet] = useState<'all' | 'settle' | null>(null)
  const open = (s: string) => { setSheet(null); onOpen(s) }
  const col = (side: Side, rows: LsRankRow[]) => (
    <div data-ls-side={side} className="min-w-0">
      <div className="flex items-baseline gap-1.5 pb-1 text-[12px] font-semibold text-secondary">
        <span>{side === 'long' ? '↑ 看涨' : '↓ 看跌'}</span><span className="tabular-nums">{rows.length}</span>
      </div>
      {rows.map((r) => {
        const earn = earnTag(r.earnings, d.day, d.hold_until)
        return (
          <button key={r.symbol} type="button" data-ls-row={r.symbol} data-side={side} onClick={() => onOpen(r.symbol)} title={earn ?? undefined}
            className="flex h-7 w-full items-center gap-1.5 rounded-btn px-1 text-left text-[13px] tabular-nums hover:bg-elevated max-md:h-9">
            <b className="text-foreground">{r.symbol}</b>
            {r.is_new && <i data-ls-new title="较上一交易日新进" className="h-[6px] w-[6px] shrink-0 rounded-full bg-accent dark:bg-blue-400" />}
            {earn && <i data-ls-earn className="shrink-0 rounded-[3px] bg-warning/15 px-[3px] text-[11px] font-semibold not-italic leading-4 text-warning">财</i>}
            <span className="ml-auto whitespace-nowrap text-[12px] text-secondary">{rankPct(r, side)}</span>
          </button>
        )
      })}
    </div>
  )
  return (
    <section data-ls-rank data-source="model" className="mb-3 min-w-0 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-3 pb-1.5 pt-2.5">
        <h2 className="shrink-0 text-[14px] font-semibold text-foreground" title="模型独立打分 · 不据此生成策略机会 · 不下单">模型打分</h2>
        <span className={badge('limit')} title="模型独立打分 · 不据此生成策略机会 · 不下单">仅参考</span>
        {d.demo && <span data-ls-demo className="shrink-0 rounded-btn bg-warning px-1.5 text-[11px] font-semibold leading-5 text-white dark:text-zinc-950">示例数据</span>}
        {d.late && <span data-ls-late title="名单日开盘后才生成 · 照常结算 · 不计入前向成绩" className={badge('warn')}>盘中补出</span>}
        <span data-ls-meta className="ml-auto whitespace-nowrap text-[12px] tabular-nums text-muted"
          title={[`模型版本 ${d.model}`, d.next_retrain ? `下次重训 ${d.next_retrain.quarter}` : ''].filter(Boolean).join('\n')}>
          {`${md(d.day)} ${weekday(d.day)} · 前向 ${d.settle.days}/${d.settle.check_days}`}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 px-2.5 pb-1">
        {col('long', d.long)}
        {col('short', d.short)}
      </div>
      <div className="flex gap-1.5 border-t border-border/70 px-3 py-2">
        <button type="button" data-ls-open="all" onClick={() => setSheet('all')} className={BTN}>{`全部 ${d.all.length}`}</button>
        <button type="button" data-ls-open="settle" onClick={() => setSheet('settle')} className={BTN}>结算明细</button>
        {[...d.long, ...d.short].some((r) => r.is_new) && (
          <span className="ml-auto inline-flex items-center gap-1 self-center whitespace-nowrap text-[12px] text-muted"><i className="h-[6px] w-[6px] rounded-full bg-accent dark:bg-blue-400" />较上一交易日新进</span>
        )}
      </div>
      {sheet === 'all' && <AllSheet d={d} onOpen={open} onClose={() => setSheet(null)} />}
      {sheet === 'settle' && <SettleSheet d={d} onOpen={open} onClose={() => setSheet(null)} />}
    </section>
  )
}

/** 名单尚未生成 (上线后第一个盘后管道之前) */
export function LsRankEmpty() {
  return (
    <section data-ls-rank-empty data-source="model" className="mb-3 flex flex-wrap items-center gap-2 rounded-card border border-border bg-surface px-3 py-2.5">
      <h2 className="text-[14px] font-semibold text-foreground">模型打分</h2>
      <span className={badge('limit')}>仅参考</span>
      <span className="ml-auto text-[12px] tabular-nums text-muted">16:35 ET 生成名单</span>
    </section>
  )
}
