import { X, RotateCcw, Filter } from 'lucide-react'

// ===== 筛选类型 =====

export interface ScreenerFilter {
  priceMin: string
  priceMax: string
  changePctMin: string
  changePctMax: string
  momentum5dMin: string
  momentum5dMax: string
  amountMin: string      // 成交额最小(亿)
  volRatioMin: string    // 量比最小
  rsiMin: string
  rsiMax: string
}

export const defaultFilter: ScreenerFilter = {
  priceMin: '', priceMax: '',
  changePctMin: '', changePctMax: '',
  momentum5dMin: '', momentum5dMax: '',
  amountMin: '',
  volRatioMin: '',
  rsiMin: '', rsiMax: '',
}

export function filterActive(f: ScreenerFilter): boolean {
  // 只识别仍可编辑的过滤条件，旧本地状态中的已退役字段不能幽灵激活。
  return countActiveFilters(f) > 0
}

export function countActiveFilters(f: ScreenerFilter): number {
  let n = 0
  if (f.priceMin || f.priceMax) n++
  if (f.changePctMin || f.changePctMax) n++
  if (f.momentum5dMin || f.momentum5dMax) n++
  if (f.amountMin) n++
  if (f.volRatioMin) n++
  if (f.rsiMin || f.rsiMax) n++
  return n
}

export function applyFilter(rows: any[], f: ScreenerFilter): any[] {
  if (!filterActive(f)) return rows
  const num = (v: string) => v === '' ? null : Number(v)
  return rows.filter((r) => {
    const close = Number(r.close ?? 0)
    const v = (field: string) => num(field)
    // 现价
    if (v(f.priceMin) != null && close < v(f.priceMin)!) return false
    if (v(f.priceMax) != null && close > v(f.priceMax)!) return false
    // 涨跌幅(%)
    const chg = (r.change_pct ?? 0) * 100
    if (v(f.changePctMin) != null && chg < v(f.changePctMin)!) return false
    if (v(f.changePctMax) != null && chg > v(f.changePctMax)!) return false
    // 5日涨幅(%)
    const m5 = (r.momentum_5d ?? 0) * 100
    if (v(f.momentum5dMin) != null && m5 < v(f.momentum5dMin)!) return false
    if (v(f.momentum5dMax) != null && m5 > v(f.momentum5dMax)!) return false
    // 成交额(亿)
    const amount = (r.amount ?? 0) / 1e8
    if (v(f.amountMin) != null && amount < v(f.amountMin)!) return false
    // 量比
    if (v(f.volRatioMin) != null && (r.vol_ratio_5d ?? 0) < v(f.volRatioMin)!) return false
    // RSI
    const rsi = r.rsi_14 ?? 0
    if (v(f.rsiMin) != null && rsi < v(f.rsiMin)!) return false
    if (v(f.rsiMax) != null && rsi > v(f.rsiMax)!) return false
    return true
  })
}

// ===== 筛选面板 =====

export function FilterPanel({ value, onChange, onClose, onReset }: {
  value: ScreenerFilter
  onChange: (f: ScreenerFilter) => void
  onClose: () => void
  onReset: () => void
}) {
  const set = (key: keyof ScreenerFilter, v: string) => onChange({ ...value, [key]: v })

  // 数值字段只引用 string 类型的 key
  type NumKey = keyof Pick<ScreenerFilter,
    'priceMin' | 'priceMax' | 'changePctMin' | 'changePctMax' |
    'momentum5dMin' | 'momentum5dMax' | 'amountMin' |
    'volRatioMin' | 'rsiMin' | 'rsiMax'>
  const fields: { label: string; min: NumKey; max: NumKey; unit: string; step?: string }[] = [
    { label: '现价',      min: 'priceMin',      max: 'priceMax',      unit: 'USD', step: '0.1' },
    { label: '涨跌幅',    min: 'changePctMin',   max: 'changePctMax',  unit: '%' },
    { label: '5日涨幅',   min: 'momentum5dMin',  max: 'momentum5dMax', unit: '%' },
    { label: '成交额',    min: 'amountMin',      max: 'amountMin',     unit: '亿', step: '0.5' },
    { label: '量比',      min: 'volRatioMin',    max: 'volRatioMin',   unit: '', step: '0.1' },
    { label: 'RSI14',     min: 'rsiMin',         max: 'rsiMax',        unit: '', step: '1' },
  ]

  return (
    <div className="rounded-card border border-accent/30 bg-accent/[0.03] p-4 space-y-3">
      {/* 标题栏: 左侧标题 + 激活计数, 右侧重置 + 关闭 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Filter className="h-3.5 w-3.5 text-accent" />
          <span className="text-xs font-medium text-accent">筛选条件</span>
          {filterActive(value) && (
            <span className="bg-accent/15 text-accent rounded-full px-1.5 h-4 inline-flex items-center text-[10px] font-bold leading-none">
              {countActiveFilters(value)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {filterActive(value) && (
            <button
              onClick={onReset}
              title="清空全部筛选"
              className="inline-flex items-center gap-1 px-1.5 h-6 rounded text-[11px]
                text-muted hover:text-danger hover:bg-danger/10 transition-colors"
            >
              <RotateCcw className="h-3 w-3" />
              清空
            </button>
          )}
          <button
            onClick={onClose}
            title="收起筛选"
            className="p-1 rounded text-secondary hover:text-foreground hover:bg-elevated transition-colors"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>


      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2.5">
        {fields.map((f) => {
          const isRange = f.min !== f.max
          return (
            <div key={f.label} className="flex items-center gap-1.5">
              <span className="text-[11px] text-secondary shrink-0 w-14 text-right">{f.label}</span>
              <input
                type="number"
                placeholder="最小"
                value={value[f.min]}
                onChange={(e) => set(f.min, e.target.value)}
                step={f.step}
                className="w-16 px-1.5 py-1 rounded-btn bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
              />
              {isRange && (
                <>
                  <span className="text-[10px] text-muted">~</span>
                  <input
                    type="number"
                    placeholder="最大"
                    value={value[f.max]}
                    onChange={(e) => set(f.max, e.target.value)}
                    step={f.step}
                    className="w-16 px-1.5 py-1 rounded-btn bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
                  />
                </>
              )}
              {f.unit && <span className="text-[10px] text-muted shrink-0">{f.unit}</span>}
            </div>
          )
        })}
      </div>
      <div className="text-[10px] text-muted pl-1">输入即生效 · 点击市场/ST 按钮切换</div>
    </div>
  )
}
