// 0.39.0 机会页系统文字守门 (10-25 产品决策: 专业简洁, 不口语化)。
// 机会页相关源码里的中文字面量不含禁用词 (注释除外)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { cardVerdict, setupKind, zonePos } from './terms'

export const BANNED = ['今天不会', '不会再', '不用操作', '不用管', '先回区先得', '才释放', '也不成交', '就退出', '照着追', '没拿到', '没出卡',
  '今日不开', '本卡不开', '两边都不开', '能不能', '接下来', '建议', '暂无', '还有 ', '一下', '吧', '呢', '啦', '哦', '。']

it('机会页源码里的中文字面量不含禁用词 (注释不算)', () => {
  const files = ['components/opportunities/Desk.tsx', 'components/opportunities/StatusRail.tsx', 'components/opportunities/terms.ts',
    'pages/Opportunities.tsx', 'components/opportunities/RulesConsole.tsx', 'components/opportunities/LsRank.tsx',
    'components/opportunities/MarketHero.tsx', 'components/opportunities/Cards.tsx', 'components/opportunities/SideRail.tsx',
    'components/opportunities/PriceRuler.tsx', 'components/opportunities/AutoSelect.tsx']
  for (const f of files) {
    const src = readFileSync(resolve(__dirname, '..', '..', f), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    const lits = [...src.matchAll(/(['"`])((?:(?!\1)[^\\\n]|\\.)*[\u4e00-\u9fff](?:(?!\1)[^\\\n]|\\.)*)\1/g)].map((m) => m[2])
      .concat([...src.matchAll(/>([^<>{}\n]*[\u4e00-\u9fff][^<>{}\n]*)</g)].map((m) => m[1]))
    for (const s of lits) {
      if (s.includes('禁用')) continue                    // 守门清单本身
      for (const b of BANNED.filter((x) => x !== '。')) expect(s, `${f}: ${b} in ${s}`).not.toContain(b)
    }
  }
})

it('机会板卡结论: 以后端裁决为准, 不做写白话原因; 没有裁决的旧载荷按 blocked 判', () => {
  expect(cardVerdict({ verdict: 'order', state: 'in_zone' })).toEqual({ order: true, label: '已到进场价', detail: '' })
  expect(cardVerdict({ verdict: 'order', state: 'pending', dist_pct: -1.44 })).toEqual({ order: true, label: '等进场', detail: '还差 1.4%' })
  expect(cardVerdict({ verdict: 'order', state: 'pending', dist_pct: null }).detail).toBe('等触发')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'premarket_beyond_stop', dist_pct: -3 })).toMatchObject({ order: false, label: '不做', detail: '盘前已破止损' })
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'cooldown', blocked: 'cooldown' }).detail).toBe('刚卖出 暂不买回')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'holding_other' }).detail).toBe('已持仓')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'direction_conflict' }).detail).toBe('多空冲突')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'entry_closed' }).detail).toBe('今日入场已截止')
  // 新原因码不直接上屏: 写「其他」, 原码留在悬停, 不吞掉
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'x_new_code' })).toMatchObject({ detail: '其他', title: 'x_new_code' })
  // 交易环给的英文准入码换成中文
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch', paper_reason: '准入阻断：direction_conflict；sector_side_cap' }).detail).toBe('多空冲突')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch', paper_reason: '准入阻断：source_not_tradable:phantom_4h' }).detail).toBe('该来源未开放下单')
  const pw = cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch', paper_reason: '准入阻断：单日开仓已满; 现金不足' })
  expect(pw.detail).toBe('单日开仓已满')
  expect(pw.title).toBe('准入阻断：单日开仓已满; 现金不足')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch' }).detail).toBe('账户限制')
  expect(cardVerdict({ blocked: 'earnings', state: 'pending' })).toMatchObject({ order: false, detail: '临近财报' })
  expect(cardVerdict({ state: 'pending', dist_pct: -2 }).order).toBe(true)
})

it('现价相对进场区: 与交易环成交条件同式; 穿过整个区 = past (不会成交)', () => {
  expect(zonePos('long', 109.6, [108.2, 110.4])).toEqual({ pos: 'in', gap: 0 })
  expect(zonePos('long', 1443.5, [1401.99, 1414.64]).pos).toBe('wait')
  expect(zonePos('long', 1443.5, [1401.99, 1414.64]).gap).toBeCloseTo(2.0, 1)
  expect(zonePos('long', 100, [101, 102]).pos).toBe('past')               // 多头跌破区下沿
  expect(zonePos('short', 115.88, [120.58, 125.28]).pos).toBe('wait')
  expect(zonePos('short', 115.88, [120.58, 125.28]).gap).toBeCloseTo(4.06, 2)
  expect(zonePos('short', 130, [120.58, 125.28]).pos).toBe('past')        // 空头涨破区上沿
  expect(zonePos('long', null, [1, 2])).toEqual({ pos: null, gap: null })
})

it('策略 → 白话类型: 现行 3+3 逐个对照; 未知按名字归类; 都不中给原名', () => {
  expect(setupKind([{ id: 'ibs_mean_reversion', name: 'IBS 均值回归' }], 'long')).toBe('超跌反弹')
  expect(setupKind([{ id: 'tv_halftrend', name: 'HalfTrend 翻空' }], 'short')).toBe('趋势转空')
  expect(setupKind([{ id: 'pullback_ma20_bounce', name: 'x' }], 'long')).toBe('回调买入')
  expect(setupKind([{ id: 'zzz', name: '某策略' }], 'long')).toBe('某策略')
  expect(setupKind([], 'long')).toBe('')
})
