/** 机会页统一用词 (0.39.0 起; 0.41.0 雷达读数退役, 只留状态栏与作战台用到的词)。
 *
 * 规则 (Opportunities.terms.test.ts 守门):
 * - 状态用名词短语 + 原因 / 时刻, 不用口语句: 「科技·空 名额满 4/4」而不是「今天不会再开空」;
 *   「入场截止 15:30」而不是「已经截止了」; 「休市 · 无待执行」而不是「现在不用操作」。
 * - 只写可核对的事实: 名额 / 价位 / 时刻 / 原因码对应的中文; 不写推测、劝告、语气词。
 * - 禁用: 今天不会 / 不用操作 / 先回区先得 / 照着追 / 才释放 / 也不成交 / 就退出 / 还有 / 吧 / 呢 / 了。 / 。
 * 后端只给代码与数字, 文案只在这里拼。
 */
import type { OppRadarView, RadarSlot } from '@/lib/api'

export const SIDE_ZH: Record<string, string> = { long: '多', short: '空' }

/** 板块 (instruments.sector, Nasdaq 粗分类) 中文名 */
const SECTOR_ZH: Record<string, string> = {
  Technology: '科技', Finance: '金融', 'Consumer Discretionary': '可选消费', 'Consumer Staples': '必需消费',
  Industrials: '工业', Energy: '能源', 'Health Care': '医疗', Telecommunications: '通信', Utilities: '公用事业',
  'Real Estate': '地产', 'Basic Materials': '原材料', Miscellaneous: '其他',
}
const sectorZh = (s?: string | null) => (s ? SECTOR_ZH[s] ?? s : '未识别')
export const slotLabel = (s: Pick<RadarSlot, 'sector' | 'side'>) => `${sectorZh(s.sector)}·${SIDE_ZH[s.side] ?? ''}`

/** 下一事件 */
export const EVENT_ZH: Record<string, string> = {
  board_first: '机会板首刷', gap: '跳空定稿', open: '开盘', entry_open: '纸面可成交', open_radar: '开盘放量榜',
  intraday_check: '盘中检查', entry_close: '入场截止 · 时间平仓', close: '收盘', pipeline: '盘后管道',
}

/** 页头阶段 */
export const PHASE_ZH: Record<string, string> = {
  closed: '休市', night: '盘前未开', premarket: '盘前', open_wait: '开盘观察', session: '盘中', after: '已收盘',
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

/** 泳道右侧读数 */
export function laneText(view: OppRadarView): Record<'board' | 'gap' | 'open' | 'intraday', string> {
  const L = view.lanes
  const ck = view.clock
  const today = ck.et.slice(0, 10)
  const nextTag = dayTag(ck.next.day, today)
  const blocked = view.blips.filter((b) => b.kind === 'blocked').length
  const orders = view.blips.filter((b) => b.kind === 'order').length
  const fills = L.board.fills.length
  const board = !ck.trading_day ? `挂单 ${orders + blocked} · ${nextTag || '次日'} ${ck.board_first} 首刷`
    : [`挂单 ${orders + blocked}`, blocked ? `受限 ${blocked}` : '', fills ? `成交 ${fills}` : ''].filter(Boolean).join(' · ')
  const gap = L.gap.at ? `定稿 ${L.gap.n} 只 · 观察` : L.gap.warming ? '取数中' : L.gap.live ? `实时 ${L.gap.n} 只` : `${!ck.trading_day ? `${nextTag} ` : ''}09:00 定稿`
  const push = L.open.push ? (!L.open.push.enabled ? '推送关' : L.open.push.today ? `推送 ${L.open.push.today.symbols.length}` : `推送 ≥${L.open.push.min_rvol}×`) : ''
  const open = L.open.at ? [`点名 ${L.open.picks.length}`, push].filter(Boolean).join(' · ') : [`${!ck.trading_day ? `${nextTag} ` : ''}${L.open.ready_at} 出榜`, push].filter(Boolean).join(' · ')
  const intraday = L.intraday.ticks.length ? `检查 ${L.intraday.ticks.length}/${L.intraday.schedule.length} · 出卡 ${L.intraday.fired}`
    : `${!ck.trading_day ? `${nextTag} ` : ''}${L.intraday.schedule[0] ?? '10:00'} 首检`
  return { board, gap, open, intraday }
}

/** 名额卡副标题 */
export function capSub(s: RadarSlot): string {
  if (s.state === 'full') return `受限 ${s.waiting.length}`
  if (s.state === 'caution') return `余 ${s.free} · 临近 ${s.near.length}`
  if (s.state === 'tight') return `余 ${s.free} · 候选 ${s.waiting.length}`
  return ''
}
