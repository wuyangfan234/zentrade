/**
 * 个股详情外链 — 可配置 URL 模板 + 证券代码。
 *
 * 占位符保持独立, 用户自行排列:
 *   {code}   纯代码, 如 AAPL
 *   {market} 市场标识小写 (美股恒 us)
 *   {symbol} 完整 symbol, 如 AAPL
 * 模板留空 = 关闭外链。常用: https://www.tradingview.com/chart/?symbol={symbol}
 */
import { storage } from '@/lib/storage'

/** 形状守卫: 美股代码 (1-5 位大写字母, 可带点如 BRK.B) */
const STOCK_SYMBOL_RE = /^([A-Z]{1,5}(?:\.[A-Z])?)$/

/** 未设置或留空 → 返回 '' 即关闭外链 */
export function loadStockExternalTemplate(): string {
  return storage.stockExternalTemplate.get('')
}

export function saveStockExternalTemplate(tpl: string): void {
  storage.stockExternalTemplate.set(tpl.trim())
}

export function buildStockExternalUrl(template: string, symbol: string): string | null {
  if (!template) return null
  // scheme 白名单: 只放行 http/https, 挡掉 javascript:/data: 等危险 scheme
  if (!/^https?:\/\//i.test(template.trim())) return null
  const m = symbol.match(STOCK_SYMBOL_RE)
  if (!m) return null
  const code = m[1]
  const market = 'us'
  return template
    .replaceAll('{code}', code)
    .replaceAll('{market}', market)
    .replaceAll('{symbol}', symbol)
}
