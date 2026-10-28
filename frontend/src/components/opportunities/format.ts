/** 机会页共用的数字格式 (0.29.0 从 SwingBoards / IntradayBoard 各自一份合并)。 */
const fin = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n)

export const p2 = (n?: number | null) => (fin(n) ? n.toFixed(2) : '—')
/** 带符号的百分比, 1 位小数; 没值给空串 (卡上不占位) */
export const pct = (n?: number | null) => (fin(n) ? `${n > 0 ? '+' : ''}${n.toFixed(1)}%` : '')
export const usd = (v: number) => `${v > 0 ? '+' : v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
export const rr = (v?: number | null) => (fin(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}R` : '—')
/** YYYY-MM-DD... → MM-DD */
export const md = (iso?: string | null) => (iso ? iso.slice(5, 10) : '')
/** ISO (美东带偏移) → 北京时间 HH:MM */
export const hm = (iso?: string | null) => (iso ? new Date(iso).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }) : '')
/** 今天 (美东) YYYY-MM-DD */
export const etToday = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' })

/** 现价来源角标: 实时不标; 收盘带日期 (休市时是最近一个已收盘交易日, 不是信号日) */
const PX_SRC: Record<string, string> = { live: '实时', snapshot: '快照', premarket: '盘前', close: '收盘' }
export const pxSrc = (c: { price_source?: string | null; price_asof?: string | null }) =>
  c.price_source === 'close' && c.price_asof ? `收盘 ${md(c.price_asof)}` : (PX_SRC[c.price_source ?? 'close'] ?? '')
export const pxTitle = (c: { price_source?: string | null; price_asof?: string | null }) =>
  `现价 · ${PX_SRC[c.price_source ?? 'close'] ?? ''}${c.price_asof ? ` ${c.price_asof}` : ''}`
