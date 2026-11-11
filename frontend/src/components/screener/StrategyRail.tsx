import { badge } from '@/components/ui/badge'
import {
  RAIL_GROUP_LABEL,
  fmtR,
  selectedLabel,
  type RailFilter,
  type RailGroup,
  type RailRow,
} from '@/lib/strategyPanel'

interface Props {
  /** 已按 入选 → 扫描 / 监控 → 未启用 排好序, 且已按筛选过滤 */
  rows: RailRow[]
  filter: RailFilter
  counts: Record<RailFilter, number>
  onFilter: (f: RailFilter) => void
  activeId: string | null
  onSelect: (id: string) => void
  /** 全市场命中数 (没有 = 未扫描) */
  marketHits: Record<string, number | undefined>
  /** 正在扫描的策略 */
  computing: Set<string>
  loading?: boolean
}

const FILTERS: [RailFilter, string][] = [['selected', '入选'], ['active', '已启用'], ['all', '全部']]
const GROUPS: RailGroup[] = ['selected', 'active', 'idle']

// 选中行有淡蓝底: 标签用次级文字色, 否则亮色主题下对比度不够
function Cell({ label, on, children }: { label: string; on: boolean; children: React.ReactNode }) {
  return (
    <span className="min-w-0 truncate">
      <span className={`mr-1 ${on ? 'text-secondary' : 'text-muted'}`}>{label}</span>
      <span className={on ? 'text-foreground' : undefined}>{children}</span>
    </span>
  )
}

/**
 * 策略列表: 一个策略一行。桌面是左侧竖列 (自带滚动), 手机是顶部横滑的一排名字。
 * 行内四个数: 自选命中 / 全市场命中 / 回放超额 / 模拟每笔; 只读展示, 点击切换右侧详情。
 * 行内数字不着色 (选中行有淡蓝底, 红绿小字压上去对比度不够); 涨跌色留给详情。
 */
export function StrategyRail({ rows, filter, counts, onFilter, activeId, onSelect, marketHits, computing, loading }: Props) {
  return (
    <aside data-strategy-rail className="min-w-0 rounded-card border border-border bg-surface md:sticky md:top-3">
      <div className="border-b border-border/60 p-2">
        <div role="tablist" aria-label="策略范围" className="flex h-7 overflow-hidden rounded-btn border border-border">
          {FILTERS.map(([f, label]) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              data-rail-filter={f}
              onClick={() => onFilter(f)}
              className={`h-full flex-1 whitespace-nowrap border-r border-border px-2 text-xs font-medium transition-colors last:border-r-0 cursor-pointer
                ${filter === f ? 'bg-accent/10 text-accent dark:text-blue-400' : 'text-secondary hover:bg-elevated hover:text-foreground'}`}
            >
              {label}
              <span className={`ml-1 tabular-nums ${filter === f ? 'text-secondary' : 'text-muted'}`}>{counts[f]}</span>
            </button>
          ))}
        </div>
      </div>
      <div
        data-rail-list
        className="flex gap-1.5 overflow-x-auto p-2 md:block md:max-h-[calc(100vh-152px)] md:space-y-0 md:overflow-y-auto md:overflow-x-hidden md:p-0 md:pb-1"
      >
        {loading && rows.length === 0 && <div className="px-3 py-3 text-xs text-muted">加载中</div>}
        {!loading && rows.length === 0 && <div data-rail-empty className="px-3 py-3 text-xs text-muted">暂无策略</div>}
        {GROUPS.map(g => {
          const items = rows.filter(r => r.group === g)
          if (!items.length) return null
          return (
            <div key={g} data-rail-group={g} className="contents md:block">
              <div className="hidden px-3 pb-1 pt-2 text-[11px] font-medium text-muted md:block">
                {RAIL_GROUP_LABEL[g]}
                <span className="ml-1.5 tabular-nums">{items.length}</span>
              </div>
              {items.map(r => {
                const on = r.id === activeId
                const mkt = marketHits[r.id]
                return (
                  <button
                    key={r.id}
                    type="button"
                    data-rail-item={r.id}
                    aria-current={on ? 'true' : undefined}
                    onClick={() => onSelect(r.id)}
                    className={`shrink-0 rounded-btn border px-2.5 py-1.5 text-left transition-colors cursor-pointer
                      md:block md:w-full md:rounded-none md:border-0 md:border-l-[3px] md:px-3 md:py-2
                      ${on
                        ? 'border-accent bg-accent/10 md:border-l-accent'
                        : 'border-border hover:bg-elevated/60 md:border-l-transparent'}`}
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span
                        title={r.name}
                        className={`min-w-0 truncate text-[13px] md:flex-1 ${r.group === 'idle' ? 'text-secondary' : 'font-medium text-foreground'}`}
                      >
                        {r.name}
                      </span>
                      {r.diverged && <span data-rail-diverged className={badge('warn')} title="模拟成绩的区间不含回放值">背离</span>}
                      {r.selected.length > 0 && (
                        <span data-rail-selected className={badge('status')} title="入选策略 · 在机会页生成机会">{selectedLabel(r.selected)}</span>
                      )}
                    </span>
                    <span className="mt-1 hidden grid-cols-[minmax(0,0.8fr)_minmax(0,1.05fr)_minmax(0,1.25fr)_minmax(0,1.25fr)] gap-x-1.5 text-[11px] tabular-nums text-secondary md:grid">
                      <Cell label="自选" on={on}>{r.hits}</Cell>
                      <Cell label="全市场" on={on}>
                        {computing.has(r.id)
                          ? <span className="inline-block h-2 w-4 animate-pulse rounded bg-elevated align-middle" />
                          : mkt == null ? '—' : mkt}
                      </Cell>
                      <Cell label="超额" on={on}>{fmtR(r.excess)}</Cell>
                      <Cell label="模拟" on={on}>{fmtR(r.paperMean)}</Cell>
                    </span>
                  </button>
                )
              })}
            </div>
          )
        })}
      </div>
    </aside>
  )
}
