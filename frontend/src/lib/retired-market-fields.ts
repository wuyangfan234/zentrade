/** Built-in fields without a supported US data source. Exact IDs only: ext data is independent. */
export const RETIRED_MARKET_FIELDS = new Set([
  'turnover_rate', 'total_shares', 'float_shares', 'market_cap', 'float_market_cap',
  'deviate_3d', 'deviate_10d', 'deviate_30d',
])

/** 后端 SHARE_CAPITAL_FIELDS (app/factors/registry.py) 的镜像: 缺股本数据源, 策略评分里这些因子的
 *  权重只能保持原值 (改动即 400 「缺少股本数据」)。改后端集合时同步这里, 由单测守住。 */
export const SHARE_CAPITAL_FIELDS = new Set([
  'turnover_rate', 'turnover_ratio_5d', 'turnover_z_60d', 'turnover_mean_20d', 'turnover_std_20d', 'log_float_mv',
  'total_shares', 'float_shares', 'market_cap', 'float_market_cap',
])

const RETIRED_BASIC_FILTER_KEYS = [
  'market_cap_min', 'market_cap_max', 'float_cap_min', 'float_cap_max', 'turnover_min', 'turnover_max',
] as const

type RetiredBasicFilterKey = typeof RETIRED_BASIC_FILTER_KEYS[number]

/** Drop retired settings when loading defaults for a new strategy; never mutate saved data. */
export function stripRetiredBasicFilter<T extends object>(value: T): Omit<T, RetiredBasicFilterKey> {
  const cleaned = { ...value }
  for (const key of RETIRED_BASIC_FILTER_KEYS) delete (cleaned as Record<string, unknown>)[key]
  return cleaned
}

/** Runtime overrides must explicitly cancel inherited META defaults, not merely omit them. */
export function clearRetiredBasicFilter(value: object = {}): Record<string, unknown> {
  return {
    ...stripRetiredBasicFilter(value),
    market_cap_min: null, market_cap_max: null,
    float_cap_min: null, float_cap_max: null,
    turnover_min: null, turnover_max: null,
  }
}
