/** 净值曲线纵轴刻度 (总览槽位 2 与采集页候选图共用)。
 * 步长取 1 / 2 / 5 × 10^k, 不小于 0.5, 整条轴约 6 条以内; 范围内有 1.00 (净值起点) 时保留这一条。
 * 只返回严格落在 (mn, mx) 之间的刻度。 */
export function navTicks(mn: number, mx: number): number[] {
  const span = mx - mn
  if (!Number.isFinite(span) || span <= 0) return []
  const raw = span / 6
  const pow = 10 ** Math.floor(Math.log10(raw))
  const step = Math.max(0.5, [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? 10 * pow)
  const out: number[] = []
  for (let k = Math.ceil(mn / step); k * step < mx; k++) {
    const t = Math.round(k * step * 1e6) / 1e6
    if (t > mn) out.push(t)
  }
  if (mn < 1 && mx > 1 && !out.includes(1)) out.unshift(1)
  return out.sort((a, b) => a - b)
}
