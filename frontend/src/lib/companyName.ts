/**
 * 美股公司名展示用缩短 (ui-audit-1011): 去掉交易所全称里冗长的证券类别尾巴,
 * 如「NVIDIA Corporation Common Stock」→「NVIDIA Corporation」,
 * 「Alibaba Group Holding Limited American Depositary Shares, each represents …」→「Alibaba Group Holding Limited」。
 * 保留 Class A/B 等股份类别 (区分 GOOGL/GOOG); 优先股/权证/单位等不动 (类别本身是关键信息)。只影响显示, 完整名放 title。
 */
const TAIL = /\s*,?\s+(?:(Class [A-Z])\s+)?(?:Common Stock|Common Shares|Ordinary Shares?|Capital Stock|American Depositary Shares?|American Depository Shares?|Sponsored ADRs?|ADRs?|ADSs?)\b.*$/i

export function shortCompanyName(name: string | null | undefined): string {
  if (!name) return ''
  const m = TAIL.exec(name)
  if (!m || m.index === 0) return name
  const head = name.slice(0, m.index).replace(/[\s,]+$/, '')
  return m[1] ? `${head} ${m[1]}` : head
}
