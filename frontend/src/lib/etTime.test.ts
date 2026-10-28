// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { addCalendarDays, calendarDayDiff, etNaiveToDate, etToday, fmtRelativeEt } from './etTime'

describe('etNaiveToDate: 美东墙钟 naive 串 → 绝对时刻', () => {
  it('夏令时 (EDT, UTC-4)', () => {
    expect(etNaiveToDate('2026-09-30T01:36:34')?.toISOString()).toBe('2026-09-30T05:36:34.000Z')
  })
  it('冬令时 (EST, UTC-5)', () => {
    expect(etNaiveToDate('2026-12-01T09:30:00')?.toISOString()).toBe('2026-12-01T14:30:00.000Z')
  })
  it('已带偏移的串保持原样; 空/非法返回 null', () => {
    expect(etNaiveToDate('2026-09-30T01:36:34Z')?.toISOString()).toBe('2026-09-30T01:36:34.000Z')
    expect(etNaiveToDate('')).toBeNull()
    expect(etNaiveToDate('abc')).toBeNull()
  })
})

describe('fmtRelativeEt', () => {
  it('刚生成的报告显示「刚刚」, 不因浏览器在北京而差 12 小时', () => {
    const now = new Date('2026-10-02T01:30:20Z') // = 美东 10-01 21:30:20
    expect(fmtRelativeEt('2026-10-01T21:30:00', now)).toBe('刚刚')
  })
  it('按真实间隔计小时/天', () => {
    const now = new Date('2026-10-02T01:37:00Z')
    expect(fmtRelativeEt('2026-09-30T01:36:34', now)).toBe('1 天前')
    expect(fmtRelativeEt('2026-10-01T18:30:00', now)).toBe('3 小时前')
  })
})

describe('etToday', () => {
  it('北京上午 = 美东前一日晚上', () => {
    expect(etToday(new Date('2026-10-02T01:37:00Z'))).toBe('2026-10-01')
  })
})

describe('日历日与浏览器时区无关', () => {
  it('加减与差值', () => {
    expect(addCalendarDays('2026-10-15', 1)).toBe('2026-10-16')
    expect(addCalendarDays('2026-10-01', -1)).toBe('2026-09-30')
    expect(calendarDayDiff('2026-10-15', '2026-10-16')).toBe(-1)
  })
})
