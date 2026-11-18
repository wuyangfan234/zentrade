/** 机会页统一用词 (0.39.0 起; 0.41.0 雷达读数退役, 只留状态栏与作战台用到的词)。
 *
 * 规则 (Opportunities.terms.test.ts 守门):
 * - 状态用名词短语 + 原因 / 时刻, 不用口语句: 「科技·空 名额满 4/4」而不是「今天不会再开空」;
 *   「入场截止 15:30」而不是「已经截止了」; 「休市 · 无待执行」而不是「现在不用操作」。
 * - 只写可核对的事实: 名额 / 价位 / 时刻 / 原因码对应的中文; 不写推测、劝告、语气词。
 * - 禁用: 今天不会 / 不用操作 / 先回区先得 / 照着追 / 才释放 / 也不成交 / 就退出 / 还有 / 吧 / 呢 / 了。 / 。
 * 后端只给代码与数字, 文案只在这里拼。
 */
import type { RadarSlot } from '@/lib/api'
import { admissionLabel } from '@/lib/reasonLabels'

export const SIDE_ZH: Record<string, string> = { long: '多', short: '空' }
/** 2026-11-15 B 方案: 方向用中性色 + 箭头, 红绿只留给盈亏 */
export const SIDE_ARROW: Record<string, string> = { long: '↑ 做多', short: '↓ 做空' }

/** 板块 (instruments.sector, Nasdaq 粗分类) 中文名 */
const SECTOR_ZH: Record<string, string> = {
  Technology: '科技', Finance: '金融', 'Consumer Discretionary': '可选消费', 'Consumer Staples': '必需消费',
  Industrials: '工业', Energy: '能源', 'Health Care': '医疗', Telecommunications: '通信', Utilities: '公用事业',
  'Real Estate': '地产', 'Basic Materials': '原材料', Miscellaneous: '其他',
}
export const sectorZh = (s?: string | null) => (s ? SECTOR_ZH[s] ?? s : '未识别')
export const slotLabel = (s: Pick<RadarSlot, 'sector' | 'side'>) => `${sectorZh(s.sector)}·${SIDE_ZH[s.side] ?? ''}`

/** 下一事件 */
export const EVENT_ZH: Record<string, string> = {
  board_first: '机会板首次刷新', gap: '跳空定稿', open: '开盘', entry_open: '模拟可成交', open_radar: '开盘放量榜',
  intraday_check: '盘中检查', entry_close: '入场截止 · 时间平仓', close: '收盘', pipeline: '盘后同步',
}

/** 页头阶段 */
export const PHASE_ZH: Record<string, string> = {
  closed: '休市', night: '盘前未开', premarket: '盘前', open_wait: '开盘观察', session: '盘中', after: '已收盘',
}

/** 机会板卡不做的原因 (后端 verdict_reason 原因码) · 2026-11-15 白话 */
export const SKIP_ZH: Record<string, string> = {
  holding_other: '已持仓', cooldown: '刚卖出 暂不买回', earnings: '临近财报', direction_conflict: '多空冲突',
  paper_watch: '账户限制', premarket_beyond_stop: '盘前已破止损', entry_closed: '今日入场已截止',
}

type VerdictCard = { verdict?: string | null; verdict_reason?: string | null; blocked?: string | null; paper_reason?: string | null
  state?: string | null; dist_pct?: number | null }
/** 一张机会板卡的结论: 进场 (已到进场价 / 等进场) 或不做 + 原因。以后端裁决 (verdict) 为准; 载荷里没有裁决时按 blocked 判 */
export function cardVerdict(c: VerdictCard): { order: boolean; label: string; detail: string; title?: string } {
  const code = c.verdict === 'skip' ? (c.verdict_reason ?? c.blocked ?? '') : c.verdict ? '' : (c.blocked ?? '')
  if (code) {
    // 没有中文名的新原因码: 屏上写「其他」, 原码留在悬停 (不直接上屏, 也不吞掉)
    const known = SKIP_ZH[code] ?? admissionLabel(code)
    const raw = !known && /^[a-z][a-z0-9_.:]*$/.test(code)
    let detail = known ?? (raw ? '其他' : code)
    let title: string | undefined = raw ? code : undefined
    if (code === 'paper_watch' && c.paper_reason) {
      const body = c.paper_reason.includes('：') ? c.paper_reason.split('：').slice(1).join('：') : c.paper_reason
      const first = body.replace(/;/g, '；').split('；').map((x) => x.trim()).filter(Boolean)[0]
      // 准入原因是英文码时换成中文 (direction_conflict → 多空冲突); 映射不到的码不直接上屏
      if (first) detail = admissionLabel(first) ?? (/^[a-z][a-z0-9_.:]*$/.test(first) ? SKIP_ZH.paper_watch : first)
      title = c.paper_reason
    }
    return { order: false, label: '不做', detail, title }
  }
  if (c.state === 'in_zone') return { order: true, label: '已到进场价', detail: '' }
  const detail = typeof c.dist_pct === 'number' && Number.isFinite(c.dist_pct) ? `还差 ${Math.abs(c.dist_pct).toFixed(1)}%` : '等触发'
  return { order: true, label: '等进场', detail }
}

/** 现价相对进场区 (与交易环成交条件同式 lo <= 价 <= hi): in 区内 · wait 未到 · past 已穿过 (不会成交); gap = 离区边 % */
export function zonePos(side: string, price: number | null | undefined, zone: number[] | null | undefined): { pos: 'in' | 'wait' | 'past' | null; gap: number | null } {
  const lo = zone?.[0], hi = zone?.[1]
  if (typeof price !== 'number' || !Number.isFinite(price) || price <= 0 || typeof lo !== 'number' || typeof hi !== 'number') return { pos: null, gap: null }
  if (lo <= price && price <= hi) return { pos: 'in', gap: 0 }
  const wait = side === 'short' ? price < lo : price > hi
  const gap = (price > hi ? price - hi : lo - price) / price * 100
  return { pos: wait ? 'wait' : 'past', gap }
}

/** 策略 → 白话类型 (现行 3+3 逐个对照, 其余按名字归类); 原名放悬停 */
const KIND_BY_ID: Record<string, string> = {
  ibs_mean_reversion: '超跌反弹', tv_zlsma_cross: '趋势转多', trend_breakout: '突破追涨',
  tv_smc_structure: '结构转空', tv_chandelier_exit: '趋势转空', tv_halftrend: '趋势转空',
}
export function setupKind(strategies: { id: string; name: string }[] | null | undefined, side: string): string {
  const s = strategies?.[0]
  if (!s) return ''
  if (KIND_BY_ID[s.id]) return KIND_BY_ID[s.id]
  const id = s.id.toLowerCase()
  if (/breakout|new_high|platform|squeeze/.test(id)) return '突破追涨'
  if (/pullback|support|bounce|midline/.test(id)) return '回调买入'
  if (/oversold|reversal|reversion|rsi2|ibs|williams|down_days|low_overnight|mdd|connors|shadow/.test(id)) return side === 'short' ? '超涨回落' : '超跌反弹'
  if (/volume|turnover|surge|strong_open/.test(id)) return '放量走强'
  if (/golden|macd|alignment|supertrend|zlsma|halftrend|chandelier|ut_bot|wavetrend|tke|phantom|smc|cross/.test(id)) return side === 'short' ? '趋势转空' : '趋势转多'
  return s.name
}

export const p2 = (n?: number | null) => (typeof n === 'number' && Number.isFinite(n) ? n.toFixed(2) : '—')
export const usdS = (n?: number | null) => (typeof n === 'number' && Number.isFinite(n) ? `${n > 0 ? '+' : n < 0 ? '−' : ''}$${Math.abs(Math.round(n)).toLocaleString('en-US')}` : '—')
export const rS = (n?: number | null) => (typeof n === 'number' && Number.isFinite(n) ? `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(2)}R` : '—')
export const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
export const weekday = (iso: string) => WD[new Date(`${iso.slice(0, 10)}T12:00:00Z`).getUTCDay()]

/** 美东 ISO → 北京 HH:MM (按时区换算, 夏令时 / 冬令时自动) */
export function bjHm(iso?: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false })
  } catch { return '' }
}

/** 日期标签: 同日 = 空; 次交易日 = 「周一」; 其余 MM-DD */
export function dayTag(day: string, today: string): string {
  if (day === today) return ''
  const diff = (Date.parse(`${day}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86_400_000
  return diff > 0 && diff <= 6 ? weekday(day) : md(day)
}

/** 名额卡副标题 */
export function capSub(s: RadarSlot): string {
  if (s.state === 'full') return `受限 ${s.waiting.length}`
  if (s.state === 'caution') return `余 ${s.free} · 临近 ${s.near.length}`
  if (s.state === 'tight') return `余 ${s.free} · 候选 ${s.waiting.length}`
  return ''
}

/** 等进场卡的距离: 不到 0.05% 的写「<0.1%」, 不写成「还差 0.0%」(价格没到进场区) */
export const gapText = (gap: number): string => `还差 ${gap > 0 && gap < 0.05 ? '<0.1' : gap.toFixed(1)}%`
