import { type LucideIcon, Construction } from 'lucide-react'

interface Props {
  icon?: LucideIcon
  /** 「暂无 X」 */
  title: string
  /** 至多一行短注, 不写操作教程 */
  hint?: string
  /** 至多一个按钮: 下一步操作 */
  action?: { label: string; onClick: () => void }
}

// 空状态统一写法: 图标 +「暂无 X」+ 至多一行短注 + 至多一个按钮
export function EmptyState({ icon: Icon = Construction, title, hint, action }: Props) {
  return (
    <div className="h-full grid place-items-center px-8 py-16">
      <div className="text-center max-w-md">
        <Icon className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} />
        <h2 className="mt-4 text-[14px] leading-6 font-medium text-foreground">{title}</h2>
        {hint && <p className="mt-1.5 text-xs text-secondary leading-relaxed">{hint}</p>}
        {action && (
          <button type="button" data-empty-action onClick={action.onClick}
            className="mt-4 inline-flex h-8 items-center rounded-btn border border-border px-3 text-xs font-medium text-foreground hover:bg-elevated">
            {action.label}
          </button>
        )}
      </div>
    </div>
  )
}
