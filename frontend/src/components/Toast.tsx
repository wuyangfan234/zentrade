import { useCallback, useEffect, useState } from 'react'

// ===== 全局 toast 状态 =====
/** 「已保存 / 已完成」类提示带的下一步: 点了直接落到目标页 (界面规则: 只给状态词等于没给路) */
export type ToastAction = { label: string; onClick: () => void }
type ToastItem = { id: number; msg: string; kind: 'error' | 'success'; action?: ToastAction }
let _id = 0
const _listeners: Set<(items: ToastItem[]) => void> = new Set()
let _queue: ToastItem[] = []
/** 普通提示 4 秒; 带下一步按钮的留 8 秒, 给出点按的时间 */
const TOAST_MS = 4000
const ACTION_TOAST_MS = 8000

function _emit() { _listeners.forEach(fn => fn([..._queue])) }

function _dismiss(id: number) {
  _queue = _queue.filter(t => t.id !== id)
  _emit()
}

function toast(msg: string, kind: 'error' | 'success' = 'error', options?: { action?: ToastAction }) {
  const item: ToastItem = { id: ++_id, msg, kind, action: options?.action }
  _queue = [..._queue, item]
  _emit()
  setTimeout(() => _dismiss(item.id), item.action ? ACTION_TOAST_MS : TOAST_MS)
}

export { toast }

// ===== Toast 容器 — 挂在 Layout 最顶层 =====
export function ToastContainer() {
  const [items, setItems] = useState<ToastItem[]>([])

  const sub = useCallback(() => {
    _listeners.add(setItems)
    return () => { _listeners.delete(setItems) }
  }, [])

  useEffect(sub, [sub])

  if (!items.length) return null

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 right-4 z-[9999] flex flex-col gap-2 pointer-events-none"
    >
      {items.map(t => (
        <div
          key={t.id}
          className={`pointer-events-auto ${t.action ? 'flex items-center gap-3 ' : ''}px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium animate-in slide-in-from-bottom-2 fade-in duration-200 ${
            t.kind === 'error'
              ? 'bg-red-500/90 text-white'
              : 'bg-emerald-500/90 text-white'
          }`}
        >
          {t.action ? (
            <>
              <span>{t.msg}</span>
              {/* 白底深色字: 白字压在 emerald-500 / red-500 上只有约 2.3:1, 按钮单独做到 AA */}
              <button
                type="button"
                data-toast-action=""
                onClick={() => { _dismiss(t.id); t.action!.onClick() }}
                className={`shrink-0 rounded-btn bg-white px-2 py-0.5 text-xs font-semibold transition-colors hover:bg-white/90 ${
                  t.kind === 'error' ? 'text-red-700' : 'text-emerald-800'
                }`}
              >
                {t.action.label}
              </button>
            </>
          ) : t.msg}
        </div>
      ))}
    </div>
  )
}
