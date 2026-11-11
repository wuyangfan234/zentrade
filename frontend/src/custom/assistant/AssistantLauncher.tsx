/**
 * AI 助手宿主 — 挂载于 layout.navigation.extra 插槽(全站常驻渲染点),
 * 但不在侧栏菜单里渲染任何入口。聚合三件套:
 * ① 可拖动悬浮球(位置持久化); ② 左上角 AI 配置徽标右侧的打开入口
 * (DOM 锚定); ③ 从页面右缘滑入的非模态面板。
 * 另负责全局快捷键 ⌘K/Ctrl+K 与 Esc、按当前路由上报页面上下文。
 */
import { useEffect } from 'react'
import { Sparkles } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'
import type { FrontendSlotContextMap } from '@/extensions/types'
import { closeAssistant, setPageContext, toggleAssistant, useAssistantStore } from './store'
import { AssistantFloatingButton } from './ui/AssistantFloatingButton'
import { AiConfigEntry } from './ui/AiConfigEntry'
import { AssistantDrawer } from './ui/AssistantDrawer'

// 插槽注册表把组件 props 定为所有插槽上下文的联合类型(保守契约);
// 本组件只在 layout.navigation.extra 渲染, 在此收窄到该插槽的上下文。
type NavigationContext = FrontendSlotContextMap['layout.navigation.extra']
type AnySlotContext = FrontendSlotContextMap[keyof FrontendSlotContextMap]

// 与核心侧栏导航文案保持一致的轻量映射(仅用于上下文提示, 展示不走这里)。
const PAGE_LABELS: Record<string, string> = {
  '/': '机会',
  '/watchlist': '自选',
  '/screener': '策略',
  '/factors': '因子',
  '/backtest': '回测',
  '/stock-analysis': '个股分析',
  '/monitor': '监控中心',
  '/lots': '持仓提醒',
  '/paper': '持仓',
  '/signals': '信号库',
  '/data': '数据',
  '/status': '系统状态',
  '/settings': '设置',
}

/** 桌面端入口: 侧栏菜单末尾一项。悬浮球会压住表格最右一列, 桌面端不再使用。 */
function AssistantNavButton({ collapsed }: { collapsed: boolean }) {
  const { open, sending } = useAssistantStore()
  return (
    <button
      type="button"
      data-assistant-nav
      onClick={() => toggleAssistant()}
      title={collapsed ? 'AI 助手 (⌘K / Ctrl+K)' : '⌘K / Ctrl+K'}
      aria-label="AI 助手"
      className={cn(
        'group relative mt-1 flex w-full cursor-pointer items-center rounded-btn text-sm transition-all duration-150 ease-smooth',
        collapsed ? 'justify-center px-0 py-2' : 'gap-3 px-3 py-2',
        open ? 'bg-elevated font-medium text-foreground' : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground',
      )}
    >
      <Sparkles className={cn('h-4 w-4 shrink-0 transition-colors', open ? 'text-accent' : 'text-foreground/60 group-hover:text-foreground/85')} />
      {!collapsed && <span className="flex-1 text-left">AI 助手</span>}
      {!collapsed && <span className="text-[11px] text-muted">⌘K</span>}
      {sending && <span className="absolute right-1.5 top-1.5 h-2 w-2 animate-pulse rounded-full bg-warning" title="生成中" />}
    </button>
  )
}

export function AssistantLauncher(props: AnySlotContext) {
  const { pathname, collapsed } = props as NavigationContext
  const { open } = useAssistantStore()
  const isDesktop = useIsDesktop()

  useEffect(() => {
    setPageContext({ page: PAGE_LABELS[pathname] ?? '' })
  }, [pathname])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        toggleAssistant()
        return
      }
      if (event.key === 'Escape' && open) closeAssistant()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open])

  return (
    <>
      {isDesktop ? <AssistantNavButton collapsed={!!collapsed} /> : <AssistantFloatingButton />}
      <AiConfigEntry />
      <AssistantDrawer />
    </>
  )
}
