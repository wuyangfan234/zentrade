import { isRouteErrorResponse, useNavigate, useRouteError } from 'react-router-dom'

/**
 * 路由级错误兜底 (审计 2026-10-26 H-3)。
 * 页面组件渲染出错时只替换内容区, 导航和布局保留; 切换页面即自动恢复。
 * 此前路由树没有 errorElement, 任一组件抛错整页变成 "Unexpected Application Error"。
 */
export function RouteError({ fullPage = false }: { fullPage?: boolean }) {
  const error = useRouteError()
  const navigate = useNavigate()
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error ? error.message : String(error ?? '')
  return (
    <div role="alert" className={`flex flex-col items-center justify-center gap-3 px-6 text-center ${fullPage ? 'min-h-screen' : 'py-24'}`}>
      <div className="text-sm font-medium text-danger">这个页面出错了</div>
      <div className="max-w-md break-all font-mono text-xs text-muted">{detail}</div>
      <div className="flex gap-2">
        <button type="button" className="rounded border border-border px-3 py-1 text-xs" onClick={() => window.location.reload()}>
          重新加载
        </button>
        <button type="button" className="rounded border border-border px-3 py-1 text-xs" onClick={() => navigate('/')}>
          回机会页
        </button>
      </div>
    </div>
  )
}
