/** 期权多空力量热力图 (只展示)。横轴到期日, 纵轴现价附近行权价; 红 = 看涨持仓多, 绿 = 看跌持仓多。点格子看明细。 */
import { Fragment, useState } from 'react'
import type { OptionsHeatCell, OptionsHeatmap as HM } from '@/lib/api'

const $ = (x: number) => `$${x % 1 === 0 ? x.toFixed(0) : x.toFixed(1)}`

export function heatSentence(h: HM): string {
  const head = h.stance === '偏多' && h.call_focus
    ? `偏多：看涨期权集中在 ${$(h.call_focus[0])}–${$(h.call_focus[1])}`
    : h.stance === '偏空' ? '偏空：看跌期权持仓占多' : '多空均衡'
  const walls = [h.resistance != null ? `上方 ${$(h.resistance)} 压力` : '', h.support != null ? `下方 ${$(h.support)} 支撑` : ''].filter(Boolean)
  return walls.length ? `${head} · ${walls.join(' / ')}` : head
}

function cellStyle(c: OptionsHeatCell | null, maxOi: number) {
  if (!c) return undefined
  const strength = 0.15 + 0.7 * Math.min(1, Math.sqrt(c.oi / (maxOi || 1)))
  const a = (Math.abs(c.bias) * strength).toFixed(2)
  return { backgroundColor: c.bias >= 0 ? `hsl(var(--bull) / ${a})` : `hsl(var(--bear) / ${a})` }
}

export function OptionsHeatmap({ h }: { h: HM }) {
  const [pick, setPick] = useState<{ k: number; e: string; c: OptionsHeatCell } | null>(null)
  const maxOi = Math.max(1, ...h.rows.flatMap((r) => r.cells.map((c) => c?.oi ?? 0)))
  // 现价线: 插在第一个低于现价的行权价之前
  const spotIdx = h.rows.findIndex((r) => r.strike < h.spot)
  return (
    <div data-opt-heatmap className="space-y-2">
      <div data-heat-sentence className="text-sm font-medium text-foreground">{heatSentence(h)}</div>
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0.5 text-[10px] tabular-nums">
          <thead>
            <tr>
              <th />
              {h.expirations.map((e) => <th key={e} className="px-1 font-normal text-muted">{e.slice(5)}</th>)}
            </tr>
          </thead>
          <tbody>
            {h.rows.map((r, i) => (
              <Fragment key={r.strike}>
                {i === spotIdx && (
                  <tr key={`spot-${r.strike}`} data-heat-spot>
                    <td className="pr-1 text-right font-medium text-accent">{`现价 ${$(h.spot)}`}</td>
                    <td colSpan={h.expirations.length}><div className="h-0.5 bg-accent" /></td>
                  </tr>
                )}
                <tr>
                  <td className={`pr-1 text-right ${r.strike === h.resistance || r.strike === h.support ? 'font-semibold text-foreground' : 'text-muted'}`}>
                    {$(r.strike)}{r.strike === h.resistance ? ' 压' : r.strike === h.support ? ' 撑' : ''}
                  </td>
                  {r.cells.map((c, j) => (
                    <td key={j}>
                      <button type="button" data-heat-cell={`${r.strike}|${h.expirations[j]}`} disabled={!c}
                        onClick={() => c && setPick({ k: r.strike, e: h.expirations[j], c })}
                        style={cellStyle(c, maxOi)}
                        className={`block h-7 w-full min-w-[2.25rem] rounded-sm border ${pick && pick.k === r.strike && pick.e === h.expirations[j] ? 'border-foreground' : 'border-border/40'} ${c ? '' : 'bg-elevated/40'}`} />
                    </td>
                  ))}
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-3 text-[10px] text-muted">
        <span><span className="mr-1 inline-block h-2 w-3 rounded-sm bg-bull/70 align-middle" />看涨多</span>
        <span><span className="mr-1 inline-block h-2 w-3 rounded-sm bg-bear/70 align-middle" />看跌多</span>
        <span>颜色越深持仓越大 · 点格子看明细</span>
      </div>
      {pick && (
        <div data-heat-detail className="rounded border border-border/60 px-2 py-1.5 text-[11px] tabular-nums">
          {`${pick.e} · 行权价 ${$(pick.k)}：看涨 ${pick.c.call_oi.toLocaleString()} 张 / 看跌 ${pick.c.put_oi.toLocaleString()} 张（持仓）；今日成交 看涨 ${pick.c.call_vol.toLocaleString()} / 看跌 ${pick.c.put_vol.toLocaleString()}`}
        </div>
      )}
    </div>
  )
}
