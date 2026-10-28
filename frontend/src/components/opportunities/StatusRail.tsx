/** 机会页状态栏 (0.39.0 起; 0.41.0 从雷达台拆出单独保留): 双时钟 + 当前时段 + 下一事件 + 名额 + 四条引擎泳道。
 * 只展示 radar_view (后端投影, 契约 opp-radar-v1); 交易环 / 准入 / 价位一律不在前端判断。 */
import type { ReactNode } from 'react'
import type { OppRadarView, RadarSlot } from '@/lib/api'
import { bjHm, capSub, dayTag, EVENT_ZH, laneText, PHASE_ZH, slotLabel } from '@/components/opportunities/terms'

const FW = { bold: 700, semibold: 600 } as const
const hmOf = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')
const toMin = (hm: string) => Number(hm.slice(0, 2)) * 60 + Number(hm.slice(3, 5))

// ────────────────────────── 1. 状态栏 ──────────────────────────

function Clock({ view }: { view: OppRadarView }) {
  const ck = view.clock
  const today = ck.et.slice(0, 10)
  const phase = PHASE_ZH[ck.phase] ?? ck.phase
  const nxt = ck.next
  const tag = dayTag(nxt.day, today)
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
      <div data-rail-clock className="flex items-baseline gap-1.5 tabular-nums">
        <b className="font-mono text-[20px] font-semibold leading-[26px] tracking-tight text-foreground">{hmOf(ck.et)}</b>
        <span className="text-[12px] text-secondary">{`ET ${tag ? '' : ''}${weekdayOf(today)}`}</span>
        <span className="text-[12px] text-muted">{`北京 ${bjHm(ck.et)}`}</span>
      </div>
      <div data-rail-phase className="flex flex-wrap items-center gap-1.5">
        <span className={`rounded-btn px-2 py-0.5 text-[13px] font-semibold ${ck.trading_day && ck.phase !== 'after' ? 'bg-accent text-white dark:bg-blue-700' : 'bg-zinc-700 text-white dark:bg-zinc-600'}`}>
          {ck.entry_window ? `${phase} · 入场窗` : phase}
        </span>
        <span data-rail-next className="whitespace-nowrap text-[12.5px] font-semibold text-secondary">
          {'→ 下一 '}
          <b className="font-mono text-accent">{`${tag ? `${tag} ` : ''}${nxt.at}`}</b>
          {` ${EVENT_ZH[nxt.code] ?? nxt.code}`}
          <span className="ml-1 font-normal text-muted">{`北京 ${bjHm(nxt.ts)}`}</span>
        </span>
      </div>
    </div>
  )
}

const weekdayOf = (iso: string) => ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(`${iso}T12:00:00Z`).getUTCDay()]

function CapCell({ k, used, cap, unit, sub, state, title, ratio, ...rest }: {
  k: string; used: number | string; cap: number | string | null; unit?: string; sub?: string; state?: string; title?: string
  ratio?: number | null
} & Record<`data-${string}`, string | undefined>) {
  // 进度条比例: 显式 ratio 优先 (组合风险的 used 是格式化后的字符串); 否则按 used / cap
  const raw = ratio ?? (typeof used === 'number' && typeof cap === 'number' && cap > 0 ? used / cap : 0)
  const n = Number.isFinite(raw) ? Math.min(Math.max(raw, 0), 1) : 0
  const warn = state === 'full' || state === 'caution' || state === 'tight'
  return (
    <div {...rest} title={title} data-cap-state={state ?? 'ok'}
      className={`min-w-0 rounded-btn border px-2.5 py-1 md:min-w-[92px] max-md:px-1.5 ${state === 'full' ? 'border-warning/60 bg-warning/10' : warn ? 'border-warning/50 bg-surface' : 'border-border bg-surface'}`}>
      <div className="truncate whitespace-nowrap text-[11px] leading-[14px] text-secondary">{k}</div>
      <div className={`whitespace-nowrap font-mono text-[14px] font-bold leading-[18px] tabular-nums max-md:text-[13px] ${state === 'full' ? 'text-warning' : 'text-foreground'}`}>
        {`${used}${unit ?? ''}`}<span className="font-medium text-muted">{cap === null ? '' : `/${cap}${unit ?? ''}`}</span>
      </div>
      <div className="mt-[3px] h-1 overflow-hidden rounded-sm bg-elevated">
        <i className={`block h-full ${warn ? 'bg-warning' : 'bg-zinc-400 dark:bg-zinc-500'}`} style={{ width: `${Math.round(n * 100)}%` }} />
      </div>
      {sub && <div className="mt-[3px] whitespace-nowrap text-[11px] font-semibold leading-[14px] text-warning">{sub}</div>}
    </div>
  )
}

function Caps({ view }: { view: OppRadarView }) {
  const c = view.caps
  const today = view.clock.et.slice(0, 10)
  // 只显示最紧的一个板块名额 (满 / 预警 / 偏紧), 都正常时显示占用最多的那个
  const top: RadarSlot | undefined = c.slots[0]
  const pr = c.portfolio_risk
  const openTag = c.opens.day !== today ? `${dayTag(c.opens.day, today)}开仓` : '单日开仓'
  // 镜像受限的原因落在哪一格, 那一格标琥珀 (组合风险 / 单日开仓 / 持仓数)
  const hit = (code: string) => view.blips.filter((b) => b.kind === 'blocked' && (b.codes ?? [])[0] === code).length
  return (
    <div data-rail-caps className="flex flex-wrap gap-1.5 md:ml-auto max-md:grid max-md:w-full max-md:grid-cols-2 max-md:gap-1">
      {top && <CapCell data-cap="slot" k={slotLabel(top)} used={top.used} cap={top.cap} state={top.state} sub={capSub(top)}
        title={`同板块同方向持仓上限 = 2% ÷ 每笔风险；占用 ${top.holders.join('、') || '无'}${top.waiting.length ? `；候选 ${top.waiting.join('、')}` : ''}`} />}
      <CapCell data-cap="opens" k={openTag} used={c.opens.used} cap={c.opens.cap} state={hit('daily_open_cap') ? 'full' : undefined}
        sub={hit('daily_open_cap') ? `受限 ${hit('daily_open_cap')}` : undefined} title="单日新开总风险上限 5% ÷ 每笔风险（按美东当日开仓成交计）" />
      <CapCell data-cap="positions" k="持仓" used={c.positions.used} cap={c.positions.cap} state={hit('max_positions') ? 'full' : undefined}
        sub={hit('max_positions') ? `受限 ${hit('max_positions')}` : undefined} title="持仓数上限 = 账户设定与（组合风险上限 ÷ 每笔风险）取大" />
      <CapCell data-cap="risk" k="组合风险" used={pr.used === null ? '—' : (pr.used * 100).toFixed(1)} unit="%"
        cap={pr.cap === null ? null : Math.round(pr.cap * 100)} state={hit('portfolio_risk') ? 'full' : undefined}
        ratio={pr.used !== null && pr.cap ? pr.used / pr.cap : null}
        sub={hit('portfolio_risk') ? `受限 ${hit('portfolio_risk')}` : undefined}
        title={`组合风险 = Σ 股数 × 成本到当前止损 ÷ 权益；现有 + 本笔计划风险超上限时不成交；大盘闸 ${pr.gate ?? '—'}`} />
    </div>
  )
}

/** 阶段条 + 四条引擎泳道 (Grafana state timeline: 实心 = 已运行, 空心 = 未到点, 竖线 = 现在) */
function Lanes({ view }: { view: OppRadarView }) {
  const ck = view.clock
  const L = view.lanes
  // 冬令时机会板首刷 = 07:00 ET (北京 20:00): 横轴起点随首刷前移到整点, 不把 07:00 / 07:30 挤在左缘
  const W = 1120, x0 = 78, x1 = W - 214, T0 = Math.min(480, Math.floor(toMin(ck.board_first) / 60) * 60), T1 = 960
  const X = (m: number) => x0 + ((x1 - x0) * (Math.max(T0, Math.min(T1, m)) - T0)) / (T1 - T0)
  const now = ck.trading_day ? toMin(hmOf(ck.et)) : null
  const done = (t: string) => now !== null && toMin(t) <= now
  const sum = laneText(view)
  const rows: [string, 'board' | 'gap' | 'open' | 'intraday'][] = [['机会板', 'board'], ['跳空', 'gap'], ['开盘榜', 'open'], ['盘中强势', 'intraday']]
  const RH = 19, top = 28, H = top + rows.length * RH + 16
  const phases: [number, number, string][] = [[T0, 570, '盘前'], [570, 630, '开盘'], [630, toMin(ck.close ?? '16:00'), '盘中']]
  const run = (y: number, a: string, b: string, col: string) => {
    const A = toMin(a), B = toMin(b)
    return (
      <g key={`r${y}${a}`}>
        <rect x={X(A)} y={y - 3.5} width={Math.max(X(B) - X(A), 0)} height={7} rx={3.5} fill={col} fillOpacity={0.2} />
        {now !== null && now > A && <rect x={X(A)} y={y - 3.5} width={Math.max(X(Math.min(B, now)) - X(A), 0)} height={7} rx={3.5} fill={col} fillOpacity={0.7} />}
      </g>
    )
  }
  const tick = (y: number, t: string, col: string, big?: boolean, ran?: boolean) => (
    <circle key={`t${y}${t}`} cx={X(toMin(t))} cy={y} r={big ? 4.6 : 3} fill={(ran ?? done(t)) ? col : 'hsl(var(--surface))'} stroke={col} strokeWidth={1.4} />
  )
  // 机会板每 30 分钟一刷 (首刷 → 收盘): 已落的快照实心, 未到的空心
  const boardSched: string[] = []
  for (let m = toMin(ck.board_first); m <= toMin(ck.close ?? '16:00') - 30; m += 30) boardSched.push(`${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`)
  const boardTicks = [...new Set([...boardSched, ...L.board.ticks])].sort()
  return (
    <div data-rail-lanes className="mt-2">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="引擎泳道" className="hidden h-auto w-full overflow-visible md:block">
        {phases.map(([a, b, l]) => {
          const on = now !== null && now >= a && now < b, past = now !== null && now >= b
          return (
            <g key={l}>
              <rect x={X(a) + 1} y={2} width={X(b) - X(a) - 2} height={20} rx={4} className={on ? 'fill-accent' : past ? 'fill-zinc-200 dark:fill-zinc-700' : 'fill-elevated'} />
              <text x={(X(a) + X(b)) / 2} y={16.5} textAnchor="middle" fontSize={12} fontWeight={on ? FW.bold : FW.semibold} className={on ? 'fill-white' : 'fill-secondary'}>{`${l}${past ? ' ✓' : ''}`}</text>
            </g>
          )
        })}
        <text x={x0 - 10} y={16.5} textAnchor="end" fontSize={12} className="fill-secondary">阶段</text>
        {Array.from({ length: (T1 - T0) / 60 + 1 }, (_, i) => T0 + i * 60).map((m) => (
          <g key={m}>
            <line x1={X(m)} x2={X(m)} y1={top - 3} y2={top + rows.length * RH} className="stroke-border" strokeOpacity={0.6} />
            <text x={X(m)} y={H - 2} textAnchor="middle" fontSize={11} className="fill-muted font-mono">{`${String(m / 60).padStart(2, '0')}:00`}</text>
          </g>
        ))}
        {rows.map(([lbl, key], i) => {
          const y = top + i * RH + RH / 2
          const accent = 'hsl(var(--accent))', ink = 'hsl(var(--fg-secondary))'
          return (
            <g key={key} data-lane={key}>
              <text x={x0 - 10} y={y + 4} textAnchor="end" fontSize={12} fontWeight={600} className="fill-foreground">{lbl}</text>
              <line x1={x0} x2={x1} y1={y} y2={y} className="stroke-border" />
              {key === 'board' && (
                <>
                  {run(y, L.board.window[0], L.board.window[1], accent)}
                  {boardTicks.map((t) => tick(y, t, accent, false, L.board.ticks.includes(t)))}
                  {L.board.full.map(([a, b], j) => a && (
                    <rect key={`f${j}`} x={X(toMin(a))} y={y + 5} width={Math.max(X(toMin(b ?? (now !== null ? hmOf(ck.et) : a))) - X(toMin(a)), 2)} height={3.5} rx={1} className="fill-warning" />
                  ))}
                  {L.board.fills.map((f, j) => f.at && <path key={`v${j}`} d={`M${X(toMin(f.at)) - 4.5} ${y - 11} l4.5 5.5 l4.5 -5.5z`} className="fill-accent" />)}
                </>
              )}
              {key === 'gap' && (<>{run(y, '04:00', '09:00', ink)}{tick(y, '09:00', ink, true, !!L.gap.at)}</>)}
              {key === 'open' && (<>{run(y, '09:30', L.open.ready_at, ink)}{tick(y, L.open.ready_at, ink, true, !!L.open.at)}</>)}
              {key === 'intraday' && (<>{run(y, L.intraday.schedule[0] ?? '10:00', L.intraday.schedule[L.intraday.schedule.length - 1] ?? '15:00', ink)}
                {L.intraday.schedule.map((t) => tick(y, t, ink, false, L.intraday.ticks.includes(t)))}</>)}
              <text x={x1 + 26} y={y + 4} fontSize={12} fontWeight={600} className="fill-foreground">{sum[key]}</text>
            </g>
          )
        })}
        {now !== null && <line x1={X(now)} x2={X(now)} y1={0} y2={top + rows.length * RH + 1} className="stroke-foreground" strokeWidth={1.6} />}
      </svg>
      <div data-lanes-m className="mt-1 md:hidden">
        {now !== null && (
          <div className="mb-1.5 flex gap-[3px]">
            {phases.map(([a, b, l]) => {
              const on = now >= a && now < b, past = now >= b
              return <span key={l} className={`flex-1 rounded text-center text-[12px] font-semibold leading-6 ${on ? 'bg-accent text-white dark:bg-blue-700' : past ? 'bg-zinc-200 text-secondary dark:bg-zinc-700' : 'bg-elevated text-secondary'}`}>{`${l}${past ? ' ✓' : ''}`}</span>
            })}
          </div>
        )}
        {rows.map(([lbl, key]) => (
          <div key={key} data-lane-m={key} className="grid grid-cols-[64px_1fr] gap-1.5 border-t border-elevated text-[12px] leading-[22px]">
            <b className="font-semibold text-foreground">{lbl}</b><span className="text-secondary">{sum[key]}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function StatusRail({ view, onGear, gear }: { view: OppRadarView; onGear?: () => void; gear?: ReactNode }) {
  return (
    <section data-status-rail className="mb-3.5 rounded-card border border-border bg-surface px-3.5 pb-2 pt-2.5 max-md:px-2.5">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2">
        <Clock view={view} />
        <Caps view={view} />
        {gear && onGear && <div className="max-md:hidden">{gear}</div>}
      </div>
      <Lanes view={view} />
    </section>
  )
}
