/** 市场状态条 (P1-A regime 三态): 机会页与因子页共用这一个组件 (同一状态 / 同一时间戳 / 同一指纹, 不两套)。
 * 数据 = 板 meta.regime (swing_desk.regime_for_snapshot)。三态有颜色, 也有图标 + 文字, 不只靠颜色。
 * 没有 regime (回滚开关关 / 旧快照) → 明确降级说明, 不编状态。 */
import { Minus, ShieldAlert, TrendingUp } from 'lucide-react'
import type { SwingRegimeMeta } from '@/lib/api'
import { nextRejudge, parseReason, REGIME_SRC_ZH, REGIME_STATES, regimeFp } from '@/components/opportunities/p1'

const TONE: Record<string, { cls: string; Icon: typeof TrendingUp; note: string }> = {
  进攻: { cls: 'border-accent/60 bg-accent/10 text-accent dark:text-blue-300', Icon: TrendingUp, note: '多头开 · 空头关' },
  震荡: { cls: 'border-border bg-elevated text-foreground', Icon: Minus, note: '多头开 · 空头关' },
  防守: { cls: 'border-warning/70 bg-warning/15 text-warning', Icon: ShieldAlert, note: '多头关 · 空头开 (观察级)' },
}
const tsText = (s?: string | null) => (!s ? '—' : s.length >= 16 ? `${s.slice(5, 10)} ${s.slice(11, 16)}` : s)

export function RegimeBar({ regime, clockEt, tradingDay }: { regime: SwingRegimeMeta | null; clockEt?: string | null; tradingDay?: boolean | null }) {
  if (!regime) {
    return (
      <section data-regime-bar="" data-regime-fp="none" data-regime-missing="" className="mb-3 flex flex-wrap gap-x-2 gap-y-0.5 rounded-card border border-dashed border-border bg-surface px-3 py-2.5 text-[13px]">
        <span className="font-semibold text-foreground">市场状态 · 未提供</span>
        <span className="text-secondary">regime 开关关闭或快照不含 regime · 出卡按旧口径 (SPY &lt; MA50 才开空)</span>
      </section>
    )
  }
  const t = TONE[regime.state] ?? TONE['震荡']
  const reasons = (regime.reasons ?? []).map(parseReason)
  const met = reasons.filter((r) => r.ok).length
  return (
    <section data-regime-bar="" data-regime-fp={regimeFp(regime)} data-regime-state={regime.state} className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2.5">
        <h2 className="shrink-0 text-[14px] font-semibold text-foreground">市场状态</h2>
        <span data-regime-chip="" className={`inline-flex items-center gap-1.5 rounded-btn border px-2 py-0.5 text-[15px] font-bold ${t.cls}`}>
          <t.Icon className="h-4 w-4" aria-hidden="true" />{regime.state}
        </span>
        <span role="group" aria-label="三态" className="inline-flex overflow-hidden rounded-btn border border-border text-[12px]">
          {REGIME_STATES.map((s) => (
            <span key={s} data-regime-seg={s} aria-current={s === regime.state ? 'true' : undefined}
              className={`px-2 py-0.5 ${s === regime.state ? 'bg-foreground font-semibold text-surface' : 'text-secondary'}`}>{s}</span>
          ))}
        </span>
        <span className="text-[12px] text-secondary">{t.note}</span>
        {regime.source === 'intraday' && regime.snapshot_state && regime.snapshot_state !== regime.state && (
          <span data-regime-down="" className="rounded-[4px] border border-warning/60 px-1.5 text-[12px] font-medium text-warning">{`盘中降级 · 快照为${regime.snapshot_state}`}</span>
        )}
        <span data-regime-meta="" className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] tabular-nums text-secondary">
          <span>{`更新 ${tsText(regime.as_of)} · ${REGIME_SRC_ZH[regime.source ?? ''] ?? regime.source ?? '—'}`}</span>
          <span data-regime-next="">{`下次重判 ${nextRejudge(clockEt, tradingDay)}`}</span>
          <span className="text-muted" title={`两页同一状态条 · 指纹 ${regimeFp(regime)}`}>同一状态条</span>
        </span>
      </div>
      <div data-regime-reasons="" className="grid grid-cols-1 border-t border-border/70 sm:grid-cols-2 xl:grid-cols-3">
        {reasons.length === 0 ? <div className="px-3 py-2 text-[12px] text-muted">无依据明细</div> : reasons.map((r) => (
          <div key={r.raw} data-regime-reason={r.ok === null ? 'unknown' : r.ok ? 'ok' : 'no'} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-border/50 px-3 py-1.5 text-[12px] last:border-b-0">
            <span aria-hidden="true" className={`w-4 shrink-0 text-center font-bold ${r.ok ? 'text-accent dark:text-blue-300' : r.ok === false ? 'text-warning' : 'text-muted'}`}>{r.ok ? '✓' : r.ok === false ? '✗' : '?'}</span>
            <span className="min-w-0 flex-1 text-secondary">{r.label}</span>
            <b className="shrink-0 tabular-nums text-foreground">{r.value}</b>
            <span className="shrink-0 tabular-nums text-muted">{`门槛 ${r.threshold}`}</span>
            <span className="shrink-0 font-medium text-foreground">{r.ok ? '满足' : r.ok === false ? '未满足' : '—'}</span>
          </div>
        ))}
      </div>
      <div className="border-t border-border/70 px-3 py-1 text-[11px] text-muted">{`进攻条件满足 ${met}/${reasons.length} · 降级任一层单次触发 · 升级需慢层同意或快层连续 2 个整点确认 · 盘中只降不升`}</div>
    </section>
  )
}
