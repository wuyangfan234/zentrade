import { expect, it, vi } from 'vitest'

vi.mock('echarts-for-react', () => ({ default: () => null }))
import { formatAmount } from './EChartsMultiDayIntraday'

it('ui-audit-1011: 多日分时成交额 1亿 = 1e8 (与单日分时一致)', () => {
  expect(formatAmount(18_830_000_000)).toBe('188.30亿')
  expect(formatAmount(250_000_000)).toBe('2.50亿')
  expect(formatAmount(56_000_000)).toBe('5600万')
  expect(formatAmount(null)).toBe('—')
})
