import { expect, it } from 'vitest'
import { shortCompanyName } from './companyName'

it('ui-audit-1011: 公司名去掉证券类别尾巴, 保留股份类别', () => {
  expect(shortCompanyName('NVIDIA Corporation Common Stock')).toBe('NVIDIA Corporation')
  expect(shortCompanyName('Alphabet Inc. Class A Common Stock')).toBe('Alphabet Inc. Class A')
  expect(shortCompanyName('Alibaba Group Holding Limited American Depositary Shares, each represents eight Ordinary Shares')).toBe('Alibaba Group Holding Limited')
  expect(shortCompanyName('Arm Holdings plc American Depositary Shares')).toBe('Arm Holdings plc')
  expect(shortCompanyName('Spotify Technology S.A.')).toBe('Spotify Technology S.A.')
  expect(shortCompanyName('BERKSHIRE HATHAWAY Class B')).toBe('BERKSHIRE HATHAWAY Class B')
  expect(shortCompanyName('Common Stock')).toBe('Common Stock')
  expect(shortCompanyName('Some Corp 6.5% Series A Preferred Stock')).toBe('Some Corp 6.5% Series A Preferred Stock')
  expect(shortCompanyName(null)).toBe('')
})
