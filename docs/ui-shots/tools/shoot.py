"""Todo #10 UI 检查: 真浏览器 (Playwright Chromium) 渲染机会页 / 因子页, 拦截 /api/** 喂 dump-scenes.mjs 导出的虚拟数据。

用法: python3 shoot.py <base_url> <scenes.json> <out_dir> [prefix]
  - 双视口 1440x900 / 390x844; 三档密度 (满 / 稀疏 / 空 / 异常);
  - 自检: JS 报错 (console error + pageerror) / 横向溢出 / 新组件文字截断 / 文字重叠 / 对比度 / 计数断言 / 两页 regime 指纹一致;
  - 结果写 <out_dir>/<prefix>check.json, 任一项非 0 → 退出码 1。
兼容 macOS 自带 Python 3.9。"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

assert sys.version_info >= (3, 8), sys.version
BASE, SCENES, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
PREFIX = sys.argv[4] if len(sys.argv) > 4 else ''
OUT.mkdir(parents=True, exist_ok=True)
D = json.loads(SCENES.read_text(encoding='utf-8'))
VIEWS = {'desktop': {'width': 1440, 'height': 900}, 'mobile': {'width': 390, 'height': 844}, 'wide': {'width': 1920, 'height': 1080}}
ONLY = set(filter(None, os.environ.get('ONLY', '').split(',')))  # 只跑这些场景 (调试用)
SCOPE = '[data-regime-bar],[data-ledger],[data-board],[data-slot="board"]'

CHECK_JS = r"""(scopeSel) => {
  const de = document.documentElement
  const res = { overflowX: Math.max(0, de.scrollWidth - de.clientWidth), trunc: [], overlap: [], contrast: [] }
  const scopes = [...document.querySelectorAll(scopeSel)]
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' }
  const ownText = (el) => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1] }
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) }
  const bgOf = (el) => {
    const layers = []
    for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break } }
    let base = [255, 255, 255]
    for (const c of layers.reverse()) base = [0, 1, 2].map(i => c[i] * c[3] + base[i] * (1 - c[3]))
    return base
  }
  for (const sc of scopes) {
    const leaves = [...sc.querySelectorAll('*')].filter(el => !(el instanceof SVGElement) && ownText(el) && vis(el))
    for (const el of leaves) {
      const cs = getComputedStyle(el)
      if ((cs.overflow === 'hidden' || cs.textOverflow === 'ellipsis' || cs.overflowX === 'hidden') && el.scrollWidth > el.clientWidth + 1)
        res.trunc.push(el.textContent.trim().slice(0, 40))
      const fg = parse(cs.color); if (!fg) continue
      const bg = bgOf(el); const f = [0, 1, 2].map(i => fg[i] * fg[3] + bg[i] * (1 - fg[3]))
      const L1 = lum(f), L2 = lum(bg); const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05)
      const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700
      const need = (size >= 24 || (bold && size >= 18.66)) ? 3 : 4.5
      if (ratio < need) res.contrast.push(`${el.textContent.trim().slice(0, 30)} ${ratio.toFixed(2)}<${need}`)
    }
    const rects = leaves.map(el => ({ el, r: el.getBoundingClientRect() }))
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i], b = rects[j]
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue
      const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left), h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top)
      if (w > 1 && h > 1) res.overlap.push(`${a.el.textContent.trim().slice(0, 20)} × ${b.el.textContent.trim().slice(0, 20)}`)
    }
  }
  return res
}"""

COUNT_JS = r"""() => {
  const q = (s) => [...document.querySelectorAll(s)]
  const bad = []
  const eq = (name, a, b) => { if (a !== b) bad.push(`${name}: 页面 ${a} ≠ 实际 ${b}`) }
  const lc = document.querySelector('[data-ledger-count]')
  if (lc) eq('账本计数', Number(lc.textContent), q('[data-ledger-row]').length)
  for (const g of q('[data-ledger-group]')) {
    const t = g.getAttribute('data-ledger-group'), n = g.querySelectorAll('[data-ledger-row]').length
    const chip = document.querySelector(`[data-ledger-tier-count="${t}"]`)
    if (chip) eq(`账本 ${t}`, Number(chip.textContent.trim().split(' ').pop()), n)
  }
  const oc = document.querySelector('[data-observe-count]')
  if (oc) eq('观察级计数', Number(oc.getAttribute('data-observe-count')), q('[data-observe-grid] [data-board-card]').length)
  const bs = document.querySelector('[data-board] h2 + span')
  if (bs) eq('今日机会计数', Number(bs.textContent), q('[data-board-grid] [data-board-card]').length)
  for (const r of q('[data-loop-rule]')) {
    const c = r.querySelector('[data-loop-count]'); eq(`闭环 ${r.getAttribute('data-loop-rule')}`, Number(c.getAttribute('data-loop-count')), r.querySelectorAll('[data-loop-card]').length)
  }
  // 末行不留整列空白: 每个卡片网格最后一张卡的右缘 = 网格内容区右缘
  for (const g of q('[data-grid-fill]')) {
    const kids = [...g.children]; if (!kids.length) continue
    const cs = getComputedStyle(g), gr = g.getBoundingClientRect(), last = kids[kids.length - 1].getBoundingClientRect()
    const right = gr.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth)
    if (right - last.right > 2) bad.push(`末行留白 ${g.getAttributeNames().filter(a => a.startsWith('data-'))[0]}: ${Math.round(right - last.right)}px`)
  }
  // 同一张表的区间条: 0 基准线横坐标一致 (全表同一刻度)
  for (const t of q('table')) {
    const xs = [...t.querySelectorAll('[data-ci-zero]')].map(l => Math.round(l.getBoundingClientRect().left))
    if (xs.length > 1 && Math.max(...xs) - Math.min(...xs) > 1) bad.push(`0 基准未对齐: ${xs.join(',')}`)
  }
  return { bad, fp: document.querySelector('[data-regime-bar]')?.getAttribute('data-regime-fp') ?? null,
    cards: q('[data-board-card]').length, ledger: q('[data-ledger-row]').length, reasons: q('[data-reason-row]').length, observe: q('[data-observe] ').length }
}"""


def payload(path, scene):
    page_kind, sc = scene
    opp = D['OPP'][sc if page_kind == 'opp' or sc != 'sparse' else 'sparse']
    if path == '/api/auth/status':
        return {'configured': True, 'authenticated': True}
    if path == '/api/settings':
        return {'onboarding_completed': True}
    if path == '/api/settings/setup-checklist':
        return {'items': [], 'summary': {'required_missing': [], 'recommended_missing': [], 'ready': True}, 'dismissed': True}
    if path == '/api/opportunities/page':
        return opp
    if path == '/api/opportunities/rules':
        return {'catalog': (D['RULES_SPARSE'] if sc in ('sparse', 'empty') else D['RULES_FULL'])['catalog'], 'ruleset': {'version': 52, 'enabled': []}}
    if path == '/api/opportunities/asof/days':
        return {'today': '2026-11-25', 'days': []}
    if path == '/api/factor-lab/page':
        return D['LAB']
    if path.startswith('/api/opportunities/quotes'):
        return {'quotes': {}, 'at': None}
    # 外围接口 (外壳 / 打分槽位): 最小合法返回, 不让 404 弹提示干扰截图; 本卡不涉及这些区块
    return PERIPHERAL.get(path)


PERIPHERAL = {
    '/api/alerts': {'alerts': [], 'total': 0}, '/api/analysis-menus': {'items': []}, '/api/custom-signals': {'signals': []},
    '/api/data/version': {'version': 'dev'}, '/api/intraday/indices': {'indices': [], 'quotes': {}},
    '/api/intraday/status': {'quote_age_ms': None, 'is_trading_hours': False, 'last_fetch_ms': None},
    '/api/pipeline/jobs': {'items': [], 'jobs': []}, '/api/settings/preferences': {}, '/api/settings/data-sources': {'sources': [], 'items': []},
    '/api/settings/capability-matrix': {'rows': [], 'items': []}, '/api/strategies': {'strategies': []},
    '/api/factors': {'factors': []}, '/api/backtest/mining/runs': {'items': []},
}


def run():
    results, fps = {}, {}
    plan = [('opp', s) for s in ('full', 'two', 'sparse', 'empty', 'anomaly')] + [('factor', s) for s in ('full', 'sparse', 'anomaly')]
    with sync_playwright() as p:
        exe = os.environ.get('PW_CHROMIUM')  # 本机 playwright 包与已缓存浏览器版本不一致时显式指定
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        for kind, sc in plan:
            for vname, vp in VIEWS.items():
                if sc in ('sparse', 'empty', 'two') and vname == 'mobile':
                    continue
                if vname == 'wide' and sc != 'two':  # 宽屏只用来复现「2 张卡占 3 列」
                    continue
                if ONLY and sc not in ONLY:
                    continue
                ctx = br.new_context(viewport=vp, device_scale_factor=2 if vname == 'mobile' else 1, locale='zh-CN', color_scheme='light')
                page = ctx.new_page()
                errs, misses = [], []
                page.on('console', lambda m, errs=errs: errs.append(m.text) if m.type == 'error' else None)
                page.on('pageerror', lambda e, errs=errs: errs.append(f'pageerror {e}'))

                def make(scene, misses):
                    def handle(route):
                        path = route.request.url.split('://', 1)[1].split('/', 1)[1]
                        if path.startswith('api/intraday/stream'):
                            return  # SSE 保持挂起 (不回应): 不触发「正在重连」提示
                        path = '/' + (path if path.startswith('api/opportunities/quotes') else path.split('?')[0])
                        body = payload(path, scene)
                        if body is None:
                            misses.append(path)
                            route.fulfill(status=404, content_type='application/json', body='{"detail":"mock 未提供"}')
                        else:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps(body, ensure_ascii=False))
                    return handle
                handle = make((kind, sc), misses)
                page.route('**/api/**', handle)
                page.goto(BASE + ('/' if kind == 'opp' else '/factors'), wait_until='domcontentloaded')
                sel = '[data-board]' if kind == 'opp' else '[data-slot="board"]'
                try:
                    page.wait_for_selector(sel, timeout=15000)
                except Exception:
                    page.screenshot(path=str(OUT / f'{PREFIX}DEBUG-{kind}-{sc}-{vname}.png'), full_page=True)
                    print('TIMEOUT', kind, sc, vname, page.url, 'misses', sorted(set(misses)), 'errs', errs[:5])
                    raise
                if kind == 'opp' and sc == 'full' and page.locator('[data-attr-toggle="AMD"]').count():
                    page.click('[data-attr-toggle="AMD"]')
                if kind == 'factor' and page.locator('[data-loop-toggle="board"]').count():
                    page.click('[data-loop-toggle="board"]')
                page.wait_for_timeout(400)
                # Layout 的 <main> 是内部滚动容器: 截图前展开成文档流, full_page 才能截到整页 (只作用于截图)
                page.add_style_tag(content='html,body,#root,#root>div{height:auto!important;min-height:0!important;overflow:visible!important}'
                                   'main{height:auto!important;overflow:visible!important}')
                page.wait_for_timeout(200)
                name = f'{PREFIX}{kind}-{sc}-{vname}'
                page.screenshot(path=str(OUT / f'{name}.png'), full_page=True)
                # 细节对照的局部截图
                if kind == 'factor' and sc == 'full' and vname == 'desktop':
                    page.locator('[data-slot="board"]').screenshot(path=str(OUT / f'{PREFIX}detail-ci-reason.png'))
                if kind == 'opp' and sc in ('two', 'full') and vname in ('desktop', 'wide'):
                    page.locator('[data-board]').screenshot(path=str(OUT / f'{PREFIX}detail-grid-{sc}-{vname}.png'))
                chk = page.evaluate(CHECK_JS, SCOPE)
                cnt = page.evaluate(COUNT_JS)
                fps.setdefault(sc, {})[kind] = cnt['fp']
                # 404 的接口只允许是本页用不到的外围接口 (由页面自己的「读取失败」兜住, 不算 JS 报错)
                errs = [e for e in errs if 'Failed to load resource' not in e]
                results[name] = {'js_errors': errs, 'overflowX': chk['overflowX'], 'trunc': chk['trunc'], 'overlap': chk['overlap'],
                                 'contrast': chk['contrast'], 'count_bad': cnt['bad'], 'cards': cnt['cards'], 'ledger_rows': cnt['ledger'],
                                 'reason_rows': cnt['reasons'], 'fp': cnt['fp'], 'mock_404': sorted(set(misses))}
                ctx.close()
        br.close()
    fp_bad = [f'{sc}: 机会页 {v.get("opp")} ≠ 因子页 {v.get("factor")}' for sc, v in fps.items() if 'opp' in v and 'factor' in v and v['opp'] != v['factor']]
    summary = {k: {m: (len(v[m]) if isinstance(v[m], list) else v[m]) for m in ('js_errors', 'overflowX', 'trunc', 'overlap', 'contrast', 'count_bad')} for k, v in results.items()}
    total = sum(sum(x.values()) for x in summary.values()) + len(fp_bad)
    (OUT / f'{PREFIX}check.json').write_text(json.dumps({'summary': summary, 'fp_mismatch': fp_bad, 'detail': results}, ensure_ascii=False, indent=1), encoding='utf-8')
    for k, v in summary.items():
        print(k, v, 'cards', results[k]['cards'], 'ledger', results[k]['ledger_rows'], 'reasons', results[k]['reason_rows'], 'fp', results[k]['fp'])
    print('fp_mismatch', fp_bad)
    print('TOTAL_ISSUES', total)
    return 0 if total == 0 else 1


if __name__ == '__main__':
    sys.exit(run())
