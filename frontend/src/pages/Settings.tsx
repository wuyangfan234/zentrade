/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { BarChart3, Database, Radio, SlidersHorizontal, Sparkles, Settings2, PanelLeftClose, PanelLeftOpen, Clock3, KeyRound } from 'lucide-react'
import { SettingsAIPanel } from './settings/AI'
import { SettingsMonitoringPanel } from './settings/Monitoring'
import { SettingsExtPagesPanel } from './settings/ExtPages'
import { SettingsMenuSettingsPanel } from './settings/MenuSettings'
import { SettingsTimeoutPanel } from './settings/Timeout'
import { SettingsSystemPanel } from './settings/System'
import { SettingsDataSourcesPanel } from './settings/DataSources'
import { SettingsApiAccessPanel } from './settings/ApiAccess'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

import type { ComponentType } from 'react'

// ===== Tab 定义 =====

type TabDef = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  panel: ComponentType<{ highlight?: string }>
  badge?: string
}

const TABS: readonly TabDef[] = [
  { key: 'data-sources', label: '数据源',     icon: Database,  panel: SettingsDataSourcesPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,     panel: SettingsMonitoringPanel },
  { key: 'ext-pages',  label: '扩展页面',   icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'timeout',    label: '网络设置',   icon: Clock3,    panel: SettingsTimeoutPanel },
  { key: 'menus',      label: '菜单设置',   icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'api',        label: 'API 与 MCP', icon: KeyRound,  panel: SettingsApiAccessPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2, panel: SettingsSystemPanel },
]

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as TabKey | null
  const activeTab = TABS.find((t) => t.key === tabParam) ?? TABS[0]
  const highlight = searchParams.get('highlight') ?? ''

  // 设置菜单收起状态 — 持久化到 localStorage
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('tf-settings-nav-collapsed') === '1' } catch { return false }
  })
  const toggleCollapsed = () => {
    setCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem('tf-settings-nav-collapsed', next ? '1' : '0') } catch {}
      return next
    })
  }

  return (
    <>
      <PageHeader
        title="设置"
        subtitle="管理账户、数据刷新策略和高级功能配置。"
      />

      <div className="px-4 py-4 md:px-8 md:py-6">
        <div className="flex flex-col gap-4 md:flex-row md:gap-6 md:items-stretch">
          {/* ===== 竖向 Tab 侧栏 (桌面) / 顶部横滑标签条 (移动端) ===== */}
          <nav className={cn('shrink-0 transition-all duration-200 ease-smooth', collapsed ? 'md:w-10' : 'md:w-36')}>
            <div className="flex flex-row gap-1 overflow-x-auto md:flex-col md:gap-0.5 md:justify-center md:min-h-[60vh] md:sticky md:top-6 md:overflow-visible">
              {/* 收起/展开 按钮 (仅桌面) */}
              <button
                onClick={toggleCollapsed}
                className={cn(
                  'hidden md:flex items-center gap-2 rounded-btn text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth mb-1',
                  collapsed ? 'justify-center px-0 py-2' : 'px-3 py-2 text-xs',
                )}
                title={collapsed ? '展开菜单' : '收起菜单'}
              >
                {collapsed
                  ? <PanelLeftOpen className="h-3.5 w-3.5 shrink-0" />
                  : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
                }
                {!collapsed && <span>收起菜单</span>}
              </button>

              {/* Tab 按钮列表 — 桌面收起时只显示图标; 移动端横向排列不折行 */}
              {TABS.map(({ key, label, icon: Icon, badge }) => (
                <button
                  key={key}
                  onClick={() => setSearchParams({ tab: key }, { replace: true })}
                  title={collapsed ? label : undefined}
                  className={cn(
                    'relative flex shrink-0 items-center whitespace-nowrap rounded-btn text-sm transition-colors duration-150 ease-smooth',
                    collapsed ? 'md:justify-center md:px-0' : 'gap-2 text-left',
                    'px-3 py-2',
                    activeTab.key === key
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-secondary hover:text-foreground hover:bg-elevated/60',
                  )}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  <span className={collapsed ? 'md:hidden' : ''}>{label}</span>
                  {!collapsed && badge && (
                    <span className="ml-auto inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400 shrink-0">
                      {badge}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </nav>

          {/* ===== Tab 内容 ===== */}
          <motion.div
            key={activeTab.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="min-w-0 flex-1"
          >
            {activeTab.key === 'monitoring'
            ? <SettingsMonitoringPanel highlight={highlight} />
            : <activeTab.panel highlight={highlight} />}
          </motion.div>
        </div>
      </div>
    </>
  )
}
