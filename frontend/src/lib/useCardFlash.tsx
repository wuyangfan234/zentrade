import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'

/**
 * 设置卡片定位闪烁锚点。
 *
 * 用法: /settings?tab=monitoring&highlight=<key> 打开时, 声明了 anchor=key 的
 * 卡片滚动到视口中央并 ring 高亮 2 秒 — 从其他页面引导用户"去某张卡片开启/配置"
 * 时精确定位, 不再让用户在设置页里大海捞针。
 *
 * 每次导航只触发一次 (按 location.key), 避免 StrictMode 双渲染重复闪烁; 再次点击同一链接会重新定位。
 */
export function useCardFlash(highlight: string | undefined, key: string, contentKey: string = '') {
  const ref = useRef<HTMLDivElement>(null)
  // 按导航记录是否已触发: 同一次导航只闪一次 (StrictMode 双渲染), 再次点击同一链接 (新的 location.key) 会重新定位
  const firedKeyRef = useRef<string | null>(null)
  const [flash, setFlash] = useState(false)
  const navKey = useLocation().key

  useEffect(() => {
    const stamp = `${navKey}|${contentKey}`
    if (highlight !== key || firedKeyRef.current === stamp || !ref.current) return
    firedKeyRef.current = stamp
    const el = ref.current
    // 目标内容可能刚切换 (如 ?source= 换了数据源详情): 等两帧布局稳定后再滚动
    requestAnimationFrame(() => requestAnimationFrame(() => el.scrollIntoView({ behavior: 'smooth', block: 'center' })))
    // 等滚动起一帧再点亮, 视觉上"滚过去 → 闪一下"更连贯
    requestAnimationFrame(() => {
      setFlash(true)
      window.setTimeout(() => setFlash(false), 2000)
    })
  }, [highlight, key, navKey, contentKey])

  return { ref, flash }
}

/** 闪烁态样式 */
export function cardFlashCls(flash: boolean): string {
  return `rounded-card transition-all duration-500 ${
    flash ? 'ring-2 ring-accent/60 ring-offset-2 ring-offset-base scale-[1.01]' : 'ring-0 ring-transparent'
  }`
}

/** 锚点包裹层: 面板没有统一 Card 组件时, 直接包住目标区块即可 */
export function AnchorWrap({
  highlight,
  anchor,
  contentKey,
  children,
}: {
  highlight?: string
  anchor: string
  /** 包裹内容切换时 (如选中的数据源变了) 重新定位一次 */
  contentKey?: string
  children: React.ReactNode
}) {
  const { ref, flash } = useCardFlash(highlight, anchor, contentKey)
  return (
    <div ref={ref} id={anchor} className={cardFlashCls(flash)}>
      {children}
    </div>
  )
}
