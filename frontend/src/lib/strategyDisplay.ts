/**
 * 策略 / 来源名的展示层处理（2026-10-11 P2、P6）。只改显示，不改后端名称、id、指纹与台账。
 *
 * - P6：去掉内部出处名，如「（oasis-quant 原版·吊灯止损）」和「Pine 原版」里的「原版」。
 * - P2：英文指标名后紧跟中文名，如「UT Bot（ATR 追踪止损）翻多」。纯中文名和已带中文名的不变。
 */

const PROVENANCE_RE = /\s*[（(]\s*oasis-quant[^（）()]*[）)]/g
const ORIGINAL_RE = /\s原版(?=\s|$)/g

/** 英文名 → 中文名。按顺序匹配，长的在前（「Phantom 4h」先于「Phantom」）。 */
const ZH_ALIASES: Array<[RegExp, string]> = [
  [/UT Bot/, 'ATR 追踪止损'],
  [/Supertrend/i, '超级趋势'],
  [/ZLSMA/, '零滞后均线'],
  [/HalfTrend/, '半趋势'],
  [/WaveTrend/, '波浪趋势'],
  [/\bSMC\b/, '聪明钱结构'],
  [/Phantom 4h/, '幻影'],
  [/Phantom/, '幻影'],
]

export function stripProvenance(name: string): string {
  return name.replace(PROVENANCE_RE, '').replace(ORIGINAL_RE, '').trim()
}

export function strategyZh(name: string): string | null {
  for (const [re, zh] of ZH_ALIASES) if (re.test(name)) return zh
  return null
}

/** 去出处名，再在英文指标名后面紧跟中文名（已带过就不重复）。 */
export function displayStrategyName(name: string): string {
  const clean = stripProvenance(name)
  for (const [re, zh] of ZH_ALIASES) {
    const m = re.exec(clean)
    if (!m) continue
    const end = m.index + m[0].length
    if (clean.slice(end).startsWith(`（${zh}）`)) return clean
    return `${clean.slice(0, end)}（${zh}）${clean.slice(end).trimStart()}`
  }
  return clean
}

const NAME_KEYS = new Set(['name', 'source_name'])

/** 决策台载荷的名称清洗：names 映射的值、各处 name / source_name 字段。其他文本、id 不动。 */
export function cleanDeskNames<T>(payload: T): T {
  const walk = (v: unknown, key: string | null, inNames: boolean): unknown => {
    if (typeof v === 'string') return inNames || (key !== null && NAME_KEYS.has(key)) ? displayStrategyName(v) : v
    if (Array.isArray(v)) return v.map((x) => walk(x, null, false))
    if (v && typeof v === 'object') {
      const out: Record<string, unknown> = {}
      for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
        out[k] = k === 'names' && x && typeof x === 'object' && !Array.isArray(x)
          ? Object.fromEntries(Object.entries(x as Record<string, unknown>).map(([nk, nv]) => [nk, walk(nv, null, true)]))
          : walk(x, k, false)
      }
      return out
    }
    return v
  }
  return walk(payload, null, false) as T
}
