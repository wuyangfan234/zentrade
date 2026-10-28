import { describe, expect, it } from 'vitest'
import type { MinuteKlineRow } from '@/lib/api'
import { computeIntradayAverage, FULL_DAY_TIMES } from '@/lib/intraday-chart'

describe('US intraday chart contract', () => {
  it('computes VWAP from raw share volume', () => {
    const rows: MinuteKlineRow[] = [
      { datetime: '2026-09-25 09:30:00', open: 10, high: 10, low: 10, close: 10, volume: 100, amount: 1000 },
      { datetime: '2026-09-25 09:31:00', open: 20, high: 20, low: 20, close: 20, volume: 100, amount: 2000 },
    ]

    expect(computeIntradayAverage(rows)).toEqual([10, 15])
  })

  it('uses one continuous 390-minute session plus a 16:00 boundary', () => {
    expect(FULL_DAY_TIMES).toHaveLength(391)
    expect(FULL_DAY_TIMES[0]).toBe('09:30')
    expect(FULL_DAY_TIMES[120]).toBe('11:30')
    expect(FULL_DAY_TIMES[150]).toBe('12:00')
    expect(FULL_DAY_TIMES[210]).toBe('13:00')
    expect(FULL_DAY_TIMES[389]).toBe('15:59')
    expect(FULL_DAY_TIMES[390]).toBe('16:00')
  })
})
