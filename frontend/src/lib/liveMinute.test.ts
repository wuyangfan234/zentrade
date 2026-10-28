import { describe, expect, it } from 'vitest'
import { usLiveMinute } from './liveMinute'

describe('usLiveMinute (watchlist intraday live extension)', () => {
  it('uses America/New_York regular session regardless of browser timezone', () => {
    // 2026-10-01 10:15:30 ET (Thu) = 22:15:30 Beijing
    const r = usLiveMinute(new Date('2026-10-01T14:15:30Z'))
    expect(r).toEqual({ inSession: true, barTs: '2026-10-01T10:15:00' })
  })

  it('covers the whole continuous session with no lunch break', () => {
    expect(usLiveMinute(new Date('2026-10-01T16:45:00Z')).inSession).toBe(true)   // 12:45 ET
    expect(usLiveMinute(new Date('2026-10-01T19:59:00Z')).inSession).toBe(true)   // 15:59 ET
  })

  it('starts at the 09:30 bar (bars are start-labeled) and is closed from 16:00', () => {
    expect(usLiveMinute(new Date('2026-10-01T13:29:59Z')).inSession).toBe(false)  // 09:29:59 ET
    expect(usLiveMinute(new Date('2026-10-01T13:30:30Z'))).toEqual({ inSession: true, barTs: '2026-10-01T09:30:00' })
    expect(usLiveMinute(new Date('2026-10-01T20:00:00Z')).inSession).toBe(false)  // 16:00 ET
    expect(usLiveMinute(new Date('2026-10-03T15:00:00Z')).inSession).toBe(false)  // Saturday
  })

  it('handles winter time (EST, UTC-5)', () => {
    expect(usLiveMinute(new Date('2026-12-01T14:31:00Z'))).toEqual({ inSession: true, barTs: '2026-12-01T09:31:00' })
  })
})
