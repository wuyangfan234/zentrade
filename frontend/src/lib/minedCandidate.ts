/**
 * 挖掘候选 → 发布链路的共用口径: 「去发布」深链 + 候选显示名。
 * 只换显示与导航, 不改 strategy_id / 候选 id / candidate_signature / 任何已存文件。
 *
 * 显示名规则 (候选方案、挖掘页候选列表、策略池三处同名):
 *  - 已发布: 策略本名, 走策略池同一套换名 (humanizeFactorIds, 因子 id 换中文);
 *  - 未发布的挖掘因子候选: 发布时的默认名 = 因子中文名 + 方向 (“日振幅 低”), 与后端 default_strategy_name 同口径;
 *  - 其他候选: 保存时的名字。
 */
import type { ResearchCandidate } from './api'
import { humanizeFactorIds } from './strategyDisplay'

/** 与后端 mining_candidates.STRATEGY_NAME_MAX 一致 */
const NAME_MAX = 40

/** 挖掘页深链: 因子 › 挖掘, 打开该候选所属那一轮并选中它 (MiningWorkbench 读 run / candidate 参数) */
export function miningCandidateHref(runId?: string | null, signature?: string | null): string {
  const params = new URLSearchParams({ tab: 'mining' })
  if (runId) {
    params.set('run', runId)
    if (signature) params.set('candidate', signature)
  }
  return `/factors?${params.toString()}`
}

/** 随深链带上的路由状态: 挖掘页据此把「策略候选」区滚进视口 (只这一次, 平时点选候选不滚动) */
export interface MiningFocusState { miningFocus: string }
export function miningFocusState(signature?: string | null): MiningFocusState | undefined {
  return signature ? { miningFocus: signature } : undefined
}

/** 挖掘因子组合的默认名 = 因子中文名 + 方向 (“日振幅 低”“5日动量 高 + 量比 低”)。
 *  directions 两种形状都收: 候选方案存的是与 factorIds 对齐的列表, 挖掘定义里是 id → 方向。 */
export function minedFactorName(factorIds: readonly string[], directions: unknown, labels: ReadonlyMap<string, string>): string {
  const dirOf = (id: string, index: number): unknown => (
    Array.isArray(directions) ? directions[index]
      : directions && typeof directions === 'object' ? (directions as Record<string, unknown>)[id] : undefined
  )
  const name = factorIds.map((id, index) => `${labels.get(id) || id} ${dirOf(id, index) === 'low' ? '低' : '高'}`).join(' + ') || '因子组合'
  const chars = Array.from(name)
  return chars.length <= NAME_MAX ? name : `${chars.slice(0, NAME_MAX - 1).join('')}…`
}

export interface MinedCandidateRef {
  runId: string | null
  signature: string | null
  strategyId: string
  factorIds: string[]
  directions: unknown
}

/** 候选方案里来自挖掘的因子组合候选 (config 带 factor_names); 其他候选返回 null */
export function minedCandidateRef(item: ResearchCandidate): MinedCandidateRef | null {
  if (item.kind !== 'strategy') return null
  const config = (item.config ?? {}) as Record<string, unknown>
  if (!Array.isArray(config.factor_names)) return null
  const factorIds = config.factor_names.filter((id): id is string => typeof id === 'string' && id.length > 0)
  if (!factorIds.length) return null
  const text = (value: unknown) => (typeof value === 'string' && value ? value : null)
  return {
    runId: text(config.origin_run_id),
    signature: text(config.candidate_signature),
    strategyId: text(config.strategy_id) ?? '',
    factorIds,
    directions: config.directions,
  }
}

/** 已发布策略在策略池里的显示名 (策略页同一套换名) */
export function publishedStrategyName(name: string, labels: ReadonlyMap<string, string>): string {
  return labels.size ? humanizeFactorIds(name, labels as Map<string, string>) : name
}

/** 候选方案里一条候选的显示名 (见文件头规则)。
 *  因子目录或策略列表还没到 (strategies = undefined) 时先用保存时的名字: 不先闪英文 id, 也不先闪一个随后会变的默认名。 */
export function researchCandidateName(
  item: ResearchCandidate,
  labels: ReadonlyMap<string, string>,
  strategies: readonly { id: string; name: string }[] | undefined,
): string {
  const ref = minedCandidateRef(item)
  if (!ref || !labels.size || !strategies) return item.name
  const published = ref.strategyId ? strategies?.find(strategy => strategy.id === ref.strategyId) : undefined
  return published ? publishedStrategyName(published.name, labels) : minedFactorName(ref.factorIds, ref.directions, labels)
}
