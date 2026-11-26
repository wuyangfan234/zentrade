/** P1 UI (2026-11-26 Todo #10): regime 状态条 / 策略账本 / 卡时效 / 卡归因 的纯函数 (单测守着)。
 * 只读后端已有字段, 不新增接口:
 *  - 板 meta.regime = swing_desk.regime_for_snapshot → {state, as_of, reasons, source, ...}
 *  - 板 meta.ledger = strategy_ledger.ledger_for 切片 → {status, month, regime, k, cells{"rule|side": {n, excess, tier}}}
 *  - 卡上五字段 regime_state / gate_id / ledger_tier / score_components / timing_tier + T1 卡 t1 (docs/P1-B2-CARD-ATTRIBUTION.md)
 * regime 依据后端只给字符串 (如「自选宽度 55%」); 门槛按 backend/app/opportunity/regime.py 的常量还原, 改那边常量要同步这里。 */
import type { CardT1Info, LedgerTierZh, RegimeState, SwingBoardCard, SwingBoardsPayload, SwingLedgerMeta, SwingRegimeMeta } from '@/lib/api'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const MINUS = '−'
/** R 值两位小数带符号 (舍入成 0 的不带符号) */
export const fR = (v: number | null | undefined) => {
  if (!fin(v)) return '—'
  const a = Math.abs(v).toFixed(2)
  return `${Number(a) === 0 ? '' : v > 0 ? '+' : MINUS}${a}R`
}

// ───────────────────────────── regime ─────────────────────────────
export const REGIME_STATES: RegimeState[] = ['进攻', '震荡', '防守']
export const REGIME_SRC_ZH: Record<string, string> = { snapshot: '盘后快照', intraday: '盘中整点', t1_record: 'T1 锚定记录' }

/** 板 meta.regime → 校验过的对象; 回滚开关关 / 旧快照 = null */
export function regimeOf(swing: SwingBoardsPayload | null | undefined): SwingRegimeMeta | null {
  const r = (swing?.meta as Record<string, unknown> | undefined)?.regime as SwingRegimeMeta | undefined
  return r && typeof r === 'object' && REGIME_STATES.includes(r.state) ? r : null
}

/** 状态条指纹: 两页同一状态 + 同一时间戳 + 同一来源 → 同一指纹 (页面上写出来, 断言两页一致) */
export const regimeFp = (r: SwingRegimeMeta | null) => (r ? `${r.state}|${r.as_of ?? ''}|${r.source ?? ''}` : 'none')

// regime.py 常量 (见文件头)
const BREADTH_ATTACK = 0.55, BREADTH_DEFENSE = 0.40, VOL_CALM = 1.2, VOL_HOT = 1.5

export type RegimeReason = { raw: string; label: string; value: string; threshold: string; ok: boolean | null }
/** 一条依据 → 指标 / 读数 / 门槛 / 是否满足进攻条件 (null = 认不出的新依据, 原文照写) */
export function parseReason(raw: string): RegimeReason {
  let m = /^SPY\s*([<>])\s*MA50$/.exec(raw)
  if (m) return { raw, label: 'SPY 对 MA50', value: m[1] === '>' ? '在上方' : '在下方', threshold: '> MA50', ok: m[1] === '>' }
  m = /^MA50\s*20日(上行|下行)$/.exec(raw)
  if (m) return { raw, label: 'MA50 20 日斜率', value: m[1], threshold: '上行', ok: m[1] === '上行' }
  m = /^自选宽度\s*(\d+(?:\.\d+)?)%$/.exec(raw)
  if (m) {
    const v = Number(m[1]) / 100
    return { raw, label: '自选宽度 (收盘 > MA20)', value: `${m[1]}%`, threshold: `≥ ${BREADTH_ATTACK * 100}% · 防守 < ${BREADTH_DEFENSE * 100}%`, ok: v >= BREADTH_ATTACK }
  }
  m = /^SPY\s*([<>])\s*EMA10$/.exec(raw)
  if (m) return { raw, label: 'SPY 对 EMA10', value: m[1] === '>' ? '在上方' : '在下方', threshold: '> EMA10', ok: m[1] === '>' }
  if (raw === '突破前20日高') return { raw, label: '前 20 日高点', value: '已突破', threshold: '突破 = 进攻', ok: true }
  if (raw === '跌破前20日低') return { raw, label: '前 20 日低点', value: '已跌破', threshold: '不跌破', ok: false }
  m = /^波动比5\/60\s*(\d+(?:\.\d+)?)$/.exec(raw)
  if (m) {
    const v = Number(m[1])
    return { raw, label: '波动比 5/60 日', value: v.toFixed(2), threshold: `< ${VOL_CALM} · 防守 ≥ ${VOL_HOT}`, ok: v < VOL_CALM }
  }
  return { raw, label: raw, value: '—', threshold: '—', ok: null }
}

/** 快层挂在 09:25 + 10:00–15:00 整点大盘重判任务上; 慢层随盘后扫描 */
export const REJUDGE_SLOTS = ['09:25', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00']
export function nextRejudge(et: string | null | undefined, tradingDay: boolean | null | undefined): string {
  const hm = et && et.length >= 16 ? et.slice(11, 16) : ''
  if (tradingDay && hm) {
    const nx = REJUDGE_SLOTS.find((s) => s > hm)
    return nx ? `${nx} ET 整点快层` : '今日盘后扫描 · 慢层'
  }
  return '下一交易日 09:25 ET 快层'
}

// ───────────────────────────── 账本 ─────────────────────────────
export const LEDGER_TIERS: LedgerTierZh[] = ['在用', '观察', '待定', '冷冻', '复活']
/** 待定 / 冷冻 = 不出卡 (除颜色外用图标 + 文字区分) */
export const TIER_OFF = new Set<string>(['待定', '冷冻'])
export const TIER_ICON: Record<string, string> = { 在用: '●', 观察: '◐', 待定: '⏸', 冷冻: '❄', 复活: '↺' }

export function ledgerOf(swing: SwingBoardsPayload | null | undefined): SwingLedgerMeta | null {
  const l = (swing?.meta as Record<string, unknown> | undefined)?.ledger as SwingLedgerMeta | undefined
  return l && typeof l === 'object' && typeof l.status === 'string' ? { ...l, cells: l.cells ?? {} } : null
}

export type LedgerRow = { key: string; rule: string; side: 'long' | 'short'; name: string; tier: string; n: number | null
  excess: number | null; lo: number | null; hi: number | null }
export function ledgerRows(l: SwingLedgerMeta | null, names: Map<string, string>): LedgerRow[] {
  if (!l) return []
  const rows = Object.entries(l.cells).map(([key, c]) => {
    const i = key.lastIndexOf('|')
    const rule = i > 0 ? key.slice(0, i) : key
    const side = (i > 0 ? key.slice(i + 1) : 'long') === 'short' ? 'short' as const : 'long' as const
    return { key, rule, side, name: names.get(`${rule}|${side}`) ?? names.get(rule) ?? rule, tier: String(c.tier ?? '待定'),
      n: fin(c.n) ? c.n : null, excess: fin(c.excess) ? c.excess : null, lo: fin(c.ci_low) ? c.ci_low : null, hi: fin(c.ci_high) ? c.ci_high : null }
  })
  const ord = (t: string) => { const k = LEDGER_TIERS.indexOf(t as LedgerTierZh); return k < 0 ? 99 : k }
  return rows.sort((a, b) => ord(a.tier) - ord(b.tier) || (b.excess ?? -9) - (a.excess ?? -9) || a.name.localeCompare(b.name))
}

/** 理由片段: hot = 触发条件 (淘汰行高亮) */
export type ReasonPart = { text: string; hot?: boolean }
export type TierReason = { word: '留用' | '观察' | '待定' | '换下' | '复活' | '挑战'; off: boolean; parts: ReasonPart[] }
/** 冷冻但账本没给触发条件时的写法 (不断言具体原因) */
export const FROZEN_UNKNOWN = '冷冻 · 触发条件未返回（复核连负 / 区间上限为负 / 延续上月冷冻 之一）'
/** 状态词与账本档位一一对应: 冷冻 = 换下 (触发淘汰条件), 待定 = 待定 (证据不足, 不出卡); 两者都按「不出卡」加重显示 */
/** 一行一句「为什么它在用 / 为什么被换下」, 带数字。门槛 = strategy_ledger.classify (n ≥ 30, 区间下限 > 0 / 上限 < 0, 连续 2 次复核 < 0) */
export function tierReason(tier: string | null | undefined, x: { n: number | null; excess: number | null; lo: number | null; hi: number | null }): TierReason {
  const nTxt = x.n == null ? '样本 —' : `样本 ${x.n}`
  const ci = x.lo != null && x.hi != null
  // 样本只写实际数, 够 30 才写「≥ 30」(不替后端做断言)
  const nGate = x.n != null && x.n >= 30 ? `${nTxt} ≥ 30` : nTxt
  if (tier === '在用' || tier === '复活') return { word: tier === '复活' ? '复活' : '留用', off: false, parts: [
    ci && x.lo! > 0 ? { text: `区间下限 ${fR(x.lo)} > 0` } : { text: `超额 ${fR(x.excess)}` }, { text: nGate }] }
  if (tier === '观察') return { word: '观察', off: false, parts: [
    { text: `超额 ${fR(x.excess)} > 0` }, ci ? { text: `区间 ${fR(x.lo)} ~ ${fR(x.hi)} 跨 0`, hot: true } : { text: '账本判区间跨 0', hot: true }, { text: nTxt }] }
  // 冷冻有三个来源 (strategy_ledger.classify): 复核连续为负 / 区间上限 < 0 / 延续上月冷冻。账本切片不带「哪条触发」,
  // 只有拿到区间上限且 < 0 才能指名; 否则如实写未返回, 不猜。
  if (tier === '冷冻') return { word: '换下', off: true, parts: ci && x.hi! < 0
    ? [{ text: `区间上限 ${fR(x.hi)} < 0`, hot: true }, { text: `超额 ${fR(x.excess)}` }, { text: nTxt }]
    : [{ text: FROZEN_UNKNOWN, hot: true }, { text: `超额 ${fR(x.excess)}` }, { text: nTxt }] }
  if (tier === '待定') {
    const parts: ReasonPart[] = []
    if (x.n != null && x.n < 30) parts.push({ text: `样本 ${x.n} < 30`, hot: true })
    if (x.excess != null && x.excess <= 0) parts.push({ text: `超额 ${fR(x.excess)} ≤ 0`, hot: true })
    if (!parts.length) parts.push({ text: x.excess == null ? '账本无记录' : '证据不足', hot: true }, { text: nTxt })
    return { word: '待定', off: true, parts }
  }
  return { word: '挑战', off: false, parts: [ci ? { text: `区间 ${fR(x.lo)} ~ ${fR(x.hi)}` } : { text: `超额 ${fR(x.excess)}` }, { text: nTxt }] }
}

/** 同一张表共用的刻度: 按数据收紧 (含 0, 两侧留 12%), 最小跨度 0.1R —— 不再用固定的全幅尺子把小区间挤成一段 */
export function ciScale(vals: (number | null | undefined)[]): { min: number; max: number } {
  const xs = [0, ...vals.filter(fin)]
  let min = Math.min(...xs), max = Math.max(...xs)
  if (max - min < 0.1) { const c = (max + min) / 2; min = Math.min(c - 0.05, 0); max = Math.max(c + 0.05, 0) }
  const pad = (max - min) * 0.12
  return { min: min - pad, max: max + pad }
}

// ───────────────────────────── 卡 ─────────────────────────────
export const isObserve = (c: Pick<SwingBoardCard, 'grade' | 'blocked' | 'timing_tier'>) =>
  c.grade === 'observe' || c.blocked === 'observe_only' || c.timing_tier === 'T1'
export const ANCHOR_SRC_ZH: Record<string, string> = { live: '实时报价', snapshot: '当日快照', premarket: '盘前价', unknown: '来源未知' }
export const TIER_EN_ZH: Record<string, string> = { active: '在用', observe: '观察', revived: '复活', rank: '排序因子' }
export function anchorText(t: CardT1Info | null | undefined): string {
  if (!t || !fin(t.anchor_price)) return '锚定价 —'
  const src = ANCHOR_SRC_ZH[String(t.anchor_source ?? 'unknown')] ?? String(t.anchor_source)
  return `锚定 ${t.anchor_price.toFixed(2)} · ${src}${t.anchor_at && t.anchor_at.length >= 16 ? ` ${t.anchor_at.slice(11, 16)}` : ''}`
}
/** gate_id「<方向门>+4h.<event|state|none>@tight-v1」→ 中文 */
export function gateText(g: string | null | undefined): string {
  if (!g) return '—'
  const m = /^(.+?)\+4h\.(event|state|none)@(.+)$/.exec(g)
  if (!m) return g
  return `${m[1] === 'unknown' ? '方向门未知' : m[1]} · 4h ${({ event: '窗口条件齐备', state: '方向同向', none: '未过' } as Record<string, string>)[m[2]]} · ${m[3]}`
}
