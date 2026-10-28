/**
 * 自选分时“本地续画”用的美东分钟时钟。
 *
 * 美股常规时段 09:30–16:00 ET 单段连续 (无午休)。服务端分钟K按开始时刻标注 (首根 09:30、末根 15:59),
 * 因此 09:30:00–15:59:59 内当前分钟即正在形成的那根。分钟K契约是美东墙钟 naive 'YYYY-MM-DDTHH:MM:00',
 * 必须按 America/New_York 计算, 不能用浏览器本地时区。
 * 节假日由服务端分钟数据兜底 (休市日无当日分钟K, 续画不会追加)。
 */
export function usLiveMinute(now: Date): { inSession: boolean; barTs: string } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', weekday: 'short', hour12: false,
    }).formatToParts(now).map((p) => [p.type, p.value]),
  )
  const hh = Number(parts.hour) % 24
  const mm = Number(parts.minute)
  const minutes = hh * 60 + mm
  const weekday = parts.weekday !== 'Sat' && parts.weekday !== 'Sun'
  const inSession = weekday && minutes >= 9 * 60 + 30 && minutes < 16 * 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return { inSession, barTs: `${parts.year}-${parts.month}-${parts.day}T${pad(hh)}:${pad(mm)}:00` }
}
