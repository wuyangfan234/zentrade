/** 机会页状态行 (0.39.0 起; 2026-11-15 B 方案): 双时钟 + 当前时段 + 下一事件。
 * 名额移到右栏「账户」, 四条引擎泳道退役 (各组的下次时刻写在盘中异动各组里)。只展示 radar_view (后端投影, 契约 opp-radar-v1)。 */
import type { OppRadarView } from '@/lib/api'
import { bjHm, dayTag, EVENT_ZH, PHASE_ZH } from '@/components/opportunities/terms'

const hmOf = (iso?: string | null) => (iso && iso.length >= 16 ? iso.slice(11, 16) : '')

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
        <b className="tabular-nums text-[20px] font-semibold leading-[26px] tracking-tight text-foreground">{hmOf(ck.et)}</b>
        <span className="text-[12px] text-secondary">{`ET ${tag ? '' : ''}${weekdayOf(today)}`}</span>
        <span className="text-[12px] text-muted">{`北京 ${bjHm(ck.et)}`}</span>
      </div>
      <div data-rail-phase className="flex flex-wrap items-center gap-1.5">
        <span data-rail-phase-badge className={`rounded-[4px] px-2 py-0.5 text-[13px] font-semibold ${ck.trading_day && ck.phase !== 'after' ? 'bg-accent/10 text-accent dark:text-blue-400' : 'bg-elevated text-secondary'}`}>
          {ck.entry_window ? `${phase} · 入场窗` : phase}
        </span>
        <span data-rail-next className="whitespace-nowrap text-[13px] font-semibold text-secondary">
          {'下一项 '}
          <b className="tabular-nums text-accent">{`${tag ? `${tag} ` : ''}${nxt.at} ET`}</b>
          {` ${EVENT_ZH[nxt.code] ?? nxt.code}`}
          <span className="ml-1 font-normal text-muted">{`（北京 ${bjHm(nxt.ts)}）`}</span>
        </span>
      </div>
    </div>
  )
}

const weekdayOf = (iso: string) => ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(`${iso}T12:00:00Z`).getUTCDay()]

export function StatusLine({ view }: { view: OppRadarView }) {
  return (
    <section data-status-rail className="mb-2.5 px-1">
      <Clock view={view} />
    </section>
  )
}
