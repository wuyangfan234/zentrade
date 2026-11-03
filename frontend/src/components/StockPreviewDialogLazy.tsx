// 个股详情弹窗的按需加载入口 (0.47.2)。
// 详情弹窗静态引入日K / 分时 / 多日分时图表, 会把约 340KB (gzip) 的 echarts 拖进自选、情报、监控等页面的首屏;
// 这些页面只有点开某只票时才需要它。这里在第一次打开时才加载实现, 之后一直挂着 (symbol 变回 null 时
// 由实现内部的 AnimatePresence 播放关闭动画), 用法与 props 和原组件完全相同。
import { lazy, Suspense, useState, type ComponentProps } from 'react'
import type { StockPreviewDialog as StockPreviewDialogImpl } from './StockPreviewDialog'

const Impl = lazy(() => import('./StockPreviewDialog').then(m => ({ default: m.StockPreviewDialog })))

export function StockPreviewDialog(props: ComponentProps<typeof StockPreviewDialogImpl>) {
  const [armed, setArmed] = useState(false)
  if (props.symbol && !armed) setArmed(true)
  if (!armed && !props.symbol) return null
  return (
    <Suspense fallback={null}>
      <Impl {...props} />
    </Suspense>
  )
}
