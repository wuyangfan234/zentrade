// 把 frontend/src/components/opportunities/__fixtures__/p1scenes.ts 导成 JSON (用 Vite ssrLoadModule, 路径别名照常解析)。
// 用法: cd frontend && node ../docs/ui-shots/tools/dump-scenes.mjs <out.json>
import { createRequire } from 'node:module'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const fe = resolve(process.cwd())
const req = createRequire(resolve(fe, 'package.json'))
const { createServer } = await import(pathToFileURL(resolve(req.resolve('vite/package.json'), '..', 'dist/node/index.js')).href)
const server = await createServer({ root: fe, configFile: resolve(fe, 'vite.config.ts'), server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'error' })
try {
  const m = await server.ssrLoadModule('/src/components/opportunities/__fixtures__/p1scenes.ts')
  const lab = JSON.parse(readFileSync(resolve(fe, 'src/pages/factors/__fixtures__/factor-lab-page.json'), 'utf8'))
  const out = { OPP: m.OPP, RULES_FULL: m.RULES_FULL, RULES_SPARSE: m.RULES_SPARSE, LAB: lab }
  writeFileSync(process.argv[2], JSON.stringify(out))
  console.log('scenes', Object.keys(m.OPP).join(','))
} finally { await server.close() }
