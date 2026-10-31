// 0.43.2: 回测/因子检验的结束日只能落在「最近一个已定版的日K」上。
// 美东盘中今天的日K分区是每 ~10 秒整份重写的实时快照, 结束日默认今天会读到它: 回测报「指标数据正在发布更新」,
// 年化等统计出不来; 侥幸读到也是半天的价格。后端 /data/status 给出 settled_date (不含盘中快照), 前端以它为默认与上限。
import { useEffect } from 'react'
import { useDataStatus } from './useSharedQueries'

/** 最近一个已定版的日K日期; 数据状态未就绪或后端未提供时为 null (调用方回退到原来的“今天”)。 */
export function useSettledDate(assetType: 'stock' | 'etf' = 'stock'): string | null {
  const status = useDataStatus()
  const table = assetType === 'etf' ? status.data?.etf_enriched : status.data?.enriched
  return table?.settled_date ?? null
}

/** 结束日为空或晚于定版日 (默认今天 / 上次保存的今天 / 载入的旧配置) 时回落到定版日; 更早的结束日不动。 */
export function useClampEndToSettled(end: string, setEnd: (value: string) => void, settled: string | null) {
  useEffect(() => {
    if (settled && (end === '' || end > settled)) setEnd(settled)
  }, [end, settled, setEnd])
}
