/**
 * 后端「美东墙钟 naive 串」的解析与展示。
 *
 * AI 报告 created_at 由 JsonReportStore 写成美东墙钟、无时区后缀 ('2026-09-30T01:36:34')。
 * `new Date(naive)` 会按浏览器本地时区解析 —— 在北京打开会把报告时间算早 12 小时
 * (夏令时) / 13 小时 (冬令时), 「N 小时前」与「今天是否已生成」都会错。
 * 这里显式按 America/New_York 解析, 结果与浏览器时区无关。
 */
const ET_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const ET_DATE = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' })

/** 某绝对时刻在美东的 UTC 偏移 (毫秒, EDT=-4h, EST=-5h)。 */
function etOffsetMs(at: Date): number {
  const p = Object.fromEntries(ET_PARTS.formatToParts(at).map(x => [x.type, x.value]))
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second)
  return asUtc - Math.floor(at.getTime() / 1000) * 1000
}

/** 美东墙钟 naive 串 → Date; 已带 Z/偏移的串原样解析; 空/非法返回 null。 */
export function etNaiveToDate(iso: string | null | undefined): Date | null {
  const raw = String(iso ?? '').trim()
  if (!raw) return null
  if (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)) {
    const d = new Date(raw)
    return isNaN(d.getTime()) ? null : d
  }
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/)
  if (!m) return null
  const wallAsUtc = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] ?? 0))
  // 先用墙钟当 UTC 估一次偏移, 再按修正后的时刻复算 (跨夏令时切换点也成立)
  let t = wallAsUtc - etOffsetMs(new Date(wallAsUtc))
  t = wallAsUtc - etOffsetMs(new Date(t))
  return new Date(t)
}

/** 美东今日 'YYYY-MM-DD'。 */
export function etToday(now: Date = new Date()): string {
  return ET_DATE.format(now)
}

/** 日历日加减。iso 必须是 YYYY-MM-DD, 与浏览器时区无关。 */
export function addCalendarDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}

/** later - earlier, 单位日历日。两边都是 YYYY-MM-DD。 */
export function calendarDayDiff(later: string, earlier: string): number {
  const utc = (s: string) => {
    const [y, m, d] = s.split('-').map(Number)
    return Date.UTC(y, m - 1, d)
  }
  return Math.round((utc(later) - utc(earlier)) / 86_400_000)
}

/** 美东 naive 时间戳的相对时间文案。 */
export function fmtRelativeEt(iso: string, now: Date = new Date()): string {
  const d = etNaiveToDate(iso)
  if (!d) return iso ?? ''
  const diff = now.getTime() - d.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
  if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
  return d.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })
}
