// ZenTrade 标志 — 禅圆 (Ensō) 包裹一根阳线 K 线
//
// 概念:
//   - 外环: 一笔画成的禅圆 — 起笔饱满, 行笔渐干, 收笔提锋, 右上留口 (不求圆满)
//   - 中央 wick+body: 一根阳线 — 实体偏上、下影更长 (站稳, 波段只做确定的机会)
//   - 禅 = 耐心等待, 交易 = 一次出手 — 名字 ZenTrade 的两半
//
// 纯矢量 (环为笔触轮廓填充, 小尺寸不糊); 用 currentColor 继承父级颜色, 方便切换品牌色。
interface LogoProps {
  className?: string
  size?: number
  style?: React.CSSProperties
}

/** 禅圆笔触轮廓 (生成式: 笔宽沿弧长变化, 起笔 3.6 → 收笔 0.3) */
const ENSO_PATH =
  'M27.75 10.01C28.17 10.81 28.52 11.64 28.77 12.51C29.01 13.37 29.16 14.27 29.21 15.17C29.25 16.06 29.19 16.97 29.04 17.85C28.9 18.74 28.67 19.6 28.36 20.44C28.05 21.28 27.66 22.09 27.19 22.85C26.72 23.61 26.18 24.32 25.56 24.97C24.95 25.62 24.27 26.2 23.54 26.71C22.81 27.22 22.03 27.66 21.21 28.01C20.4 28.37 19.54 28.63 18.67 28.81C17.8 28.99 16.91 29.08 16.02 29.08C15.13 29.08 14.24 28.99 13.37 28.81C12.5 28.63 11.64 28.36 10.83 28C10.01 27.65 9.23 27.21 8.51 26.7C7.78 26.19 7.11 25.6 6.5 24.96C5.89 24.31 5.35 23.6 4.89 22.84C4.42 22.08 4.04 21.28 3.74 20.44C3.44 19.61 3.22 18.74 3.1 17.87C2.97 16.99 2.95 16.1 3.03 15.22C3.12 14.34 3.31 13.47 3.59 12.64C3.87 11.81 4.25 11.02 4.7 10.28C5.16 9.55 5.69 8.87 6.28 8.26C6.87 7.65 7.52 7.11 8.21 6.64C8.9 6.17 9.63 5.78 10.38 5.46C11.13 5.14 11.9 4.9 12.69 4.73C13.47 4.56 14.26 4.46 15.05 4.43C15.84 4.4 16.63 4.43 17.41 4.53A0.16 0.16 0 0 1 17.37 4.84C16.61 4.75 15.84 4.75 15.08 4.85C14.33 4.94 13.59 5.13 12.88 5.39C12.18 5.66 11.51 6.01 10.89 6.42C10.27 6.83 9.71 7.31 9.21 7.84C8.7 8.36 8.26 8.93 7.89 9.54C7.51 10.14 7.2 10.77 6.95 11.42C6.71 12.07 6.53 12.73 6.4 13.4C6.28 14.07 6.22 14.74 6.21 15.41C6.2 16.08 6.24 16.74 6.34 17.4C6.43 18.05 6.6 18.7 6.83 19.33C7.05 19.95 7.35 20.55 7.7 21.11C8.05 21.68 8.45 22.2 8.91 22.68C9.37 23.16 9.87 23.6 10.41 23.98C10.96 24.35 11.54 24.68 12.15 24.94C12.76 25.2 13.39 25.39 14.04 25.52C14.69 25.66 15.35 25.72 16.01 25.72C16.67 25.71 17.33 25.64 17.98 25.51C18.63 25.37 19.26 25.17 19.86 24.9C20.47 24.64 21.05 24.31 21.58 23.93C22.12 23.55 22.62 23.11 23.07 22.63C23.52 22.15 23.92 21.62 24.26 21.05C24.6 20.49 24.88 19.89 25.1 19.27C25.31 18.65 25.46 18 25.53 17.35C25.6 16.7 25.6 16.05 25.55 15.4C25.49 14.75 25.39 14.11 25.23 13.47C25.07 12.84 24.85 12.23 24.56 11.64A1.79 1.79 0 0 1 27.75 10.01Z'

export function Logo({ className, size = 32, style }: LogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="none"
      className={className}
      style={style}
      role="img"
      aria-label="ZenTrade"
    >
      {/* 禅圆: 一笔画, 右上留口 */}
      <path d={ENSO_PATH} fill="currentColor" />
      {/* K 线 wick (上下影线, 半透明) */}
      <line
        x1="16" y1="7.6" x2="16" y2="24.4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeOpacity="0.55"
      />
      {/* K 线 body — 阳线实体偏上, 下影更长 */}
      <rect
        x="12.8" y="10.4" width="6.4" height="8.8"
        fill="currentColor"
        rx="0.8"
      />
    </svg>
  )
}

/** 标志 + 字标 (ZENTRADE) 横排组合 — 侧栏/顶栏/登录页共用 */
export function Wordmark({
  className,
  size = 26,
  color,
  glow = true,
  tagline,
}: {
  className?: string
  size?: number
  color?: string
  glow?: boolean
  tagline?: string
}) {
  return (
    <div className={`flex items-center gap-2 ${className ?? ''}`}>
      <Logo
        size={size}
        className="shrink-0"
        style={{
          color,
          filter: glow && color ? `drop-shadow(0 0 8px ${color}66)` : undefined,
        }}
      />
      <div className="min-w-0 leading-none">
        <div className="whitespace-nowrap font-bold text-[12px] tracking-[0.2em] text-foreground">
          ZEN<span style={{ color }}>TRADE</span>
        </div>
        {tagline && (
          <div className="mt-1 whitespace-nowrap text-[9px] tracking-[0.08em] text-muted">{tagline}</div>
        )}
      </div>
    </div>
  )
}
