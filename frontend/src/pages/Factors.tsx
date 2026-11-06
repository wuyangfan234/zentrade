import { lazy, Suspense, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { BookmarkCheck, Combine, Compass, Grid3x3, LayoutGrid, LibrarySquare, PenLine, Pickaxe, Sigma, Wrench } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { FactorComposite } from './factors/FactorComposite'
import { FactorDiscover } from './factors/FactorDiscover'
import { FactorEditor } from './factors/FactorEditor'
import { FactorLibrary } from './factors/FactorLibrary'
import { FactorMatrix, FactorSlots, pendingCount } from './factors/FactorSlots'
// 0.47.2: 检验 / 挖掘 / 候选方案三块带 echarts 图表 (约 340KB gzip), 按需加载, 默认的「发现」页不再等它们
const FactorDiscovery = lazy(() => import('./backtest/FactorDiscovery').then(m => ({ default: m.FactorDiscovery })))
const MiningWorkbench = lazy(() => import('./backtest/MiningWorkbench').then(m => ({ default: m.MiningWorkbench })))
const ResearchCandidatesDialog = lazy(() => import('./backtest/ResearchCandidatesDialog').then(m => ({ default: m.ResearchCandidatesDialog })))
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type Tab = 'slots' | 'matrix' | 'discover' | 'library' | ToolTab
type ToolTab = 'inspect' | 'editor' | 'composite' | 'mining'
type NavKey = 'slots' | 'matrix' | 'discover' | 'library' | 'tools'

// 0.54.0 C 方案: 首页 = 按用途看因子 (槽位), 第二视图 = 依赖矩阵; 原「发现」改名「进件」,
// 检验 / 编辑器 / 组合 / 挖掘收进「工具」。旧地址 ?tab=discover|inspect|library|editor|composite|mining 照旧可用。
const NAV: { key: NavKey; title: string; icon: typeof Sigma }[] = [
  { key: 'slots', title: '槽位', icon: LayoutGrid },
  { key: 'matrix', title: '依赖矩阵', icon: Grid3x3 },
  { key: 'discover', title: '进件', icon: Compass },
  { key: 'library', title: '因子库', icon: LibrarySquare },
  { key: 'tools', title: '工具', icon: Wrench },
]
const TOOLS: { key: ToolTab; title: string; icon: typeof Sigma }[] = [
  { key: 'inspect', title: '检验', icon: Sigma },
  { key: 'editor', title: '编辑器', icon: PenLine },
  { key: 'composite', title: '组合', icon: Combine },
  { key: 'mining', title: '挖掘', icon: Pickaxe },
]
const isTool = (t: string | null): t is ToolTab => t === 'inspect' || t === 'editor' || t === 'composite' || t === 'mining'

const TAB_SUBTITLE: Record<Tab, string> = {
  slots: '在用 vs 挑战 · 每个槽位一把尺子',
  matrix: '打分因子 × 策略族',
  discover: '采集 · 投递 · 周度挖掘',
  library: '因子目录',
  inspect: '单因子 · 批量检验',
  editor: '公式 · 校验 · 试算',
  composite: '复合因子',
  mining: '因子与策略挖掘',
}

export function Factors() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [candidatesOpen, setCandidatesOpen] = useState(false)
  const navigate = useNavigate()

  const requestedTab = searchParams.get('tab')
  const activeTab: Tab = requestedTab === 'matrix' || requestedTab === 'discover' || requestedTab === 'library' || isTool(requestedTab)
    ? requestedTab
    : (searchParams.get('focus') ? 'inspect' : 'slots')
  const activeNav: NavKey = isTool(activeTab) ? 'tools' : activeTab
  // 进件角标 = 待审候选策略 + 待审采集因子 (与进件页流程条第 6 格同一个数)
  const lab = useQuery({ queryKey: QK.factorLabPage, queryFn: () => api.factorLabPage(), staleTime: 60_000 })
  const pending = pendingCount(lab.data)
  const focusFactor = activeTab === 'inspect' ? searchParams.get('focus') ?? '' : ''

  const lastTool = useRef<ToolTab>('inspect')
  if (isTool(activeTab)) lastTool.current = activeTab
  const changeTab = (tab: Tab) => {
    // 点当前页签无副作用: 旧实现会清掉 edit 参数, 编辑器按 key 重挂载, 未保存的公式全部丢失
    if (tab === activeTab) return
    const next = new URLSearchParams(searchParams)
    if (tab === 'slots') next.delete('tab')
    else next.set('tab', tab)
    next.delete('focus') // 切 tab 清除单因子聚焦
    next.delete('edit') // 切 tab 清除编辑器载入目标
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="flex min-h-full flex-col bg-base xl:h-full">
      <PageHeader
        title="因子"
        subtitle={<span className="hidden md:inline">{TAB_SUBTITLE[activeTab]}</span>}
        className="shrink-0 flex-wrap gap-x-4 gap-y-2 bg-base/95 px-3 lg:flex-nowrap lg:px-5"
        right={(
          <div className="flex w-full min-w-0 items-center gap-1.5 sm:gap-2 lg:w-auto">
            <button
              type="button"
              onClick={() => setCandidatesOpen(true)}
              aria-label="打开候选方案"
              title="候选方案"
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent sm:px-2.5 sm:text-xs"
            >
              <BookmarkCheck className="h-3.5 w-3.5" />
              <span>候选方案</span>
            </button>
            <span className="h-5 w-px shrink-0 bg-border" aria-hidden="true" />
            <nav className="min-w-0 flex-1 overflow-x-auto lg:flex-none" aria-label="因子视图">
              <div className="inline-flex min-w-max items-center gap-0.5 rounded-btn border border-border bg-surface/80 p-0.5">
                {NAV.map(item => {
                  const Icon = item.icon
                  const active = activeNav === item.key
                  return (
                    <span key={item.key} className="contents">
                      {item.key === 'discover' && <span className="mx-0.5 h-4 w-px shrink-0 bg-border" aria-hidden="true" />}
                      <button
                        type="button"
                        data-nav={item.key}
                        onClick={() => changeTab(item.key === 'tools' ? lastTool.current : item.key)}
                        aria-current={active ? 'page' : undefined}
                        className={`inline-flex h-7 items-center gap-1 rounded-[5px] px-1.5 text-[11px] font-medium transition-colors sm:gap-1.5 sm:px-2.5 sm:text-xs ${active
                          ? 'bg-accent text-white shadow-sm'
                          : 'text-secondary hover:bg-elevated hover:text-foreground'
                        }`}
                      >
                        <Icon className="hidden h-3.5 w-3.5 sm:block" />
                        {item.title}
                        {item.key === 'discover' && pending > 0 && (
                          <span data-tab-badge="" className={`rounded-full px-1.5 text-[10.5px] font-semibold tabular-nums ${active ? 'bg-black/30 text-white' : 'bg-elevated text-foreground'}`}>{pending}</span>
                        )}
                      </button>
                    </span>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="min-h-0 min-w-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4 xl:overflow-hidden">
        <Suspense fallback={<div className="h-full min-h-[240px]" />}>
        {activeNav === 'tools' && (
          <div className="flex h-full min-h-0 flex-col">
          <div data-tools="" className="mb-3 inline-flex self-start items-center gap-0.5 rounded-btn border border-border bg-surface p-0.5" role="tablist" aria-label="工具">
            {TOOLS.map(t => {
              const Icon = t.icon
              const on = activeTab === t.key
              return (
                <button key={t.key} type="button" role="tab" aria-selected={on} data-tool={t.key} onClick={() => changeTab(t.key)}
                  className={`inline-flex h-7 items-center gap-1.5 rounded-[5px] px-2.5 text-xs font-medium transition-colors ${on ? 'bg-accent/15 text-foreground shadow-[inset_0_-2px_0_hsl(var(--accent))]' : 'text-secondary hover:bg-elevated hover:text-foreground'}`}>
                  <Icon className="h-3.5 w-3.5" />{t.title}
                </button>
              )
            })}
          </div>
          <div className="min-h-0 flex-1">
            {activeTab === 'inspect' && <FactorDiscovery focusFactor={focusFactor} />}
            {activeTab === 'editor' && <FactorEditor key={searchParams.get('edit') ?? ''} editId={searchParams.get('edit') ?? ''} />}
            {activeTab === 'composite' && <FactorComposite />}
            {activeTab === 'mining' && (
              <div className="h-full min-h-0 xl:overflow-y-auto">
                <MiningWorkbench />
              </div>
            )}
          </div>
          </div>
        )}
        {activeTab === 'slots' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <FactorSlots
              onInspect={factorId => {
                const next = new URLSearchParams(searchParams)
                next.set('tab', 'inspect')
                next.set('focus', factorId)
                setSearchParams(next)
              }}
              onOpenReview={() => changeTab('discover')}
            />
          </div>
        )}
        {activeTab === 'matrix' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <FactorMatrix />
          </div>
        )}
        {activeTab === 'discover' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <FactorDiscover onGo={tab => changeTab(tab)} />
          </div>
        )}
        {activeTab === 'library' && (
          <FactorLibrary
            onInspect={factorId => {
              const next = new URLSearchParams(searchParams)
              next.set('tab', 'inspect')
              next.set('focus', factorId)
              setSearchParams(next)
            }}
            onEdit={factorId => {
              const next = new URLSearchParams(searchParams)
              next.set('tab', 'editor')
              next.set('edit', factorId)
              setSearchParams(next)
            }}
          />
        )}
        </Suspense>
      </main>

      {candidatesOpen && (
        <Suspense fallback={null}>
          <ResearchCandidatesDialog
            onClose={() => setCandidatesOpen(false)}
            onLoadStrategy={candidate => {
              setCandidatesOpen(false)
              // 跨页「载入复测」: 候选经 router state 交给回测策略页消费
              navigate('/backtest?tab=strategy', { state: { loadCandidate: candidate } })
            }}
          />
        </Suspense>
      )}
    </div>
  )
}
