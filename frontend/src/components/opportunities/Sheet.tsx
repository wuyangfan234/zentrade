/** 右侧面板: 遮罩 + 实色底 + 顶栏; 挂到 body, 不与页面内容叠在一起。点遮罩 / Esc 关闭。 */
import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

export function Sheet({ title, onClose, children, attr, footer }: {
  title: ReactNode; onClose: () => void; children: ReactNode; attr: Record<string, string>; footer?: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [onClose])
  return createPortal(
    <div className="fixed inset-0 z-[60]">
      <div data-backdrop className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div {...attr} role="dialog" aria-modal="true"
        className="absolute inset-y-0 right-0 flex w-full max-w-2xl flex-col border-l border-border bg-surface shadow-2xl">
        <div className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))]">
          <div className="min-w-0 text-[16px] font-semibold leading-6 text-foreground">{title}</div>
          <button type="button" aria-label="关闭" onClick={onClose} className="flex h-9 w-9 items-center justify-center rounded-btn text-secondary hover:bg-elevated hover:text-foreground">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">{children}</div>
        {footer && (
          <div data-sheet-footer className="flex shrink-0 items-center gap-2 border-t border-border bg-surface px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] text-xs">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
