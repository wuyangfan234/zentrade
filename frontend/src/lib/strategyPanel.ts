// 策略页面板的纯函数层: 一个策略一行, 多空两面在详情里切换。只做展示换算, 不算任何新指标。
import type {
  StrategyPanelBoardCard,
  StrategyPanelEntry,
  StrategyPanelPaper,
  StrategyPanelReplay,
  StrategyPanelSide,
  StrategyPanelTrade,
} from '@/lib/api'

export type PanelSideKey = 'long' | 'short'
export type RailGroup = 'selected' | 'active' | 'idle'
export type RailFilter = 'selected' | 'active' | 'all'

/** 默认看哪一面: 只有做空入选时看空, 其余看多 */
export function defaultSide(entry?: StrategyPanelEntry | null): PanelSideKey {
  return entry?.sides.short?.selected && !entry.sides.long.selected ? 'short' : 'long'
}

export function sideOf(entry: StrategyPanelEntry | null | undefined, side: PanelSideKey): StrategyPanelSide | null {
  if (!entry) return null
  return (side === 'short' ? entry.sides.short : entry.sides.long) ?? entry.sides.long
}

/** 入选了哪几面 (机会页规则设置里的 3 多 3 空) */
export function selectedSides(entry?: StrategyPanelEntry | null): PanelSideKey[] {
  const out: PanelSideKey[] = []
  if (entry?.sides.long.selected) out.push('long')
  if (entry?.sides.short?.selected) out.push('short')
  return out
}

export function selectedLabel(sides: PanelSideKey[]): string {
  if (sides.length === 2) return '入选 · 多空'
  return sides[0] === 'short' ? '入选 · 空' : '入选 · 多'
}

/** 有完整回放 (成交 ≥ 5 笔, 给出每笔 R 与超额) */
export function hasFullReplay(r?: StrategyPanelReplay | null): r is StrategyPanelReplay & { mean_r: number; excess: number; fill: number; p: number } {
  return !!r && !r.thin && typeof r.mean_r === 'number' && typeof r.excess === 'number'
}

const absFixed = (v: number, digits: number) => Math.abs(v).toFixed(digits)
const rounded = (v: number, digits: number) => Number(v.toFixed(digits))

/** +0.25R / -1.06R / 0.00R; 没有数 → — */
export function fmtR(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const x = rounded(v, digits)
  return `${x > 0 ? '+' : x < 0 ? '-' : ''}${absFixed(x, digits)}R`
}

/** 红涨绿跌的文字色类; 没有数或为 0 不着色 */
export function toneClass(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted'
  const x = rounded(v, digits)
  return x > 0 ? 'text-bull' : x < 0 ? 'text-bear' : 'text-secondary'
}

export function fmtRate(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? '—' : `${Math.round(v * 100)}%`
}

export function fmtP(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return '—'
  return p < 0.01 ? '<0.01' : p.toFixed(2)
}

/** YYYY-MM-DD → MM-DD */
export function md(day?: string | null): string {
  return day && day.length >= 10 ? day.slice(5, 10) : '—'
}

/** 机会板上一张卡的现状: 持仓 / 不下单 / 入场区内 / 挂单 */
export function boardLabel(c: Pick<StrategyPanelBoardCard, 'state' | 'skip'>): string {
  if (c.state === 'holding') return '持仓'
  if (c.skip) return '不下单'
  return c.state === 'in_zone' ? '入场区内' : '挂单'
}

const WHY: Record<string, string> = { stop: '止损', target: '目标', time: '到期' }
export function whyLabel(why?: string | null): string {
  return (why && WHY[why]) || '其他'
}

/** 模拟成绩的可信度: 没有成交 / 样本少 (生成机会日不足, 不给区间) / 背离 (区间不含回放值) / 正常 */
export type PaperState = 'none' | 'thin' | 'diverged' | 'ok'
export function paperState(p?: StrategyPanelPaper | null): PaperState {
  if (!p || p.fills === 0) return 'none'
  if (p.diverged) return 'diverged'
  return p.ci ? 'ok' : 'thin'
}

export interface CurvePoint {
  /** 平仓日 */
  d: string
  symbol: string
  r: number
  /** 模拟累计 R */
  cum: number
  /** 同样笔数按回放每笔 R 该有的累计; 没有回放 = null */
  exp: number | null
}

/** 累计 R 曲线: 一笔成交一个点 (后端已按平仓日升序), 不插值 */
export function curvePoints(trades: StrategyPanelTrade[], replayMean: number | null | undefined): CurvePoint[] {
  let cum = 0
  return trades.map((t, i) => {
    cum += t.r
    return {
      d: t.exit_date ?? t.fill_date ?? t.signal_day ?? '',
      symbol: t.symbol,
      r: t.r,
      cum: rounded(cum, 3),
      exp: replayMean == null ? null : rounded((i + 1) * replayMean, 3),
    }
  })
}

export interface DayPoint {
  /** 平仓日 */
  d: string
  /** 当日平仓笔数 */
  n: number
  /** 当日这几笔的 R 合计 */
  r: number
  /** 只有一笔时给出代码 */
  symbol: string | null
  /** 当日收尾时的模拟累计 R / 回放期望 */
  cum: number
  exp: number | null
}

/** 曲线按平仓日画: 同一天的几笔并成一个点 (同一个横坐标上不画来回的竖线) */
export function dayPoints(points: CurvePoint[]): DayPoint[] {
  const out: DayPoint[] = []
  for (const p of points) {
    const last = out[out.length - 1]
    if (last && last.d === p.d) {
      last.n += 1
      last.r = rounded(last.r + p.r, 3)
      last.symbol = null
      last.cum = p.cum
      last.exp = p.exp
    } else {
      out.push({ d: p.d, n: 1, r: p.r, symbol: p.symbol, cum: p.cum, exp: p.exp })
    }
  }
  return out
}

export interface RailRow {
  id: string
  name: string
  group: RailGroup
  /** 入选的面; 空数组 = 未入选 */
  selected: PanelSideKey[]
  inPool: boolean
  monitored: boolean
  /** 所选信号日在自选上的命中次数 (多空相加) */
  hits: number
  /** 默认那一面的回放超额 / 模拟每笔 */
  excess: number | null
  paperMean: number | null
  diverged: boolean
}

export function buildRailRow(
  s: { id: string; name: string },
  entry: StrategyPanelEntry | null | undefined,
  flags: { inPool: boolean; monitored: boolean },
): RailRow {
  const sel = selectedSides(entry)
  const side = sideOf(entry, defaultSide(entry))
  const hits = (entry?.sides.long.hits.length ?? 0) + (entry?.sides.short?.hits.length ?? 0)
  const paper = side?.paper
  return {
    id: s.id,
    name: s.name,
    group: sel.length ? 'selected' : flags.inPool || flags.monitored ? 'active' : 'idle',
    selected: sel,
    inPool: flags.inPool,
    monitored: flags.monitored,
    hits,
    excess: hasFullReplay(side?.replay) ? side!.replay!.excess! : null,
    paperMean: paper && paper.fills > 0 ? paper.mean_r : null,
    diverged: !!entry && (!!entry.sides.long.paper?.diverged || !!entry.sides.short?.paper?.diverged),
  }
}

const GROUP_ORDER: Record<RailGroup, number> = { selected: 0, active: 1, idle: 2 }

/** 列表顺序: 入选 → 扫描 / 监控 → 其余; 组内按回放超额从高到低, 没有回放的排后, 再按名字 */
export function sortRailRows(rows: RailRow[]): RailRow[] {
  return [...rows].sort((a, b) =>
    GROUP_ORDER[a.group] - GROUP_ORDER[b.group]
    || (b.excess ?? -Infinity) - (a.excess ?? -Infinity)
    || a.name.localeCompare(b.name, 'zh-Hans-CN'))
}

export function filterRailRows(rows: RailRow[], f: RailFilter): RailRow[] {
  if (f === 'all') return rows
  return rows.filter(r => (f === 'selected' ? r.group === 'selected' : r.group !== 'idle'))
}

export const RAIL_GROUP_LABEL: Record<RailGroup, string> = { selected: '入选策略', active: '扫描 / 监控', idle: '未启用' }
