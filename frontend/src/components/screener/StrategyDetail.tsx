import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { History, Settings2, SlidersHorizontal } from 'lucide-react'
import type { StrategyDetail as StrategyMeta, StrategyPanelEntry, StrategyPanelPayload } from '@/lib/api'
import { badge } from '@/components/ui/badge'
import { StrategyRCurve } from '@/components/screener/StrategyRCurve'
import {
  boardLabel,
  curvePoints,
  fmtP,
  fmtR,
  fmtRate,
  hasFullReplay,
  md,
  paperState,
  selectedLabel,
  selectedSides,
  sideOf,
  toneClass,
  whyLabel,
  type PanelSideKey,
} from '@/lib/strategyPanel'

const SOURCE_LABEL: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI', composite: '叠加' }
const SECTION_LABEL: Record<string, string> = { selected: '入选策略', replay: '回放', signals: '自选命中', board: '机会板', paper: '模拟成绩' }
const TRADES_FOLD = 6

interface Props {
  strategy: StrategyMeta
  entry: StrategyPanelEntry | null
  panel: StrategyPanelPayload | null
  panelLoading: boolean
  side: PanelSideKey
  onSideChange: (s: PanelSideKey) => void
  inPool: boolean
  onTogglePool: () => void
  monitored: boolean
  onToggleMonitor: () => void
  onSettings: () => void
  onPreview: (symbol: string) => void
}

function timeframeLabel(frames?: string[]): string {
  return frames?.includes('1h') ? '1 小时' : frames?.includes('1m') ? '分钟' : '日线'
}

function Switch({ on, label, title, onClick, name }: { on: boolean; label: string; title: string; onClick: () => void; name: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      data-switch={name}
      title={title}
      onClick={onClick}
      className={`inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-btn border px-2.5 text-xs font-medium transition-colors cursor-pointer
        ${on ? 'border-accent/50 text-foreground' : 'border-border text-secondary hover:text-foreground'}`}
    >
      <span className={`relative h-3 w-[22px] shrink-0 rounded-full transition-colors ${on ? 'bg-accent' : 'bg-border'}`}>
        <span className={`absolute top-0.5 h-2 w-2 rounded-full transition-all ${on ? 'left-3 bg-white' : 'left-0.5 bg-secondary'}`} />
      </span>
      {label}
    </button>
  )
}

function Kpi({ label, title, value, tone, sub, extra, name }: {
  label: string; title?: string; value: string; tone?: string; sub: React.ReactNode; extra?: React.ReactNode; name: string
}) {
  return (
    <div data-kpi={name} title={title} className="min-w-0 rounded-card border border-border bg-surface px-3.5 py-2.5">
      <div className="flex h-5 items-center gap-1.5 text-[11px] text-muted">
        <span className="whitespace-nowrap">{label}</span>
        {extra}
      </div>
      <div className={`h-7 overflow-hidden whitespace-nowrap text-[20px] font-bold leading-7 tabular-nums ${tone ?? 'text-foreground'}`}>{value}</div>
      <div className="min-h-[18px] truncate text-xs tabular-nums text-secondary">{sub}</div>
    </div>
  )
}

function Bars({ hist }: { hist: number[] }) {
  const mx = Math.max(1, ...hist)
  return (
    <span data-hist-bars className="inline-flex h-[18px] items-end gap-px align-middle" title={`近 ${hist.length} 个信号日 · 自选命中次数`}>
      {hist.map((v, i) => (
        <i
          key={i}
          className={`block w-[3px] rounded-[1px] ${v === 0 ? 'bg-border' : i === hist.length - 1 ? 'bg-accent' : 'bg-secondary'}`}
          style={{ height: v === 0 ? 2 : Math.max(3, Math.round((v / mx) * 18)) }}
        />
      ))}
    </span>
  )
}

function SymbolChip({ symbol, note, onClick }: { symbol: string; note?: string; onClick: () => void }) {
  return (
    <button
      type="button"
      data-chip={symbol}
      onClick={onClick}
      className="inline-flex h-[22px] items-center gap-1 whitespace-nowrap rounded-[4px] bg-elevated px-1.5 text-xs font-semibold text-foreground transition-colors hover:text-accent cursor-pointer"
    >
      {symbol}
      {note && <span className="text-[11px] font-medium text-secondary">{note}</span>}
    </button>
  )
}

const CARD = 'min-w-0 rounded-card border border-border bg-surface'
const CARD_HEAD = 'flex min-h-[37px] flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/60 px-3 py-2 text-xs'

/**
 * 单个策略的全貌 (只读): 标题与开关、四格指标、模拟累计 R 对回放期望、自选命中、模拟成交、参数。
 * 入选策略在这里只显示不修改 —— 要改去机会页的规则设置。全市场命中表由页面放在本组件下方。
 */
export function StrategyDetail({
  strategy, entry, panel, panelLoading, side, onSideChange, inPool, onTogglePool, monitored, onToggleMonitor, onSettings, onPreview,
}: Props) {
  const [allTrades, setAllTrades] = useState(false)
  const view = sideOf(entry, side)
  const replay = view?.replay ?? null
  const full = hasFullReplay(replay)
  const paper = view?.paper ?? null
  const state = paperState(paper)
  const sel = selectedSides(entry)
  const points = useMemo(() => curvePoints(paper?.trades ?? [], paper?.replay_mean_r), [paper])
  const trades = useMemo(() => [...(paper?.trades ?? [])].reverse(), [paper])
  const shownTrades = allTrades ? trades : trades.slice(0, TRADES_FOLD)
  const hits = view?.hits ?? []
  const board = view?.board ?? []
  const boardBySymbol = useMemo(() => new Map(board.map(b => [b.symbol, b])), [board])
  const boardOnly = board.filter(b => !hits.includes(b.symbol))
  const hist = view?.hist ?? []
  const histSum = hist.reduce((a, b) => a + b, 0)
  const signalDay = panel?.signals.day ?? null
  const failed = Object.keys(panel?.errors ?? {}).filter(k => SECTION_LABEL[k])
  // 看做空那一面时, 标题用做空名 (做多名是「…翻多」, 配着「入选 · 空」会读反), 原名放在后面
  const shortName = side === 'short' ? entry?.name_short ?? null : null
  const title = shortName ?? strategy.name

  // 参数卡 (只读; 改参数点「参数设置」)。成交多时叠在自选命中下面补齐左列; 成交少时通栏放在最下面, 不留缺角
  const stackParams = trades.length > 2
  const paramsCard = (
    <div className={CARD}>
      <div className={CARD_HEAD}>
        <h3 className="whitespace-nowrap font-semibold text-foreground">参数</h3>
        <span className="whitespace-nowrap tabular-nums text-muted">{strategy.params.length} 项</span>
      </div>
      <div data-params className={`grid grid-cols-1 gap-x-8 px-3 py-2 sm:grid-cols-2 ${stackParams ? '' : 'xl:grid-cols-3'}`}>
        {strategy.params.length === 0 && <span className="py-1 text-xs text-muted">无可调参数</span>}
        {strategy.params.map(p => {
          const v = strategy.params_defaults?.[p.id] ?? p.default
          return (
            <div key={p.id} className="flex min-w-0 items-center justify-between gap-3 py-1 text-xs">
              <span className="min-w-0 truncate text-secondary" title={p.label}>{p.label}</span>
              <b className="whitespace-nowrap font-semibold tabular-nums text-foreground">{v === true ? '开' : v === false ? '关' : String(v)}</b>
            </div>
          )
        })}
      </div>
    </div>
  )

  const stateBadge = state === 'diverged'
    ? <span data-paper-state="diverged" className={badge('warn')} title="模拟每笔 R 的 95% 区间不含回放值">背离</span>
    : state === 'thin'
      ? <span data-paper-state="thin" className={badge('limit')} title="有成交的生成机会日不足 8 个 · 暂不给区间">样本少</span>
      : null

  return (
    <div data-strategy-detail={strategy.id} className="min-w-0 space-y-3">
      {/* 标题区: 第一行名称与标签; 第二行左边方向与开关, 右边入口 */}
      <div className={CARD}>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3.5 pb-2.5 pt-3">
          <h2 data-detail-name className="min-w-0 truncate text-[18px] font-semibold leading-7 tracking-tight text-foreground" title={strategy.description}>
            {title}
          </h2>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={badge('limit')}>{SOURCE_LABEL[strategy.source] ?? '内置'}</span>
            <span className={badge('limit')}>{timeframeLabel(strategy.timeframes)}</span>
            {sel.length > 0 && <span data-detail-selected className={badge('status')} title="入选策略 · 在机会页生成机会">{selectedLabel(sel)}</span>}
            {shortName && <span data-detail-base className="whitespace-nowrap text-xs text-secondary">{strategy.name}</span>}
          </div>
        </div>
        <div data-detail-actions className="flex flex-wrap items-center gap-2 border-t border-border/60 px-3.5 py-2">
          {entry?.short_capable && (
            <div role="tablist" aria-label="方向" className="flex h-7 overflow-hidden rounded-btn border border-border">
              {(['long', 'short'] as const).map(s => (
                <button
                  key={s}
                  type="button"
                  role="tab"
                  aria-selected={side === s}
                  data-side={s}
                  onClick={() => onSideChange(s)}
                  className={`h-full whitespace-nowrap border-r border-border px-2.5 text-xs font-medium transition-colors last:border-r-0 cursor-pointer
                    ${side === s ? 'bg-accent/10 text-accent dark:text-blue-400' : 'text-secondary hover:bg-elevated hover:text-foreground'}`}
                >
                  {s === 'long' ? '做多' : '做空'}
                </button>
              ))}
            </div>
          )}
          <Switch name="pool" on={inPool} label="全市场扫描" title={inPool ? '移出策略池' : '加入策略池'} onClick={onTogglePool} />
          <Switch name="monitor" on={monitored} label="监控" title={monitored ? '取消策略监控' : '开启策略监控'} onClick={onToggleMonitor} />
          <div className="flex flex-wrap items-center gap-2 md:ml-auto">
            <Link
              to="/?rules=1"
              data-detail-rules
              title="入选策略在机会页的规则设置里调整"
              className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-btn border border-border px-2.5 text-xs font-medium text-secondary transition-colors hover:border-accent/50 hover:text-accent"
            >
              <SlidersHorizontal className="h-3.5 w-3.5 shrink-0" />
              入选策略
            </Link>
            <button
              type="button"
              data-detail-settings
              onClick={onSettings}
              className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-btn border border-border px-2.5 text-xs font-medium text-secondary transition-colors hover:border-accent/50 hover:text-accent cursor-pointer"
            >
              <Settings2 className="h-3.5 w-3.5 shrink-0" />
              参数设置
            </button>
            <Link
              to="/backtest"
              className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-btn border border-border px-2.5 text-xs font-medium text-secondary transition-colors hover:border-accent/50 hover:text-accent"
            >
              <History className="h-3.5 w-3.5 shrink-0" />
              回测
            </Link>
          </div>
        </div>
      </div>

      {failed.length > 0 && (
        <div data-panel-errors className="rounded-btn border border-warning/50 px-3 py-1.5 text-xs text-warning">
          读取失败 · {failed.map(k => SECTION_LABEL[k]).join(' / ')}
        </div>
      )}

      {/* 四格指标 */}
      <div className="grid grid-cols-2 gap-2.5 xl:grid-cols-4">
        <Kpi
          name="replay-mean"
          label="回放每笔"
          title={panel?.replay_meta.window ? `机会板同一张卡的历史回放 · ${panel.replay_meta.window[0]} 起` : undefined}
          value={full ? fmtR(replay.mean_r) : '—'}
          tone={full ? toneClass(replay.mean_r) : undefined}
          sub={replay ? (full ? `${replay.n.toLocaleString('en-US')} 笔 · 成交率 ${fmtRate(replay.fill)}` : `样本少 · ${replay.n} 笔`) : (panelLoading ? '加载中' : '暂无回放')}
        />
        <Kpi
          name="replay-excess"
          label="回放超额"
          title="每笔比随机基准多出的 R（随机基准 = 4h 趋势同向的随机入场）"
          value={full ? fmtR(replay.excess) : '—'}
          tone={full ? toneClass(replay.excess) : undefined}
          sub={full ? `p ${fmtP(replay.p)} · 对比随机基准` : '—'}
        />
        <Kpi
          name="paper-mean"
          label="模拟每笔"
          title={panel?.paper_since ? `模拟账户已结算的机会 · ${panel.paper_since} 起 · 多策略共振的机会各计一次` : undefined}
          value={paper && paper.fills > 0 ? fmtR(paper.mean_r) : '—'}
          tone={paper && paper.fills > 0 ? toneClass(paper.mean_r) : undefined}
          extra={stateBadge}
          sub={paper ? `${paper.fills} 笔 · 胜 ${paper.win} 负 ${paper.loss}` : (panelLoading ? '加载中' : '暂无成交')}
        />
        <Kpi
          name="paper-fill"
          label="模拟成交率"
          title="生成的机会里价格回到入场区并成交的比例"
          value={paper ? fmtRate(paper.fill_rate) : '—'}
          sub={paper ? `机会 ${paper.cards} · 回放 ${fmtRate(paper.replay_fill)}` : '—'}
        />
      </div>

      {/* 模拟累计 R 对回放期望 */}
      <div className={`${CARD} px-3.5 pb-2 pt-3`}>
        <div className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
          <h3 className="whitespace-nowrap text-[13px] font-semibold text-foreground">模拟累计 R</h3>
          {panel?.paper_since && <span className="whitespace-nowrap text-xs tabular-nums text-muted">{md(panel.paper_since)} 起 · 按平仓日</span>}
          {paper?.ci && (
            <span data-paper-ci className="whitespace-nowrap text-xs tabular-nums text-secondary">
              每笔均值 95% 区间 {fmtR(paper.ci[0])} ~ {fmtR(paper.ci[1])}
            </span>
          )}
          <span className="ml-auto flex flex-wrap items-center gap-x-3.5 gap-y-1 text-xs text-secondary">
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap"><i className="block h-[3px] w-4 rounded-sm bg-accent" />模拟</span>
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <i className="block h-0.5 w-4" style={{ background: 'repeating-linear-gradient(90deg, hsl(var(--fg-muted)) 0 4px, transparent 4px 8px)' }} />
              回放期望
            </span>
          </span>
        </div>
        <StrategyRCurve points={points} since={panel?.paper_since} />
      </div>

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)] xl:items-start">
        <div className="min-w-0 space-y-3">
        {/* 自选命中 + 机会板现状 */}
        <div className={CARD}>
          <div className={CARD_HEAD}>
            <h3 className="whitespace-nowrap font-semibold text-foreground">自选命中</h3>
            <span data-signal-day className="whitespace-nowrap tabular-nums text-muted">{signalDay ? md(signalDay) : '暂无信号'}</span>
            {hist.length > 0 && (
              <span className="ml-auto inline-flex items-center gap-2 whitespace-nowrap tabular-nums text-muted">
                近 {hist.length} 日 {histSum}
                <Bars hist={hist} />
              </span>
            )}
          </div>
          <div data-hits className="flex min-h-[38px] flex-wrap items-center gap-1.5 px-3 py-2">
            <span className="mr-0.5 whitespace-nowrap text-[11px] tabular-nums text-muted">自选 {hits.length}</span>
            {hits.length === 0 && <span className="text-xs text-muted">暂无命中</span>}
            {hits.map(s => {
              const b = boardBySymbol.get(s)
              return <SymbolChip key={s} symbol={s} note={b ? boardLabel(b) : undefined} onClick={() => onPreview(s)} />
            })}
          </div>
          {boardOnly.length > 0 && (
            <div data-board className="flex min-h-[38px] flex-wrap items-center gap-1.5 border-t border-border/60 px-3 py-2">
              <span className="mr-0.5 whitespace-nowrap text-[11px] tabular-nums text-muted" title="此前信号日生成、仍在机会板上的机会">机会板 {boardOnly.length}</span>
              {boardOnly.map(b => (
                <SymbolChip
                  key={b.card_id ?? `${b.symbol}-${b.signal_day}`}
                  symbol={b.symbol}
                  note={b.state === 'holding' && b.r_now != null ? `持仓 ${fmtR(b.r_now)}` : boardLabel(b)}
                  onClick={() => onPreview(b.symbol)}
                />
              ))}
            </div>
          )}
        </div>

        {stackParams && paramsCard}
        </div>

        {/* 模拟成交 */}
        <div className={CARD}>
          <div className={CARD_HEAD}>
            <h3 className="whitespace-nowrap font-semibold text-foreground">模拟成交</h3>
            <span className="whitespace-nowrap tabular-nums text-muted">{paper ? `${paper.fills} 笔` : '0 笔'}</span>
          </div>
          {trades.length === 0 ? (
            <div data-trades-empty className="px-3 py-5 text-xs text-muted">暂无成交</div>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table data-trades className="w-full border-collapse text-xs">
                  <thead>
                    <tr className="text-[11px] text-muted">
                      {['平仓日', '代码', '原因'].map(h => <th key={h} className="whitespace-nowrap border-b border-border px-3 py-1.5 text-left font-medium">{h}</th>)}
                      <th className="whitespace-nowrap border-b border-border px-3 py-1.5 text-right font-medium">R</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownTrades.map((t, i) => (
                      <tr key={`${t.symbol}-${t.exit_date}-${i}`} className="border-b border-border/60 last:border-b-0">
                        <td className="whitespace-nowrap px-3 py-1.5 tabular-nums text-secondary">{md(t.exit_date)}</td>
                        <td className="whitespace-nowrap px-3 py-1.5">
                          <button type="button" onClick={() => onPreview(t.symbol)} className="font-semibold text-foreground hover:text-accent cursor-pointer">{t.symbol}</button>
                        </td>
                        <td className="whitespace-nowrap px-3 py-1.5 text-secondary">{whyLabel(t.why)}</td>
                        <td className={`whitespace-nowrap px-3 py-1.5 text-right tabular-nums ${toneClass(t.r)}`}>{fmtR(t.r)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center gap-2 border-t border-border/60 px-3 py-2 text-xs">
                <span data-trades-count className="tabular-nums text-muted">1–{shownTrades.length} / {trades.length}</span>
                {trades.length > TRADES_FOLD && (
                  <button
                    type="button"
                    data-trades-toggle
                    onClick={() => setAllTrades(v => !v)}
                    className="ml-auto inline-flex h-7 items-center whitespace-nowrap rounded-btn border border-border px-2.5 font-medium text-secondary transition-colors hover:text-foreground cursor-pointer"
                  >
                    {allTrades ? '收起' : '全部'}
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {!stackParams && paramsCard}
    </div>
  )
}
