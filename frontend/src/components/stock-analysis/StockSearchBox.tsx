import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface Props {
  onSelect: (symbol: string, name: string) => void
  /** 搜索资产类型, 逗号分隔 (默认 'stock')。 */
  assetTypes?: string
}

/**
 * 个股分析页的股票搜索框 —— 代码 / 英文名模糊匹配 (后端 instrumentSearch, 不区分大小写)。
 * 下拉候选支持方向键 + 回车; 选中即回调并清空输入。
 *
 * 原 StockFinancialSearch 随 P1-5 删财务模块被连带删除, 个股分析页因此失去选股入口;
 * 这里按美股口径重建 (去掉旧市场板块标签与数字代码占位文案)。
 */
export function StockSearchBox({ onSelect, assetTypes = 'stock' }: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const trimmed = query.trim()

  const search = useQuery({
    queryKey: QK.instrumentSearch(trimmed, assetTypes),
    queryFn: () => api.instrumentSearch(trimmed, 20, assetTypes),
    enabled: trimmed.length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []

  // 点击外部关闭下拉
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handleSelect(r: { symbol: string; name: string }) {
    onSelect(r.symbol, r.name)
    setQuery('')
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || results.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      handleSelect(results[activeIdx >= 0 ? activeIdx : 0])
    }
  }

  return (
    <div ref={containerRef} className="relative w-full">
      <div className="relative flex items-center">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        <input
          type="text"
          placeholder="代码或英文名，如 NVDA"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveIdx(-1) }}
          onFocus={() => { if (trimmed) setOpen(true) }}
          onKeyDown={handleKeyDown}
          aria-label="搜索股票"
          className="w-full h-9 pl-9 pr-9 rounded-btn bg-surface border border-border text-sm text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 focus:bg-base transition-colors"
        />
        {search.isFetching && (
          <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted animate-spin" />
        )}
      </div>

      {open && trimmed && (
        <div className="absolute left-0 right-0 top-full mt-1.5 z-50 max-h-[360px] overflow-y-auto rounded-card border border-border bg-base shadow-xl">
          {search.isLoading ? (
            <div className="px-4 py-6 flex items-center justify-center gap-2 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              搜索中…
            </div>
          ) : results.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs text-muted">未找到匹配的股票</div>
          ) : (
            results.map((r, i) => (
              <button
                key={r.symbol}
                type="button"
                onClick={() => handleSelect(r)}
                className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors duration-100 ${
                  i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground'
                }`}
              >
                <span className="font-mono shrink-0 text-xs w-[64px]">{r.symbol}</span>
                <span className="truncate text-sm flex-1">{r.name}</span>
                {r.asset_type === 'etf' && (
                  <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-accent/10 text-accent">ETF</span>
                )}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
