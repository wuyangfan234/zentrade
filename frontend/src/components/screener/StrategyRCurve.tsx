import { useEffect, useMemo, useRef, useState } from 'react'
import { dayPoints, fmtR, md, toneClass, type CurvePoint } from '@/lib/strategyPanel'

const H = 190
const PAD = { top: 10, right: 58, bottom: 22, left: 40 }
const FALLBACK_W = 640
const DOT_MAX = 60          // 平仓日超过这个数只画线, 不逐日打点
const DAY_MS = 86_400_000

/** 容器实际像素宽度; 无布局环境 (测试) 回退固定宽度。 */
function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null)
  const [w, setW] = useState(FALLBACK_W)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => { if (el.clientWidth > 0) setW(el.clientWidth) }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

// 日期只按整日换算 (UTC 零点取整), 不经本地时区, 否则刻度会整体偏一天
const dayNo = (s: string) => Math.round(Date.parse(`${s.slice(0, 10)}T00:00:00Z`) / DAY_MS)
const isoOf = (n: number) => new Date(n * DAY_MS).toISOString().slice(0, 10)

function tickStep(span: number): number {
  return span > 30 ? 10 : span > 12 ? 5 : span > 5 ? 2 : 1
}

/**
 * 模拟累计 R 曲线 (纯 SVG): 一个平仓日一个台阶 (同日几笔并成一点), 不插值; 横轴按真实日期。
 * - 蓝色粗线 = 模拟累计; 灰色虚线 = 同样笔数按回放每笔 R 该有的累计 (回放只有汇总数, 所以是匀速线)。
 * - 0R 基线带标注; 读数固定在图上方一行 (悬停 = 那一笔, 否则 = 最新), 不用浮层。
 * - 只有一个高亮点: 悬停的那一天, 没悬停时是最后一天。
 */
export function StrategyRCurve({ points: trades, since }: { points: CurvePoint[]; since?: string | null }) {
  const [hover, setHover] = useState<number | null>(null)
  const [boxRef, W] = useWidth<HTMLDivElement>()
  const points = useMemo(() => dayPoints(trades), [trades])
  const last = points.length - 1

  const geom = useMemo(() => {
    if (!points.length) return null
    const hasExp = points[0].exp != null
    const ys = points.flatMap(p => [p.cum, p.exp ?? 0]).concat([0])
    let lo = Math.min(...ys)
    let hi = Math.max(...ys)
    const pad = Math.max(0.5, (hi - lo) * 0.12)
    lo -= pad
    hi += pad
    const t0 = dayNo(since && since <= points[0].d ? since : points[0].d)
    const tLast = Math.max(dayNo(points[last].d), t0 + 1)
    // 最后一笔之后留一小段 (累计值保持不变), 末端不贴着右缘
    const t1 = tLast + Math.max(1, Math.round((tLast - t0) * 0.04))
    const innerW = W - PAD.left - PAD.right
    const innerH = H - PAD.top - PAD.bottom
    const xt = (t: number) => PAD.left + ((t - t0) / (t1 - t0)) * innerW
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * innerH
    const xs = points.map(p => xt(dayNo(p.d)))
    const step = (k: 'cum' | 'exp') =>
      `M${PAD.left} ${y(0).toFixed(1)}` + points.map((p, i) => `H${xs[i].toFixed(1)}V${y(p[k] ?? 0).toFixed(1)}`).join('')
      + `H${(W - PAD.right).toFixed(1)}`
    const ticks: number[] = []
    const st = tickStep(hi - lo)
    for (let v = Math.ceil(lo / st) * st; v <= hi; v += st) if (v !== 0) ticks.push(v)
    const marks = tLast - t0 < 3 ? [t0, tLast] : [t0, t0 + (tLast - t0) / 3, t0 + ((tLast - t0) * 2) / 3, tLast]
    // 右端两个终值标签相距不足 14px 时上下错开
    let ya = y(points[last].cum)
    let yb = hasExp ? y(points[last].exp!) : null
    if (yb != null && Math.abs(ya - yb) < 14) {
      const m = (ya + yb) / 2
      if (ya <= yb) { ya = m - 7; yb = m + 7 } else { ya = m + 7; yb = m - 7 }
    }
    return { hasExp, xs, y, xt, step, ticks, marks, ya, yb, x0: PAD.left, x1: W - PAD.right }
  }, [points, since, W, last])

  if (!geom) {
    return <div data-curve-empty className="grid h-[120px] place-items-center text-xs text-muted">暂无成交</div>
  }

  const cur = hover != null && hover <= last ? hover : last
  const p = points[cur]
  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.ownerSVGElement?.getBoundingClientRect()
    if (!box) return
    const px = e.clientX - box.left
    let best = 0
    let dist = Infinity
    geom.xs.forEach((x, i) => {
      const d = Math.abs(x - px)
      if (d <= dist) { best = i; dist = d }
    })
    setHover(best)
  }

  return (
    <div>
      <div data-curve-read className="flex min-h-5 flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
        <span className="whitespace-nowrap">{hover == null ? '截至' : '平仓'} <b className="font-semibold text-foreground num">{md(p.d)}</b></span>
        {hover != null && (
          <span className="whitespace-nowrap">{p.symbol ?? `${p.n} 笔`} <b className={`font-semibold num ${toneClass(p.r)}`}>{fmtR(p.r)}</b></span>
        )}
        <span className="whitespace-nowrap">模拟累计 <b className={`font-semibold num ${toneClass(p.cum, 1)}`}>{fmtR(p.cum, 1)}</b></span>
        {p.exp != null && (
          <>
            <span className="whitespace-nowrap">回放期望 <b className="font-semibold text-foreground num">{fmtR(p.exp, 1)}</b></span>
            <span className="whitespace-nowrap">差 <b className="font-semibold text-foreground num">{fmtR(p.cum - p.exp, 1)}</b></span>
          </>
        )}
      </div>
      <div ref={boxRef} className="mt-1.5">
        <svg data-curve width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" onMouseLeave={() => setHover(null)}>
          {geom.ticks.map(v => (
            <g key={v}>
              <line x1={geom.x0} x2={geom.x1} y1={geom.y(v)} y2={geom.y(v)} stroke="hsl(var(--border))" strokeWidth={1} opacity={0.5} />
              <text x={geom.x0 - 6} y={geom.y(v) + 3} textAnchor="end" fontSize={10} className="fill-muted num">{`${v > 0 ? '+' : ''}${v}R`}</text>
            </g>
          ))}
          <line x1={geom.x0} x2={geom.x1} y1={geom.y(0)} y2={geom.y(0)} stroke="hsl(var(--fg-muted))" strokeWidth={1} />
          <text x={geom.x0 - 6} y={geom.y(0) + 3} textAnchor="end" fontSize={10} className="fill-muted num">0R</text>
          {geom.hasExp && (
            <path data-curve-exp d={geom.step('exp')} fill="none" stroke="hsl(var(--fg-muted))" strokeWidth={1.5} strokeDasharray="4 4" />
          )}
          <path data-curve-line d={geom.step('cum')} fill="none" stroke="hsl(var(--accent))" strokeWidth={2.2} strokeLinejoin="round" />
          {points.length <= DOT_MAX && points.map((q, i) => (i === cur ? null : (
            <circle key={i} data-curve-pt cx={geom.xs[i]} cy={geom.y(q.cum)} r={1.8} fill="hsl(var(--accent))" />
          )))}
          <circle data-curve-dot cx={geom.xs[cur]} cy={geom.y(p.cum)} r={3.4} fill="hsl(var(--accent))" />
          <text data-curve-end="cum" x={geom.x1 + 6} y={geom.ya + 3} fontSize={10} className="fill-foreground font-semibold num">{fmtR(points[last].cum, 1)}</text>
          {geom.hasExp && geom.yb != null && (
            <text data-curve-end="exp" x={geom.x1 + 6} y={geom.yb + 3} fontSize={10} className="fill-muted num">{fmtR(points[last].exp, 1)}</text>
          )}
          {geom.marks.map((t, k) => (
            <text key={k} data-curve-x x={geom.xt(t)} y={H - 6} fontSize={10} className="fill-muted num"
              textAnchor={k === 0 ? 'start' : 'middle'}>{md(isoOf(Math.round(t)))}</text>
          ))}
          <rect x={geom.x0} y={0} width={Math.max(0, geom.x1 - geom.x0)} height={H} fill="transparent" onMouseMove={onMove} />
        </svg>
      </div>
    </div>
  )
}
