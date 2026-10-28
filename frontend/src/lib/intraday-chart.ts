import type { MinuteKlineRow } from '@/lib/api'

export type DailySummary = { date: string; open: number | null; high: number; low: number; close: number }

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: MinuteKlineRow[]): MinuteKlineRow | null {
  if (!data.length) return null
  return {
    datetime: data[0].datetime,
    open: data[0].open,
    high: Math.max(...data.map(row => row.high)),
    low: Math.min(...data.map(row => row.low)),
    close: data[data.length - 1].close,
    // 首根拼接分钟可能无成交量 (null): 只累加已知分钟; 全部未知时为 null, 不伪装成 0
    volume: data.some(row => row.volume != null)
      ? data.reduce((total, row) => total + (row.volume ?? 0), 0) : null,
    amount: data.every(row => row.amount != null && Number.isFinite(row.amount))
      ? data.reduce((total, row) => total + row.amount!, 0) : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为美东墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[]): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    } else {
      hasAmount = false
    }
    volume += row.volume ?? 0
    result.push(hasAmount && volume > 0 ? amount / volume : null)
  }
  return result
}

/**
 * 连续美股交易日时间轴。
 *
 * 分钟根使用 [09:30, 16:00)，共 390 根；额外保留 16:00 作为收盘边界，
 * 让图表轴可以准确标注收盘而不把它误当成第 391 根成交分钟。
 */
export function generateFullDayTimes(): string[] {
  const times: string[] = []
  const openMinute = 9 * 60 + 30
  const totalMinutes = 390
  for (let index = 0; index <= totalMinutes; index++) {
    const minuteOfDay = openMinute + index
    const hour = Math.floor(minuteOfDay / 60)
    const minute = minuteOfDay % 60
    times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
