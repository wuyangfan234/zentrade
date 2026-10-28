// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { cnDateTimeFromUtc, formatLogTime, isToday } from './format'

// 后端自选 added_at 是 datetime.utcnow() 写入的 naive UTC 串 (无 Z 后缀)。
// 若按 JS 默认的本地时区解析, 非北京时区会显示错日; 北京时间 00:00-08:00 加入的
// 记录则会被显示成前一天。以下用例在任何时区的机器上都必须通过。
describe('cnDateTimeFromUtc: naive UTC 串 → 北京时间', () => {
  it('跨 UTC 日界: 北京凌晨加入的不能显示成前一天', () => {
    // UTC 09-18 20:30 = 北京 09-19 04:30
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00')).toBe('2026-09-19 04:30:00')
  })

  it('常规时段换算 (+8 小时)', () => {
    expect(cnDateTimeFromUtc('2026-09-19T03:21:07')).toBe('2026-09-19 11:21:07')
  })

  it('北京午夜显示 00:00 而非 24:00', () => {
    expect(cnDateTimeFromUtc('2026-09-18T16:00:00')).toBe('2026-09-19 00:00:00')
  })

  it('已带时区标记的串按原语义解析', () => {
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00Z')).toBe('2026-09-19 04:30:00')
    expect(cnDateTimeFromUtc('2026-09-19T04:30:00+08:00')).toBe('2026-09-19 04:30:00')
  })

  it('空值与非法值返回空串 (不抛异常、不产生 Invalid Date)', () => {
    for (const v of ['', '   ', null, undefined, 'garbage', 'not-a-date']) {
      expect(cnDateTimeFromUtc(v)).toBe('')
    }
  })
})

describe('美东日历, 不随浏览器本地日', () => {
  it('北京 10-16 05:10 仍是美东 10-15', () => {
    const now = new Date('2026-10-15T21:10:00Z')
    expect(isToday('2026-10-15T13:10:10Z', now)).toBe(true)
    expect(isToday('2026-10-16T05:00:00Z', now)).toBe(false)
  })
  it('日志时间标 ET', () => {
    expect(formatLogTime('2026-10-15T20:35:00Z')).toBe('16:35:00 ET')
  })
})
