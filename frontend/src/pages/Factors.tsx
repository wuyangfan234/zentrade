import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { BookmarkCheck, Combine, Compass, LibrarySquare, PenLine, Pickaxe, Sigma } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { FactorComposite } from './factors/FactorComposite'
import { FactorDiscover } from './factors/FactorDiscover'
import { FactorEditor } from './factors/FactorEditor'
import { FactorLibrary } from './factors/FactorLibrary'
// 0.47.2: 检验 / 挖掘 / 候选方案三块带 echarts 图表 (约 340KB gzip), 按需加载, 默认的「发现」页不再等它们
const FactorDiscovery = lazy(() => import('./backtest/FactorDiscovery').then(m => ({ default: m.FactorDiscovery })))
const MiningWorkbench = lazy(() => import('./backtest/MiningWorkbench').then(m => ({ default: m.MiningWorkbench })))
const ResearchCandidatesDialog = lazy(() => import('./backtest/ResearchCandidatesDialog').then(m => ({ default: m.ResearchCandidatesDialog })))
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type Tab = 'discover' | 'inspect' | 'library' | 'editor' | 'composite' | 'mining'

// 0.47.0: 「发现」做因子页首页 (默认页签), 原 5 个页签与地址不变
const TABS: Record<Tab, { title: string; icon: typeof Sigma }> = {
  discover: { title: '发现', icon: Compass },
  inspect: { title: '检验', icon: Sigma },
  library: { title: '因子库', icon: LibrarySquare },
  editor: { title: '编辑器', icon: PenLine },
  composite: { title: '组合', icon: Combine },
  mining: { title: '挖掘', icon: Pickaxe },
}

const TAB_SUBTITLE: Record<Tab, string> = {
  discover: '采集 · 挖掘 · 回测 · 决策',
  inspect: '单因子 · 批量检验',
  library: '因子目录',
  editor: '公式 · 校验 · 试算',
  composite: '复合因子',
  mining: '因子与策略挖掘',
}

export function Factors() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [candidatesOpen, setCandidatesOpen] = useState(false)
  const navigate = useNavigate()

  const requestedTab = searchParams.get('tab')
  const activeTab: Tab = requestedTab === 'inspect' || requestedTab === 'library' || requestedTab === 'editor' || requestedTab === 'composite' || requestedTab === 'mining'
    ? requestedTab
    : (searchParams.get('focus') ? 'inspect' : 'discover')
  // 发现角标 = 待审候选策略 + 待审采集因子 (与发现页流程条第 6 格同一个数)
  const lab = useQuery({ queryKey: QK.factorLabPage, queryFn: () => api.factorLabPage(), staleTime: 60_000 })
  const pending = (() => {
    const p = lab.data
    if (!p) return 0
    const w = p.week
    const cands = w ? w.candidates.filter(c => !c.error && !p.decisions[`${w.week}|${c.signature}`]).length : 0
    return cands + (p.intake?.funnel.pending ?? 0)
  })()
  const focusFactor = activeTab === 'inspect' ? searchParams.get('focus') ?? '' : ''

  const changeTab = (tab: Tab) => {
    // 点当前页签无副作用: 旧实现会清掉 edit 参数, 编辑器按 key 重挂载, 未保存的公式全部丢失
    if (tab === activeTab) return
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
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
                {(Object.keys(TABS) as Tab[]).map(tab => {
                  const mode = TABS[tab]
                  const Icon = mode.icon
                  const active = activeTab === tab
                  return (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => changeTab(tab)}
                      aria-current={active ? 'page' : undefined}
                      className={`inline-flex h-7 items-center gap-1 rounded-[5px] px-1.5 text-[11px] font-medium transition-colors sm:gap-1.5 sm:px-2.5 sm:text-xs ${active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:bg-elevated hover:text-foreground'
                      }`}
                    >
                      <Icon className="hidden h-3.5 w-3.5 sm:block" />
                      {mode.title}
                      {tab === 'discover' && pending > 0 && (
                        <span data-tab-badge="" className={`rounded-full px-1.5 text-[10.5px] font-semibold tabular-nums ${active ? 'bg-black/30 text-white' : 'bg-elevated text-foreground'}`}>{pending}</span>
                      )}
                    </button>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="min-h-0 min-w-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4 xl:overflow-hidden">
        <Suspense fallback={<div className="h-full min-h-[240px]" />}>
        {activeTab === 'discover' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <FactorDiscover onGo={tab => changeTab(tab)} />
          </div>
        )}
        {activeTab === 'inspect' && <FactorDiscovery focusFactor={focusFactor} />}
        {activeTab === 'editor' && <FactorEditor key={searchParams.get('edit') ?? ''} editId={searchParams.get('edit') ?? ''} />}
        {activeTab === 'composite' && <FactorComposite />}
        {activeTab === 'mining' && (
          <div className="h-full min-h-0 xl:overflow-y-auto">
            <MiningWorkbench />
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
