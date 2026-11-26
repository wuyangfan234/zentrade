#!/usr/bin/env python3
"""单文件 HTML 参考稿自检: 按状态截图 + JS 报错 / 对比度 / 折行 / 截断 / 整句与禁用词 / 横向溢出。

用法:
  python3 mockup_check.py page.html shots/ find= find_light=?light=1 dr=?open=dr-src \
      [--full find,find_light] [--extra counts.js] [--size 1440x900]
状态参数 name=query (query 可为空)。--full 指定整页截图的状态。
--extra: 文件内容是一个返回对象的箭头函数 `() => ({...})`, 每个状态都执行并打印, 用于页面专属计数断言。
约定: 选项卡视图 `.view.on` 可见, 抽屉 `.drawer.on` 可见; 原文引用/公式用 `.quote/.dsl/.mono` 免整句检查。
退出码: 任一状态有问题 = 1。
"""
import glob, json, os, sys
from playwright.sync_api import sync_playwright

CHECK_JS = r'''() => {
 const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
   return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
     && el.closest('.view:not(.on)') === null
     && (el.closest('.drawer') === null || el.closest('.drawer').classList.contains('on')); };
 const cv = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
 const rgba = c => { cv.clearRect(0,0,1,1); cv.fillStyle = '#000'; cv.fillStyle = c; cv.fillRect(0,0,1,1);
   const d = cv.getImageData(0,0,1,1).data; return [d[0], d[1], d[2], d[3] / 255]; };
 const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
   return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
 const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
 const bgOf = el => { const st = []; let e = el;
   while (e) { const b = rgba(getComputedStyle(e).backgroundColor); if (b[3] > 0) st.push(b); if (b[3] >= 1) break; e = e.parentElement; }
   let c = rgba(getComputedStyle(document.body).backgroundColor).slice(0, 3);
   for (let i = st.length - 1; i >= 0; i--) { const b = st[i]; c = [0,1,2].map(k => b[k] * b[3] + c[k] * (1 - b[3])); }
   return c; };
 const opOf = el => { let o = 1, e = el; while (e && e !== document.body) { o *= parseFloat(getComputedStyle(e).opacity); e = e.parentElement; } return o; };
 const out = {low: [], svgLow: [], wrap: [], clip: [], sent: []};
 document.querySelectorAll('body *').forEach(el => {
   if (!vis(el) || el.closest('svg')) return;
   const own = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim()).map(n => n.textContent.trim()).join('');
   if (!own) return;
   const fg = rgba(getComputedStyle(el).color), bg = bgOf(el), a = fg[3] * opOf(el);
   const f = [0,1,2].map(k => fg[k] * a + bg[k] * (1 - a));
   const fs = parseFloat(getComputedStyle(el).fontSize), cr = ratio(f, bg);
   if (cr < (fs >= 18 ? 3 : 4.5)) out.low.push([own.slice(0, 30), +cr.toFixed(2), fs]);
 });
 document.querySelectorAll('svg text').forEach(t => { if (!vis(t)) return;
   const cr = ratio(rgba(getComputedStyle(t).fill), bgOf(t.closest('.card') || document.body));
   if (cr < 4.5) out.svgLow.push([t.textContent, +cr.toFixed(2)]); });
 document.querySelectorAll('.btn,.chip,.pill,.tabs button,.seg button').forEach(b => {
   if (vis(b) && b.getBoundingClientRect().height > 34) out.wrap.push(b.textContent.trim().slice(0, 20)); });
 document.querySelectorAll('.v,.num,.big,.score,td,th').forEach(e => {
   if (vis(e) && e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflow !== 'visible') out.clip.push(e.textContent.trim().slice(0, 20)); });
 const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
 while ((n = w.nextNode())) { const s = n.textContent.trim(), el = n.parentElement;
   if (!s || !vis(el) || el.closest('script,style,.quote,.dsl,.mono,input,textarea')) continue;
   if (/[，。；！？]/.test(s) || /(示例|演示|undefined|NaN|null|not_selected)/.test(s)) out.sent.push(s.slice(0, 40)); }
 out.hscroll = document.documentElement.scrollWidth > innerWidth + 1;
 return out;
}'''


def launch(p):
    try:
        return p.chromium.launch()
    except Exception:
        cands = sorted(glob.glob(os.path.expanduser(
            '~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell')))
        if not cands:
            raise
        return p.chromium.launch(executable_path=cands[-1])


def main(argv):
    args, full, extra, size = [], set(), None, (1440, 900)
    it = iter(argv)
    for a in it:
        if a == '--full': full = set(next(it).split(','))
        elif a == '--extra': extra = open(next(it), encoding='utf-8').read()
        elif a == '--size': size = tuple(int(x) for x in next(it).split('x'))
        else: args.append(a)
    page_file, shots, states = args[0], args[1], [s.split('=', 1) for s in args[2:]] or [['default', '']]
    os.makedirs(shots, exist_ok=True)
    url = 'file://' + os.path.abspath(page_file)
    bad = False
    with sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={'width': size[0], 'height': size[1]})
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        for name, q in states:
            pg.goto(url + q); pg.wait_for_timeout(250)
            r = pg.evaluate(CHECK_JS); r['errs'] = list(errs); errs.clear()
            if extra: r['extra'] = pg.evaluate(extra)
            pg.screenshot(path=os.path.join(shots, f'{name}.png'), full_page=name in full)
            issues = {k: r[k] for k in ('errs', 'low', 'svgLow', 'wrap', 'clip', 'sent') if r[k]}
            if r['hscroll']: issues['hscroll'] = True
            bad |= bool(issues)
            print(json.dumps({'state': name, 'ok': not issues, **issues, **({'extra': r['extra']} if extra else {})}, ensure_ascii=False))
        b.close()
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
