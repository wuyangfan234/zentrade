/** 95% 区间条 (2026-11-26 用户指定细节): 机会页账本与因子页槽位共用。
 *  - 刻度由调用方按整张表数据收紧 (p1.ciScale), 全表同一把尺子;
 *  - 0 基准 = 实线贯穿全高, 一眼看出跨没跨 0;
 *  - 区间整体 > 0 实色, 整体 < 0 熊色, 跨 0 = 负段琥珀斜纹 + 正段浅色 (不只靠颜色: data-ci-cross + 端点数字);
 *  - 区间宽度最小 MIN_PX 像素, 端点标数值 (窄时合成一个标签)。 */
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const MINUS = '−'
const n2 = (v: number) => { const a = Math.abs(v).toFixed(2); return `${Number(a) === 0 ? '' : v > 0 ? '+' : MINUS}${a}` }
export const CI_MIN_PX = 8

export function CiBar({ v, lo, hi, min, max, width = 176, strong = false, dim = false }: {
  v: number | null; lo: number | null; hi: number | null; min: number; max: number; width?: number; strong?: boolean; dim?: boolean
}) {
  const W = width, H = 30, PAD = 6, Y = 21
  const x = (t: number) => PAD + ((Math.max(min, Math.min(max, t)) - min) / (max - min || 1)) * (W - PAD * 2)
  const zero = x(0)
  const hasCi = fin(lo) && fin(hi)
  let x1 = hasCi ? x(lo) : 0, x2 = hasCi ? x(hi) : 0
  if (hasCi && x2 - x1 < CI_MIN_PX) { const c = (x1 + x2) / 2; x1 = c - CI_MIN_PX / 2; x2 = c + CI_MIN_PX / 2 }
  const cross = hasCi && lo! < 0 && hi! > 0
  const pos = hasCi && lo! > 0
  const main = dim ? 'hsl(var(--fg-muted))' : strong ? 'rgb(124 58 237)' : 'hsl(var(--accent))'
  const fill = pos ? main : hasCi && hi! < 0 ? 'hsl(var(--bear))' : main
  const id = `ci-hatch-${Math.round(x1)}-${Math.round(x2)}`
  const narrow = x2 - x1 < 46
  return (
    <svg width={W} height={H} role="img" aria-label={hasCi ? `95% 区间 ${n2(lo!)} ~ ${n2(hi!)}${cross ? ' 跨 0' : ''}` : '区间未返回'}
      data-ci-bar="" data-ci-cross={hasCi ? (cross ? '1' : '0') : ''} data-ci-w={hasCi ? (x2 - x1).toFixed(1) : ''}>
      <defs><pattern id={id} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="2" height="4" fill="hsl(var(--warning))" /></pattern></defs>
      <line x1={PAD} x2={W - PAD} y1={Y} y2={Y} stroke="hsl(var(--border))" />
      {hasCi && (cross ? <>
        <rect x={x1} y={Y - 4} width={Math.max(0, zero - x1)} height={8} fill={`url(#${id})`} stroke="hsl(var(--warning))" strokeWidth={1} rx={1.5} />
        <rect x={zero} y={Y - 4} width={Math.max(0, x2 - zero)} height={8} fill={main} fillOpacity={0.45} rx={1.5} />
      </> : <rect x={x1} y={Y - 4} width={x2 - x1} height={8} fill={fill} rx={2} />)}
      <line x1={zero} x2={zero} y1={11} y2={H} stroke="hsl(var(--fg-primary))" strokeOpacity={0.75} strokeWidth={2} data-ci-zero="" />
      {fin(v) && <circle cx={x(v)} cy={Y} r={3.5} fill="hsl(var(--surface))" stroke={dim ? 'hsl(var(--fg-muted))' : 'hsl(var(--fg-primary))'} strokeWidth={1.8} />}
      {hasCi && (narrow
        ? <text x={(x1 + x2) / 2} y={9} fontSize="10" textAnchor="middle" fill="hsl(var(--fg-secondary))" data-ci-label="">{`${n2(lo!)}~${n2(hi!)}`}</text>
        : <>
          <text x={x1} y={9} fontSize="10" textAnchor="start" fill="hsl(var(--fg-secondary))" data-ci-label="">{n2(lo!)}</text>
          <text x={x2} y={9} fontSize="10" textAnchor="end" fill="hsl(var(--fg-secondary))" data-ci-label="">{n2(hi!)}</text>
        </>)}
      {!hasCi && <text x={W - PAD} y={9} fontSize="10" textAnchor="end" fill="hsl(var(--fg-muted))">区间未返回</text>}
    </svg>
  )
}

/** 表头刻度: 两端 + 0 */
export function CiAxis({ min, max, width = 176 }: { min: number; max: number; width?: number }) {
  const PAD = 6
  const x = (t: number) => PAD + ((t - min) / (max - min || 1)) * (width - PAD * 2)
  return (
    <svg width={width} height={14} aria-hidden="true" data-ci-axis="">
      {x(0) - PAD > 30 && <text x={PAD} y={11} fontSize="10" textAnchor="start" fill="hsl(var(--fg-secondary))">{n2(min)}</text>}
      <text x={x(0)} y={11} fontSize="10.5" fontWeight="700" textAnchor="middle" fill="hsl(var(--fg-primary))">0</text>
      {width - PAD - x(0) > 30 && <text x={width - PAD} y={11} fontSize="10" textAnchor="end" fill="hsl(var(--fg-secondary))">{n2(max)}</text>}
    </svg>
  )
}
