// audit1026 b3c L-10: 自选加入日按美东日 (与后端 _added_dates_et 区间收益基准日同口径)
import { describe, expect, it } from 'vitest'
import { etDateFromUtc } from './format'

describe('etDateFromUtc: naive UTC 串 → 美东日期', () => {
  it('美东下午加入 (北京已是次日) 仍按美东当天', () => {
    // ASTS 生产样本: UTC 19:35 = 美东 10-01 15:35 = 北京 10-02 03:35
    expect(etDateFromUtc('2026-10-01T19:35:19')).toBe('2026-10-01')
  })
  it('UTC 已过零点但美东仍是前一天', () => {
    expect(etDateFromUtc('2026-10-02T03:30:00')).toBe('2026-10-01')
  })
  it('冬令时 (EST, UTC-5)', () => {
    expect(etDateFromUtc('2026-12-01T04:59:00')).toBe('2026-11-30')
    expect(etDateFromUtc('2026-12-01T05:00:00')).toBe('2026-12-01')
  })
  it('已带偏移的串原样解析, 空/非法返回空串', () => {
    expect(etDateFromUtc('2026-10-01T19:35:19Z')).toBe('2026-10-01')
    for (const v of [null, undefined, '', 'x']) expect(etDateFromUtc(v)).toBe('')
  })
})
