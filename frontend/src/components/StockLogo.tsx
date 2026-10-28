// 自选股公司图标 (0.38.0)。
// - 图来自后端本地缓存 /api/watchlist/logos/<代码>?v=<版本>, 128px 方块已按对比度配好衬底;
// - 没有图 / 还在取 / 加载失败 → 字母头像 (代码首字母 + 按代码固定的色相), 同尺寸不跳版;
// - 只做识别用, 不承载状态色 (红绿留给涨跌)。
import { memo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

type LogoMap = Record<string, string>

const EMPTY: LogoMap = {}

/** 自选图标版本表。后台还在补图时每 15 秒复查一次, 补完就停。 */
export function useStockLogos(enabled = true): LogoMap {
  const q = useQuery({
    queryKey: QK.stockLogos,
    queryFn: api.watchlistLogos,
    enabled,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    refetchInterval: query => ((query.state.data?.pending?.length ?? 0) > 0 ? 15_000 : false),
  })
  return q.data?.logos ?? EMPTY
}

export function logoUrl(symbol: string, version: string): string {
  return `/api/watchlist/logos/${encodeURIComponent(symbol)}?v=${encodeURIComponent(version)}`
}

// 字母头像色相: 按代码字符串固定 (同一只票永远同一颜色), 避开红/绿 (涨跌色)。
// 亮度 34%: 8 个色相白字对比度最低 4.88:1 (WCAG AA)。
const HUES = [205, 225, 250, 275, 300, 190, 35, 45]

export function fallbackHue(symbol: string): number {
  let h = 0
  for (let i = 0; i < symbol.length; i++) h = (h * 31 + symbol.charCodeAt(i)) >>> 0
  return HUES[h % HUES.length]
}

export function fallbackText(symbol: string): string {
  const s = symbol.replace(/[^A-Z0-9]/gi, '').toUpperCase()
  return s.slice(0, s.length > 3 ? 2 : 1) || '?'
}

const SIZE_CLASS = {
  sm: 'h-5 w-5 rounded-[5px] text-[9px]',
  md: 'h-7 w-7 rounded-md text-[11px]',
  lg: 'h-9 w-9 rounded-lg text-xs',
} as const

export const StockLogo = memo(function StockLogo({
  symbol,
  version,
  size = 'md',
  className,
}: {
  symbol: string
  version?: string
  size?: keyof typeof SIZE_CLASS
  className?: string
}) {
  const [failed, setFailed] = useState<string | null>(null)
  const src = version ? logoUrl(symbol, version) : null
  const showImg = src != null && failed !== src
  const box = cn(
    'relative inline-flex shrink-0 select-none items-center justify-center overflow-hidden',
    // 描边用前景色 15%: 白底图标在亮色页面、深底图标在暗色页面都看得见边界
    'ring-1 ring-inset ring-foreground/15',
    SIZE_CLASS[size],
    className,
  )
  if (showImg) {
    return (
      <span className={box} data-stock-logo={symbol} data-logo-kind="image">
        <img
          src={src}
          alt=""
          aria-hidden="true"
          loading="lazy"
          decoding="async"
          draggable={false}
          className="h-full w-full object-cover"
          onError={() => setFailed(src)}
        />
      </span>
    )
  }
  const hue = fallbackHue(symbol)
  return (
    <span
      className={cn(box, 'font-semibold leading-none tracking-tight')}
      style={{ backgroundColor: `hsl(${hue} 55% 34%)`, color: '#fff' }}
      data-stock-logo={symbol}
      data-logo-kind="fallback"
      aria-hidden="true"
    >
      {fallbackText(symbol)}
    </span>
  )
})
