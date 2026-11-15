/**
 * 主题压力 (2026-11-15; 只显示): 用自选分组当主题, 主题内持仓一起再跌 (空头再涨) 压力跌幅时账户亏多少。
 * 一行一个主题, 最多 5 行, 按「挂单全成交后」的压力损失排序。不拦单, 不进交易环 / 准入 / 下单簿。
 * 计划止损风险与压力损失是两回事: 前者假设止损按价成交, 后者是整组同向跳空; 口径放悬停。
 */
import type { ThemeExposure } from '@/lib/api'
import { badge } from '@/components/ui/badge'

const usd0 = (v: number) => `$${Math.abs(Math.round(v)).toLocaleString('en-US')}`
const loss = (v: number) => (v > 0 ? `−${usd0(v)}` : '$0')
const pc = (v: number | null | undefined) => (typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(1)}%` : '—')
const pc2 = (v: number | null | undefined) => (typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(2)}%` : '—')
const lossPct = (v: number | null | undefined) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? `−${v.toFixed(2)}%` : '—')

const TITLE = '压力损失 = 主题内持仓名义 × 压力跌幅（空头按上涨） · 名义按现价 · 一票多主题各自计入 · 只显示 · 不拦单'

export function ThemeBar({ t }: { t: ThemeExposure }) {
  const k = t.stress_pct
  const tiers = t.tiers.map((x) => `${x.name} ${x.n} 只 · 覆盖过半 · 不算主题`).join('\n')
  const meta = t.positions > 0
    ? `持仓 ${t.positions} · 名义 ${pc(t.total.notional_pct)} · 计划止损 ${usd0(t.total.stop_risk)}（${pc2(t.total.stop_risk_pct)}） · 全部跌 ${k}% ${loss(t.total.stress_loss)}（${lossPct(t.total.stress_pct_nav)}）`
    : '暂无持仓'
  return (
    <section data-theme-bar className="mb-3 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2">
        <h2 className="shrink-0 text-[14px] font-semibold text-foreground" title={TITLE}>主题压力</h2>
        <span className={badge('limit')} title={TITLE}>{`跌 ${k}%`}</span>
        <span data-theme-meta className="min-w-0 text-[12px] tabular-nums text-muted xl:ml-auto" title={tiers || undefined}>{meta}</span>
      </div>
      {t.rows.length > 0 && (
        <div className="overflow-x-auto border-t border-border/70">
          <table className="w-full min-w-[640px] text-[13px] tabular-nums">
            <thead><tr className="h-8 bg-elevated text-[12px] text-muted">
              {['主题', '持仓', '名义', '计划止损', `跌 ${k}%`, '挂单全成交后'].map((h, i) => (
                <th key={h} className={`whitespace-nowrap px-3 font-medium ${i === 0 ? 'text-left' : 'text-right'}`}>{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {t.rows.map((r) => (
                <tr key={r.theme} data-theme-row={r.theme} className="h-8 border-t border-border/70">
                  <td className="whitespace-nowrap px-3 text-left font-semibold text-foreground" title={[r.symbols.join(' '), r.pending.length ? `挂单 ${r.pending.join(' ')}` : ''].filter(Boolean).join(' · ')}>{r.theme}</td>
                  <td className="px-3 text-right text-secondary" title={r.symbols.join(' ')}>{r.count}</td>
                  <td className="px-3 text-right text-secondary">{pc(r.notional_pct)}</td>
                  <td className="px-3 text-right text-secondary">{usd0(r.stop_risk)}</td>
                  <td className="whitespace-nowrap px-3 text-right font-semibold text-foreground">{`${loss(r.stress_loss)} · ${lossPct(r.stress_pct_nav)}`}</td>
                  <td className="whitespace-nowrap px-3 text-right text-secondary" title={r.pending.length ? `挂单 ${r.pending.join(' ')}` : '无挂单'}>
                    {r.pending.length ? `${loss(r.after_fill_loss)} · ${lossPct(r.after_fill_pct_nav)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {t.hidden > 0 && <div className="border-t border-border/70 px-3 py-1 text-[12px] text-muted">{`另 ${t.hidden} 个主题`}</div>}
        </div>
      )}
    </section>
  )
}
