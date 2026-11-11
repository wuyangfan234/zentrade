/**
 * 台账原因码 → 界面短词 (出场原因、挂单取消、准入拦截)。
 * 新增原因码时在这里补一行; reasonLabels.test.ts 拿后端源码里的原因码做差, 缺一个就不通过。
 * 映射不到的码不直接上屏: 有后端文案用后端文案, 没有则显示「其他」, 原码放悬停。
 */
export const REASON_LABEL: Record<string, string> = {
  // 出场
  invalidation: '止损',
  trailing_stop: '移动止损',
  time_stop: '到期',
  target_zone_upper: '到达目标',
  target_zone_lower: '目标减半',
  earnings_reduce: '财报前减半',
  corp_action: '公司行动',
  manual: '人工',
  watchlist_removed: '移出自选',
  breakeven_after_partial: '减半后保本',
  // 挂单取消
  entry_window_expired: '入场窗到期',
  // 准入拦截
  valuation_incomplete: '持仓估值不全',
  regular_open_unavailable: '缺当日开盘价',
  earnings_calendar_unavailable: '财报日历不可用',
  corporate_actions_unavailable: '公司行动不可用',
  circuit_breaker_day_halt: '当日熔断',
  circuit_breaker_unavailable: '熔断评估出错',
  corporate_action_pending: '公司行动待处理',
  corporate_actions_not_covered: '公司行动未覆盖',
  earnings_window: '财报窗口',
  premarket_beyond_stop: '盘前越过止损',
  premarket_check_pending: '盘前检查未完成',
}

/** 公司行动类型 → 中文; 未收录的类型统一显示「公司行动」。 */
export const CORP_ACTION_LABEL: Record<string, string> = {
  cash_dividend: '现金分红',
  stock_dividend: '送股',
  forward_split: '拆股',
  split: '拆股',
  reverse_split: '合股',
  unit_split: '单位拆分',
  spin_off: '分拆',
  name_change: '更名',
  rights_distribution: '配股',
  redemption: '赎回',
  worthless_removal: '退市清算',
}
export function corpActionLabel(type?: string | null): string {
  return CORP_ACTION_LABEL[type ?? ''] ?? '公司行动'
}

/** 准入拦截码 (交易环 / 下单簿) → 中文; 机会板「不下单」原因与系统状态页准入漏斗共用。 */
export const ADMISSION_LABEL: Record<string, string> = {
  source_not_tradable: '该来源未开放下单',
  risk_zero: '风险额为 0',
  v4_book_only: '仅机会板可开仓',
  not_in_watchlist: '不在自选',
  direction_conflict: '多空冲突',
  sector_side_cap: '同板块同向已满',
  sector_unknown: '板块未知',
  daily_open_cap: '当日开仓已满',
  max_positions: '持仓数已满',
  portfolio_risk: '组合风险已满',
  watchlist_removed: '已移出自选',
  borrow_unavailable: '不可借券',
  explore_daily_quota: '探索额度已用完',
  legacy_card: '历史规则',
  book_requires_v4: '需现行规则集',
  valuation_incomplete: '持仓估值不全',
  premarket_beyond_stop: '盘前越过止损',
  premarket_check_pending: '盘前检查未完成',
}
/** "source_not_tradable:phantom_4h" → '该来源未开放下单'; 未收录的英文码返回 null (调用方决定兜底) */
export function admissionLabel(code?: string | null): string | null {
  if (!code) return null
  return ADMISSION_LABEL[code.split(':')[0]] ?? null
}

const RAW_CODE = /^[a-z][a-z0-9_.:]*$/

/** 原因码 → 短词。fallback = 后端给的中文文案 (可空)。 */
export function reasonLabel(code?: string | null, fallback?: string | null): string {
  if (!code) return fallback ?? ''
  if (REASON_LABEL[code]) return REASON_LABEL[code]
  const m = /^trailing_(-?[\d.]+)R$/.exec(code)
  if (m) return `浮盈 ${m[1]}R`
  if (fallback && fallback !== code) return fallback
  // 后端自由文本 (中文句子) 原样显示; 只有纯英文原因码才收成「其他」
  return RAW_CODE.test(code) ? '其他' : code
}
