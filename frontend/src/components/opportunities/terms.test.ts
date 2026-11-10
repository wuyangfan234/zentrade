// 0.39.0 机会页系统文字守门 (10-25 产品决策: 专业简洁, 不口语化)。
// 机会页相关源码里的中文字面量不含禁用词 (注释除外)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { cardVerdict } from './terms'

export const BANNED = ['今天不会', '不会再', '不用操作', '不用管', '先回区先得', '才释放', '也不成交', '就退出', '照着追', '没拿到', '没出卡',
  '今日不开', '本卡不开', '两边都不开', '能不能', '接下来', '建议', '暂无', '还有 ', '一下', '吧', '呢', '啦', '哦', '。']

it('机会页源码里的中文字面量不含禁用词 (注释不算)', () => {
  const files = ['components/opportunities/Desk.tsx', 'components/opportunities/StatusRail.tsx', 'components/opportunities/terms.ts',
    'pages/Opportunities.tsx', 'components/opportunities/RulesConsole.tsx', 'components/opportunities/LsRank.tsx',
    'components/opportunities/MoodBar.tsx']
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

it('机会板卡结论: 以后端裁决为准, 不下单写原因; 没有裁决的旧载荷按 blocked 判', () => {
  expect(cardVerdict({ verdict: 'order', state: 'in_zone' })).toEqual({ order: true, label: '下单', detail: '在区' })
  expect(cardVerdict({ verdict: 'order', state: 'pending', dist_pct: -1.44 })).toEqual({ order: true, label: '下单', detail: '距区 1.4%' })
  expect(cardVerdict({ verdict: 'order', state: 'pending', dist_pct: null }).detail).toBe('待触发')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'premarket_beyond_stop', dist_pct: -3 })).toMatchObject({ order: false, label: '不下单', detail: '盘前越止损' })
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'cooldown', blocked: 'cooldown' }).detail).toBe('平仓冷却')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'holding_other' }).detail).toBe('同票持仓')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'direction_conflict' }).detail).toBe('多空冲突')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'x_new_code' }).detail).toBe('x_new_code')            // 新原因码原样显示, 不吞掉
  const pw = cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch', paper_reason: '准入阻断：单日开仓已满; 现金不足' })
  expect(pw.detail).toBe('单日开仓已满')
  expect(pw.title).toBe('准入阻断：单日开仓已满; 现金不足')
  expect(cardVerdict({ verdict: 'skip', verdict_reason: 'paper_watch', blocked: 'paper_watch' }).detail).toBe('准入阻断')
  expect(cardVerdict({ blocked: 'earnings', state: 'pending' })).toMatchObject({ order: false, detail: '财报前一日' })
  expect(cardVerdict({ state: 'pending', dist_pct: -2 }).order).toBe(true)
})
