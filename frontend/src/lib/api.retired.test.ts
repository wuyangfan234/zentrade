// 0.29.1 旧机会卡链路退役: 前端客户端不得再出现这些接口。
// - /cards /top-snapshot /watchlist(/{symbol}) /swing/strategies: 后端路由已删, 调了只会 404。
// - /swing /intraday /market-mood: 后端只读路由保留 (系统状态页链接 /intraday、排障用), 但机会页只走 /page, 客户端不再封装。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { api } from './api'

const RETIRED = ['watchlistCards', 'topSnapshot', 'watchlistTable', 'watchlistRow', 'swingBoards', 'intradayBoard', 'marketMood',
  'saveSwingStrategies', 'saveWatchlistRules'] as const

it('retired opportunity endpoints are not in the api client', () => {
  for (const k of RETIRED) expect(k in api, k).toBe(false)
  const src = readFileSync(resolve(__dirname, 'api.ts'), 'utf-8')
  for (const path of ["'/api/opportunities/cards'", '/api/opportunities/top-snapshot', "'/api/opportunities/watchlist'",
    '/api/opportunities/watchlist/$', '/api/opportunities/swing/strategies']) {
    expect(src.includes(path), path).toBe(false)
  }
  expect(src).toContain("'/api/opportunities/page'")
  expect(src).toContain("'/api/opportunities/settings'")
})
