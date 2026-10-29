// 0.39.0 机会页系统文字守门 (10-25 产品决策: 专业简洁, 不口语化)。
// 机会页相关源码里的中文字面量不含禁用词 (注释除外)。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

export const BANNED = ['今天不会', '不会再', '不用操作', '不用管', '先回区先得', '才释放', '也不成交', '就退出', '照着追', '没拿到', '没出卡',
  '今日不开', '本卡不开', '两边都不开', '能不能', '接下来', '建议', '暂无', '还有 ', '一下', '吧', '呢', '啦', '哦', '。']

it('机会页源码里的中文字面量不含禁用词 (注释不算)', () => {
  const files = ['components/opportunities/Desk.tsx', 'components/opportunities/StatusRail.tsx', 'components/opportunities/terms.ts',
    'pages/Opportunities.tsx', 'components/opportunities/RulesConsole.tsx']
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
