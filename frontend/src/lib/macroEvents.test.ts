import { expect, it } from 'vitest'
import { eventTitleZh } from './macroEvents'

it('常见宏观事件有中文名; 没有对照的保留英文原名', () => {
  expect(eventTitleZh('FOMC Member Waller Speaks')).toBe('美联储 Waller 讲话')
  expect(eventTitleZh('Fed Chair Powell Testifies')).toBe('美联储主席 Powell 作证')
  expect(eventTitleZh('10-y Bond Auction')).toBe('10 年期国债拍卖')
  expect(eventTitleZh('NFIB Small Business Index')).toBe('NFIB 小企业信心指数')
  expect(eventTitleZh('Some Unknown Release')).toBe('Some Unknown Release')
})
