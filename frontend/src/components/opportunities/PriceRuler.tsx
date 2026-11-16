/** 价格尺 (2026-11-15 B 方案): 左亏右赚 —— 止损永远在左端、目标永远在右端 (做空时价格从左往右变小)。
 * 现价 = 倒三角 + 竖线, 随报价刷新平滑移动 (left 过渡); 进场区 = 蓝带; 成本 = 空心圆 (与止损重合即保本时不画)。
 * 用 HTML 绝对定位而不是 SVG: 卡片多宽字号都不缩放。现价越过止损 / 目标时贴边, 由调用方另标「已破止损」等。 */
import type { ReactNode } from 'react'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const LO = -0.12, HI = 1.12            // 尺的可视范围 (止损 = 0, 目标 = 1); 超出的点贴边

export type RulerGeo = { stop: number; target: number | null; price: number | null; zone?: number[] | null; cost?: number | null; side: 'long' | 'short' }

/** 价格 → 尺上位置 (0~100 %) 与是否越界; 纯函数, 测试直接调 */
export function rulerGeo(g: RulerGeo): { pos: (v: number) => number; off: 'stop' | 'target' | null; far: number } {
  const dir = g.side === 'short' ? -1 : 1
  const pts = [g.price, g.cost, ...(g.zone ?? [])].filter(fin)
  // 没有目标价: 右端取离止损最远的点再放 25%
  const far = fin(g.target) ? g.target : g.stop + dir * Math.max(...pts.map((p) => Math.abs(p - g.stop)), Math.abs(g.stop) * 0.02) * 1.25
  const span = far - g.stop || dir
  const t = (v: number) => (v - g.stop) / span
  const ts = [0, 1, ...pts.map(t)]
  const lo = Math.max(LO, Math.min(...ts)), hi = Math.min(HI, Math.max(...ts))
  const pos = (v: number) => Math.min(Math.max((t(v) - lo) / (hi - lo), 0), 1) * 100
  const tp = fin(g.price) ? t(g.price) : null
  const off = tp === null ? null : tp < 0 ? 'stop' : fin(g.target) && tp > 1 ? 'target' : null
  return { pos, off, far }
}

export function PriceRuler({ geo, lossText, gainText, lossCls, stopLabel, targetLabel, ...rest }: {
  geo: RulerGeo; lossText: ReactNode; gainText: ReactNode; lossCls: string; stopLabel: ReactNode; targetLabel: ReactNode
} & Record<`data-${string}`, string | undefined>) {
  const { pos, off, far } = rulerGeo(geo)
  const ref = geo.zone && geo.zone.length === 2 ? (geo.zone[0] + geo.zone[1]) / 2 : fin(geo.cost) ? geo.cost : fin(geo.price) ? geo.price : geo.stop
  const xs = pos(geo.stop), xt = pos(far), xr = pos(ref)
  const seg = (a: number, b: number) => ({ left: `${Math.min(a, b)}%`, width: `${Math.abs(b - a)}%` })
  const z = geo.zone && geo.zone.length === 2 ? [pos(geo.zone[0]), pos(geo.zone[1])].sort((a, b) => a - b) : null
  const xc = fin(geo.cost) ? pos(geo.cost) : null
  const xp = fin(geo.price) ? pos(geo.price) : null
  return (
    <div {...rest} data-ruler-off={off ?? undefined} className="my-1.5 select-none tabular-nums">
      <div className="flex items-baseline justify-between gap-2 text-[12px] font-semibold leading-4">
        <span data-ruler-loss className={`whitespace-nowrap ${lossCls}`}>{lossText}</span>
        <span data-ruler-gain className="whitespace-nowrap text-bull dark:text-red-400">{gainText}</span>
      </div>
      <div className="relative mx-[5px] mt-1 h-[22px]" role="img" aria-label="价格尺">
        <i className="absolute inset-x-0 top-[9px] h-1 rounded-full bg-elevated" />
        <i className="absolute top-[9px] h-1 rounded-l-full bg-bear/75" style={seg(xs, xr)} />
        <i className="absolute top-[9px] h-1 rounded-r-full bg-bull/75" style={seg(xr, xt)} />
        {z && <i data-ruler-zone className="absolute top-[4px] h-[14px] rounded-[3px] border border-accent bg-accent/40 dark:border-blue-400" style={{ left: `${z[0]}%`, width: `max(4px, ${z[1] - z[0]}%)` }} />}
        <i className="absolute top-[4px] h-[14px] w-[2px] -translate-x-1/2 bg-secondary" style={{ left: `${xs}%` }} />
        {fin(geo.target) && <i className="absolute top-[4px] h-[14px] w-[2px] -translate-x-1/2 bg-secondary" style={{ left: `${xt}%` }} />}
        {xc !== null && Math.abs(xc - xs) > 2 && (
          <i data-ruler-cost className="absolute top-[6px] h-[10px] w-[10px] -translate-x-1/2 rounded-full border-2 border-foreground bg-surface" style={{ left: `${xc}%` }} />
        )}
        {xp !== null && (
          <span data-ruler-price={xp.toFixed(1)} className="absolute top-0 flex -translate-x-1/2 flex-col items-center transition-[left] duration-500 ease-out" style={{ left: `${xp}%` }}>
            <i className="h-0 w-0 border-x-[5px] border-t-[7px] border-x-transparent border-t-foreground" />
            <i className="h-[15px] w-[1.5px] bg-foreground" />
          </span>
        )}
      </div>
      <div className="mt-0.5 flex items-baseline justify-between gap-2 text-[11px] leading-4 text-secondary">
        <span data-ruler-stop className="min-w-0 whitespace-nowrap">{stopLabel}</span>
        <span data-ruler-target className="whitespace-nowrap">{targetLabel}</span>
      </div>
    </div>
  )
}

/** 卡片标题旁的迷你图例: 今日机会有进场区无成本, 持仓有成本无进场区 */
export function RulerLegend({ kind, closed }: { kind: 'board' | 'hold'; closed?: boolean }) {
  return (
    <span data-ruler-legend className="inline-flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] text-secondary">
      <span className="inline-flex items-center gap-1 whitespace-nowrap"><i className="h-0 w-0 border-x-[5px] border-t-[7px] border-x-transparent border-t-foreground" />{closed ? '收盘价' : '现价'}</span>
      {kind === 'board' && <span className="inline-flex items-center gap-1 whitespace-nowrap"><i className="h-[9px] w-[14px] rounded-[2px] border border-accent bg-accent/40 dark:border-blue-400" />进场区</span>}
      {kind === 'hold' && <span className="inline-flex items-center gap-1 whitespace-nowrap"><i className="h-[9px] w-[9px] rounded-full border-2 border-foreground" />成本</span>}
      <span className="inline-flex items-center gap-1 whitespace-nowrap"><i className="h-1 w-[14px] rounded-full bg-bear" />亏</span>
      <span className="inline-flex items-center gap-1 whitespace-nowrap"><i className="h-1 w-[14px] rounded-full bg-bull" />赚</span>
    </span>
  )
}
