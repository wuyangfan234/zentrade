import { useEffect, useMemo, useRef, useState } from 'react'

export interface EquityPoint {
  date: string
  nav: number
}

const H = 132
// 左右只留圆点半径的余量, 让曲线与卡片标题左缘对齐
const PAD = { top: 10, right: 5, bottom: 8, left: 5 }
const STROKE = 'hsl(var(--bull))'
const FALLBACK_W = 640
// 一个日期标签 (MM-DD, 10px) 约 32px 宽, 相邻标签至少隔这么多像素才全标
const LABEL_MIN_GAP = 44

function fmtUsd(n: number): string {
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
}

function fmtPct(r: number): string {
  return `${r >= 0 ? '+' : ''}${(r * 100).toFixed(2)}%`
}

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

/**
 * 纸面账户日终净值曲线 (纯 SVG, 无图表库)。
 *
 * - 每个交易日一个真实顶点, 不插值、不平滑, 不画不存在的中间值。
 * - 线条/面积/顶点统一红色 (--bull, 全站红涨口径); 面积只填到本金基线, 基线下方不着色。
 * - 读数固定在图上方一行 (悬停=当日, 否则=最新), 永不遮挡曲线;
 *   同时给「当日」与「累计 (相对本金)」两个口径, 不让人把累计误读成单日涨幅。
 * - 按容器真实像素宽度绘制 (不拉伸 viewBox), 圆点不变形; 日期空间够就全标, 不够等距抽稀且末日必标。
 */
export function EquityCurve({ points, principal }: { points: EquityPoint[]; principal: number }) {
  const [hover, setHover] = useState<number | null>(null)
  const [boxRef, W] = useWidth<HTMLDivElement>()
  const last = points.length - 1

  const geom = useMemo(() => {
    const values = points.map(p => p.nav)
    let lo = Math.min(principal, ...values)
    let hi = Math.max(principal, ...values)
    const span = hi - lo
    // 留白 15%; 完全平盘时给本金 ±0.5% 的可视范围
    const pad = span > 0 ? span * 0.15 : Math.max(principal * 0.005, 1)
    lo -= pad
    hi += pad
    const innerW = W - PAD.left - PAD.right
    const innerH = H - PAD.top - PAD.bottom
    const x = (i: number) => PAD.left + (points.length === 1 ? innerW / 2 : (i / (points.length - 1)) * innerW)
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * innerH
    const xy = points.map((p, i) => [x(i), y(p.nav)] as const)
    const baseY = y(principal)
    const line = xy.map(([px, py], i) => `${i === 0 ? 'M' : 'L'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ')
    const area = `${line} L${xy[xy.length - 1][0].toFixed(1)},${baseY.toFixed(1)} L${xy[0][0].toFixed(1)},${baseY.toFixed(1)} Z`
    const step = points.length > 1 ? innerW / (points.length - 1) : innerW
    const stride = Math.max(1, Math.ceil(LABEL_MIN_GAP / step))
    const labels: number[] = []
    for (let i = 0; i <= last; i += stride) labels.push(i)
    // 末日必标; 与前一个标签太近就让位给末日
    if (labels[labels.length - 1] !== last) {
      if (labels.length > 1 && (last - labels[labels.length - 1]) * step < LABEL_MIN_GAP) labels.pop()
      labels.push(last)
    }
    return { xy, line, area, baseY, bottom: PAD.top + innerH, step, labels }
  }, [points, principal, W, last])

  const shown = hover ?? last
  const cur = points[shown]
  const prev = shown > 0 ? points[shown - 1] : null
  const dayRet = prev && prev.nav > 0 ? cur.nav / prev.nav - 1 : null
  const cumRet = principal > 0 ? cur.nav / principal - 1 : 0
  const tone = (r: number) => (r >= 0 ? 'text-bull' : 'text-bear')

  return (
    <div ref={boxRef}>
      <div data-equity-readout className="mb-2 flex h-4 items-baseline gap-3 text-[11px] tabular-nums">
        <span className="text-secondary">{hover == null ? `最新 ${cur.date.slice(5)}` : cur.date.slice(5)}</span>
        <span className="font-medium text-foreground">{fmtUsd(cur.nav)}</span>
        <span className="text-secondary">
          当日 <span className={dayRet == null ? 'text-secondary' : tone(dayRet)}>{dayRet == null ? '—' : fmtPct(dayRet)}</span>
        </span>
        <span className="text-secondary">
          累计 <span className={tone(cumRet)}>{fmtPct(cumRet)}</span>
        </span>
      </div>

      <svg
        data-equity-curve
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        className="block w-full overflow-visible"
        role="img"
        aria-label={`净值曲线，${points.length} 个交易日`}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="equity-fill" gradientUnits="userSpaceOnUse" x1="0" y1={PAD.top} x2="0" y2={geom.baseY}>
            <stop offset="0%" stopColor="hsl(var(--bull))" stopOpacity="0.24" />
            <stop offset="100%" stopColor="hsl(var(--bull))" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        <path d={geom.area} fill="url(#equity-fill)" />
        <line
          data-equity-baseline
          x1={PAD.left}
          x2={W - PAD.right}
          y1={geom.baseY}
          y2={geom.baseY}
          stroke="hsl(var(--fg-secondary))"
          strokeOpacity={0.55}
          strokeDasharray="3 4"
          strokeWidth={1}
        />
        <text
          x={W - PAD.right}
          y={geom.baseY - 4}
          textAnchor="end"
          fontSize={10}
          fill="hsl(var(--fg-secondary))"
          className="tabular-nums"
        >
          {`本金 ${fmtUsd(principal)}`}
        </text>
        {hover != null && (
          <line
            data-equity-crosshair
            x1={geom.xy[hover][0]}
            x2={geom.xy[hover][0]}
            y1={PAD.top}
            y2={geom.bottom}
            stroke="hsl(var(--fg-secondary))"
            strokeOpacity={0.45}
            strokeWidth={1}
          />
        )}
        <path
          data-equity-line
          d={geom.line}
          fill="none"
          stroke={STROKE}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {geom.xy.map(([px, py], i) => {
          const strong = i === shown
          return (
            <circle
              key={points[i].date}
              data-equity-point
              data-active={strong || undefined}
              cx={px}
              cy={py}
              r={strong ? 3.5 : 2.5}
              fill={strong ? STROKE : 'hsl(var(--surface))'}
              stroke={STROKE}
              strokeWidth={1.5}
            />
          )
        })}
        {geom.xy.map(([px], i) => (
          <rect
            key={`hit-${points[i].date}`}
            data-equity-hit={i}
            x={Math.max(0, px - geom.step / 2)}
            y={0}
            width={geom.step}
            height={H}
            fill="transparent"
            onMouseOver={() => setHover(i)}
          />
        ))}
      </svg>

      <div className="relative mt-1.5 h-3.5 text-[10px] leading-none tabular-nums text-secondary">
        {geom.labels.map(i => (
          <span
            key={points[i].date}
            data-equity-label
            className="absolute whitespace-nowrap"
            style={{
              left: geom.xy[i][0],
              // 首标签左对齐、末标签右对齐 (不出卡片), 中间标签居中对准顶点
              transform: i === 0 ? 'none' : i === last ? 'translateX(-100%)' : 'translateX(-50%)',
            }}
          >
            {points[i].date.slice(5)}
          </span>
        ))}
      </div>
    </div>
  )
}
