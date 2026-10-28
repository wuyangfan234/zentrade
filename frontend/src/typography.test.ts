// 全局字体守门：显式字重必须走 100 步进的标准档位。
// 机会页曾用 550/650/750；浏览器会把英文与中文分别映射到不同实际字重。
import { readdirSync, readFileSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { expect, it } from 'vitest'

const SRC_ROOT = resolve(__dirname)
const STANDARD_WEIGHTS = new Set([100, 200, 300, 400, 500, 600, 700, 800, 900])

function productionSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(dir, entry.name)
    if (entry.isDirectory()) return productionSources(path)
    if (!/\.(?:ts|tsx|css)$/.test(entry.name) || /\.test\.[^.]+$/.test(entry.name)) return []
    return [path]
  })
}

it('production typography uses only standard 100-step numeric font weights', () => {
  const offenders: string[] = []
  for (const file of productionSources(SRC_ROOT)) {
    const lines = readFileSync(file, 'utf8').split('\n')
    lines.forEach((line, index) => {
      if (!/fontWeight|font-weight|font-\[\d{3}\]/.test(line)) return
      for (const match of line.matchAll(/\b\d{3}\b/g)) {
        const weight = Number(match[0])
        if (!STANDARD_WEIGHTS.has(weight)) {
          offenders.push(`${relative(SRC_ROOT, file)}:${index + 1} uses ${weight}`)
        }
      }
    })
  }
  expect(offenders).toEqual([])
})
