import { afterEach, describe, expect, it, vi } from 'vitest'
import { minuteRefetchInterval } from '@/lib/kline'
import { QK } from '@/lib/queryKeys'

afterEach(() => {
  vi.useRealTimers()
})

describe('US minute polling clock', () => {
  it('continues during the ET afternoon even after Beijing midnight', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T17:34:00.000Z')) // 13:34 ET / 01:34 Beijing

    const interval = minuteRefetchInterval(6000)({
      state: {
        data: {
          date: '2026-09-30',
          rows: [{ datetime: '2026-09-30 13:34:00' }],
          source: 'live',
        },
      },
    })

    expect(interval).toBe(6000)
  })

  it('stops after the 15:59 bar is present and the explicitly requested ET session is closed', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T20:00:00.000Z')) // 16:00 ET

    const interval = minuteRefetchInterval(6000)({
      queryKey: QK.klineMinute('AAPL', '2026-09-30'),
      state: {
        data: {
          date: '2026-09-30',
          rows: [{ datetime: '2026-09-30 15:59:00' }],
          source: 'live',
        },
      },
    })

    expect(interval).toBe(false)
  })

  it.each([undefined, 0, -1, NaN, Infinity])('disables polling for an invalid interval (%s)', (ms) => {
    expect(minuteRefetchInterval(ms)({ state: {} })).toBe(false)
  })

  it.each(['2026-09-30T13:25:00Z', '2026-09-30T13:40:00Z'])('keeps waiting for source=none before SIP minutes become available at %s', (now) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(now))
    const query = {
      queryKey: QK.klineMinute('AAPL', '2026-09-30'),
      state: { data: { date: '2026-09-30', source: 'none', rows: [] } },
    }
    expect(minuteRefetchInterval(6000)(query)).toBe(60_000)

    // The same mounted query resumes the requested cadence when delayed bars arrive.
    vi.setSystemTime(new Date('2026-09-30T13:46:00Z'))
    expect(minuteRefetchInterval(6000)({
      ...query,
      state: { data: { date: '2026-09-30', source: 'live', rows: [{ datetime: '2026-09-30 09:30:00' }] } },
    })).toBe(6000)
  })

  it.each(['', '2026-09-30'])('does not freeze a current/latest request (%s) on yesterday’s fallback', (date) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T13:40:00Z')) // 09:40 ET, SIP is still before the open
    expect(minuteRefetchInterval(6000)({
      queryKey: QK.klineMinute('AAPL', date),
      state: { data: { date: '2026-09-29', source: 'local', rows: [{ datetime: '2026-09-29 15:59:00' }] } },
    })).toBe(60_000)
  })

  it.each([undefined, { date: '2026-09-29', source: 'none', rows: [] }])('only stops an explicitly requested historical date, including missing data', (data) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T13:40:00Z'))
    expect(minuteRefetchInterval(6000)({
      queryKey: QK.klineMinute('AAPL', '2026-09-29'),
      state: { data },
    })).toBe(false)
  })

  it.each(['2026-09-30T20:00:00Z', '2026-09-30T20:17:00Z', '2026-09-30T22:00:00Z'])('keeps a slow catch-up poll after the close while the tail is missing at %s', (now) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(now))
    const query = {
      queryKey: QK.klineMinute('AAPL', '2026-09-30'),
      state: { data: { date: '2026-09-30', source: 'local', rows: [{ datetime: '2026-09-30 15:44:00' }] } },
    }
    expect(minuteRefetchInterval(6000)(query)).toBe(60_000)
    expect(minuteRefetchInterval(120_000)(query)).toBe(120_000)
  })

  it.each(['2026-09-30 16:00:00', '2026-09-30 16:10:00', '2026-09-29 15:59:00'])('does not mistake %s for today’s closing minute', (datetime) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T20:20:00Z'))
    expect(minuteRefetchInterval(6000)({
      queryKey: QK.klineMinute('AAPL', '2026-09-30'),
      state: { data: { date: '2026-09-30', source: 'local', rows: [{ datetime }] } },
    })).toBe(60_000)
  })

  it('keeps latest queries alive across the close and next opening session', () => {
    vi.useFakeTimers()
    const query = {
      queryKey: QK.klineMinute('AAPL', ''),
      state: { data: { date: '2026-09-30', source: 'local', rows: [{ datetime: '2026-09-30 15:59:00' }] } },
    }
    vi.setSystemTime(new Date('2026-09-30T20:20:00Z'))
    expect(minuteRefetchInterval(6000)(query)).toBe(300_000)
    vi.setSystemTime(new Date('2026-10-01T13:40:00Z'))
    expect(minuteRefetchInterval(6000)(query)).toBe(60_000)
  })

  it('uses the ET date during winter, even when UTC is already the next day', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-12-02T00:05:00Z')) // 19:05 ET on Dec 1
    expect(minuteRefetchInterval(6000)({
      queryKey: QK.klineMinute('AAPL', '2026-12-01'),
      state: { data: { date: '2026-12-01', source: 'none', rows: [] } },
    })).toBe(60_000)
  })
})
