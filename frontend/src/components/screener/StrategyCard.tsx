import { Settings2, TrendingDown, RadioTower } from 'lucide-react'
import { motion } from 'framer-motion'
import { storage } from '@/lib/storage'

// ===== 卡片尺寸 =====

export type CardSize = 'mini' | 'normal' | 'large' | 'hidden'

export function loadCardSize(): CardSize {
  const v = storage.screenerCardSize.get('normal')
  if (v === 'mini' || v === 'normal' || v === 'large' || v === 'hidden') return v
  return 'normal'
}

const CARD_STYLES: Record<CardSize, {
  wrap: string
  card: string
  name: string
  count: string
  desc: string
  icon: string
}> = {
  mini: {
    wrap: 'flex flex-wrap gap-1',
    card: 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full',
    name: 'text-[10px]',
    count: 'text-[11px]',
    desc: '',
    icon: 'h-3 w-3',
  },
  // 标准/详细: 等宽网格 + 卡片铺满格子, 宽度不再随策略名/描述长度变化;
  // 描述截断 (完整文字在 title 悬浮提示, 设置弹窗里也可见), 描述/数字行固定占位, 同档卡片等高。
  // 手机 (<640px) 单列整宽, 与原先 flex-wrap 在窄屏上每行一张的密度一致。
  normal: {
    wrap: 'grid grid-cols-1 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(240px,1fr))]',
    card: 'relative flex flex-col w-full min-w-0 px-3 py-1.5 rounded-lg',
    name: 'text-xs',
    count: 'text-xs',
    desc: 'block h-3.5 min-w-0 flex-1 truncate text-[10px] text-muted leading-[14px]',
    icon: 'h-3.5 w-3.5',
  },
  large: {
    wrap: 'grid grid-cols-1 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(260px,1fr))]',
    card: 'relative flex flex-col w-full min-w-0 px-3.5 py-2.5 rounded-btn',
    name: 'text-xs',
    count: 'text-lg font-mono font-bold tabular-nums',
    desc: 'mt-0.5 w-full min-h-[2.5em] line-clamp-2 break-words text-[10px] text-muted leading-tight',
    icon: 'h-3.5 w-3.5',
  },
  hidden: {
    wrap: '',
    card: '',
    name: '',
    count: '',
    desc: '',
    icon: '',
  },
}

export { CARD_STYLES }

/** 卡片容器样式: 紧凑档 flex-wrap 胶囊; 标准/详细档等宽网格 */
export function cardWrapCls(size: CardSize): string {
  return CARD_STYLES[size].wrap
}

// ===== 来源标签 =====

const SRC_MAP: Record<string, string> = { builtin: '内置', custom: '自定义', ai: 'AI', composite: '叠加' }
const BADGE_CLS_MAP: Record<string, string> = {
  builtin: 'bg-secondary/10 text-muted border-border',
  ai: 'bg-purple-500/10 text-purple-700 dark:text-purple-400 border-purple-500/30',
  custom: 'bg-amber-400/10 text-amber-700 dark:text-amber-400 border-amber-400/30',
  composite: 'bg-teal-500/10 text-teal-700 dark:text-teal-400 border-teal-500/30',
}

// ===== 策略卡片 =====

interface StrategyCardProps {
  name: string
  description?: string
  source?: string
  active: boolean
  count?: number
  /** 今日曾命中总数 */
  everMatched?: number
  /** 今日已失效数 (曾命中 - 当前命中) */
  expiredCount?: number
  loading: boolean
  cardSize: CardSize
  onRun: () => void
  disabled: boolean
  onSettings: () => void
  /** 是否已加入策略监控 */
  monitored?: boolean
  /** 切换策略监控 (点击 RadioTower 图标) */
  onToggleMonitor?: () => void
  /** 周期徽章 (如 '分钟'); 日线策略不传 */
  timeframeBadge?: string
  /** 后台计算中 (渐进式 run_all): 数字未出时显示脉冲占位 */
  computing?: boolean
  /** 等待运行 (自动计算关闭/失败时的分钟策略): 数字未出时显示「待计算」点击引导 */
  awaitRun?: boolean
}

export function StrategyCard({
  name, description, source, active, count, expiredCount,
  loading, cardSize,
  onRun, disabled, onSettings, monitored, onToggleMonitor, timeframeBadge, computing, awaitRun,
}: StrategyCardProps) {
  const cs = CARD_STYLES[cardSize]
  const activeCls = active
    ? 'border-accent/50 bg-accent/10 shadow-[0_0_10px_rgba(59,130,246,0.1)]'
    : 'border-border bg-surface hover:border-accent/40 hover:bg-accent/[0.03]'
  const countCls = count === 0
    ? 'text-muted'
    : 'bg-gradient-to-r from-amber-400 to-orange-500 bg-clip-text text-transparent'
  const srcLabel = cardSize === 'mini' ? (SRC_MAP[source ?? ''] ?? '内') : (SRC_MAP[source ?? ''] ?? '内置')
  const badgeCls = BADGE_CLS_MAP[source ?? 'builtin'] ?? BADGE_CLS_MAP.builtin

  // 失效数 > 0 时显示
  const hasExpired = expiredCount != null && expiredCount > 0
  // 名称/描述在卡面上会截断, 完整文字放悬浮提示
  const fullTitle = description ? `${name}\n${description}` : name

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
      data-strategy-card
      className={`${cs.card} border transition-colors duration-150 text-left group ${activeCls}`}
    >
      {cardSize === 'large' ? (
        <>
          <button onClick={onRun} disabled={disabled} title={fullTitle}
            className="flex flex-col items-start cursor-pointer disabled:opacity-50 disabled:cursor-wait w-full min-w-0 text-left">
            <div className="flex items-center gap-1.5 w-full min-w-0 pr-9">
              <span className={`text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 ${badgeCls}`}>{srcLabel}</span>
              {timeframeBadge && (
                <span className="text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400">{timeframeBadge}</span>
              )}
              <span className="text-xs font-medium truncate min-w-0 text-foreground">{name}</span>
            </div>
            <span data-strategy-desc className={cs.desc}>{description}</span>
            <div data-strategy-count-row className="mt-1.5 h-5 flex items-center gap-2">
              {count != null && !loading && (
                <>
                  <div className="flex items-center gap-1">
                    <span className={`text-sm font-mono font-bold tabular-nums ${countCls}`}>{count}</span>
                    <span className="text-[10px] text-muted">只</span>
                  </div>
                  {hasExpired && (
                    <div className="flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-red-500/8 border border-red-500/15">
                      <TrendingDown className="h-2.5 w-2.5 text-red-700 dark:text-red-400" />
                      <span className="text-[10px] font-mono font-medium text-red-700 dark:text-red-400">{expiredCount}</span>
                    </div>
                  )}
                </>
              )}
              {count == null && !loading && computing && (
                <span className="text-sm font-mono font-bold text-muted animate-pulse">···</span>
              )}
              {count == null && !loading && !computing && awaitRun && (
                <span className="text-[10px] text-muted transition-colors group-hover:text-accent" title="自动计算未开启或失败 — 点击卡片实时计算">待计算</span>
              )}
              {loading && <div className="h-4 w-10 rounded bg-elevated animate-pulse" />}
            </div>
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute top-1.5 right-1.5 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute top-1.5 right-7 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`relative h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
        </>
      ) : cardSize === 'normal' ? (
        <>
          <button onClick={onRun} disabled={disabled} title={fullTitle}
            className="flex flex-col items-start cursor-pointer disabled:opacity-50 disabled:cursor-wait w-full min-w-0 text-left">
            <div className="flex items-center gap-1.5 w-full min-w-0 pr-9">
              <span className={`text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 ${badgeCls}`}>{srcLabel}</span>
              {timeframeBadge && (
                <span className="text-[9px] px-1 py-px rounded border font-medium leading-tight shrink-0 border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400">{timeframeBadge}</span>
              )}
              <span className="text-xs font-medium truncate min-w-0 text-foreground">{name}</span>
              {count != null && !loading && (
                <span className={`text-xs font-mono font-bold tabular-nums shrink-0 ${countCls}`}>{count}</span>
              )}
              {count == null && !loading && computing && (
                <span className="text-xs font-mono font-bold text-muted animate-pulse shrink-0">···</span>
              )}
              {count == null && !loading && !computing && awaitRun && (
                <span className="text-[10px] text-muted transition-colors group-hover:text-accent shrink-0" title="自动计算未开启或失败 — 点击卡片实时计算">待计算</span>
              )}
              {loading && <span className="w-5 h-3 rounded bg-elevated animate-pulse shrink-0" />}
            </div>
            <div className="flex items-center gap-1.5 mt-0.5 w-full min-w-0">
              <span data-strategy-desc className={cs.desc}>{description}</span>
              {hasExpired && (
                <span className="text-[9px] font-mono text-red-700 dark:text-red-400/80 shrink-0">{'-' + expiredCount}</span>
              )}
            </div>
          </button>
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="absolute top-1.5 right-1.5 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="absolute top-1.5 right-7 p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`relative h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
        </>
      ) : (
        /* mini */
        <>
          <button onClick={onRun} disabled={disabled}
            className="flex items-center gap-1 cursor-pointer disabled:opacity-50 disabled:cursor-wait">
            <span className="text-[8px] px-0.5 rounded bg-secondary/10 text-muted border border-border font-medium leading-tight">{srcLabel}</span>
            <span className="text-[10px] font-medium whitespace-nowrap text-foreground">{name}</span>
            {count != null && !loading && (
              <span className={`text-xs font-mono font-bold tabular-nums ${countCls}`}>{count}</span>
            )}
            {count == null && !loading && computing && (
              <span className="text-xs font-mono font-bold text-muted animate-pulse">···</span>
            )}
            {count == null && !loading && !computing && awaitRun && (
              <span className="text-[9px] text-muted transition-colors group-hover:text-accent shrink-0" title="自动计算未开启或失败 — 点击卡片实时计算">待算</span>
            )}
            {hasExpired && (
              <span className="text-[9px] font-mono text-red-700 dark:text-red-400/70">{'-' + expiredCount}</span>
            )}
            {loading && <span className="w-4 h-2.5 rounded bg-elevated animate-pulse" />}
          </button>
          {onToggleMonitor && (
            <button onClick={(e) => { e.stopPropagation(); onToggleMonitor() }}
              className="relative p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title={monitored ? '取消策略监控' : '开启策略监控'}>
              <RadioTower className={`h-3 w-3 transition-colors ${monitored ? 'text-accent' : 'text-muted hover:text-accent'}`} />
              {monitored && <span className="absolute inset-0 rounded animate-ping bg-accent/20" />}
            </button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onSettings() }}
            className="p-0.5 rounded hover:bg-elevated transition-colors cursor-pointer" title="策略设置">
            <Settings2 className="h-3 w-3 text-muted hover:text-accent transition-colors" />
          </button>
        </>
      )}
    </motion.div>
  )
}
