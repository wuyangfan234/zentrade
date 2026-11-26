#!/usr/bin/env python3
"""参考稿自检 + 截图 (真浏览器渲染, Playwright Chromium headless)。

在 tools/mockup_check.py (zentrade-dev skill 原脚本: JS 报错 / 对比度 / 折行 / 数字截断 / 整句与禁用词 / 横向溢出)
之上再加三类断言:
  counts  页面上每个 [data-count=K] 的数字 = DOM 里 [data-item~=K] 的个数; [data-shown] = 可见卡数
  overlap flex / grid 容器内可见兄弟元素两两不相交 (同行文字互压)
  spill   .card / .cc / .drawer 内后代右缘不越出容器 (横滑表格容器内除外)
  textover 非 inline 元素的文字不溢出自身盒子 (溢出会压到相邻格子)
另外两页的 regime 状态条文本必须逐字相同 (同一状态、同一时间戳)。

用法 (需要带 playwright 的 Python):
  python tools/verify.py            # 自检全部状态, 截图写 shots/ 与 shots/extra/
退出码: 任一状态有问题 = 1。
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mockup_check import CHECK_JS, launch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "shots"
DESK = (1440, 900, False)
MOBILE = (390, 844, True)

# (截图名, 文件, 查询串, 视口, 是否必交截图)
STATES = [
    ("opp-desktop", "opp-next.html", "", DESK, True),
    ("opp-mobile", "opp-next.html", "", MOBILE, True),
    ("factor-desktop", "factor-next.html", "?x=all", DESK, True),
    ("factor-mobile", "factor-next.html", "?x=all", MOBILE, True),
    ("opp-sparse", "opp-next.html", "?d=sparse", DESK, True),
    ("factor-sparse", "factor-next.html", "?d=sparse&x=all", DESK, True),
    ("opp-attr-expanded", "opp-next.html", "?c=c4", DESK, False),
    ("opp-filter-intraday", "opp-next.html", "?f=i", DESK, False),
    ("opp-sparse-mobile", "opp-next.html", "?d=sparse", MOBILE, False),
    ("factor-card-drawer", "factor-next.html", "?x=all&open=r1", DESK, False),
    ("factor-drawer-mobile", "factor-next.html", "?x=all&open=c1", MOBILE, False),
    ("factor-sparse-mobile", "factor-next.html", "?d=sparse&x=all", MOBILE, False),
    ("factor-collapsed", "factor-next.html", "", DESK, False),
]

GEO_JS = r'''() => {
 const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
   return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.closest('[hidden]')
     && (el.closest('.drawer') === null || el.closest('.drawer').classList.contains('on')); };
 const out = {counts: [], overlap: [], spill: [], regime: ''};
 document.querySelectorAll('[data-count]').forEach(el => {
   const k = el.dataset.count, want = document.querySelectorAll(`[data-item~="${k}"]`).length;
   const got = parseInt(el.textContent, 10);
   if (got !== want) out.counts.push([k, got, want]);
 });
 document.querySelectorAll('[data-shown]').forEach(el => {
   const want = [...document.querySelectorAll('[data-item~="card"]')].filter(vis).length;
   if (parseInt(el.textContent, 10) !== want) out.counts.push(['shown', el.textContent, want]);
 });
 const name = el => (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : el.tagName) + ':' + (el.textContent || '').trim().slice(0, 16);
 document.querySelectorAll('.page *, .drawer.on *').forEach(p => {
   if (!vis(p)) return; const d = getComputedStyle(p).display; if (!/flex|grid/.test(d)) return;
   const kids = [...p.children].filter(k => vis(k) && !/absolute|fixed/.test(getComputedStyle(k).position));
   for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
     const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
     const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left), iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
     if (ix > 1 && iy > 1) out.overlap.push([name(kids[i]), name(kids[j])]);
   }
 });
 document.querySelectorAll('.card, .cc, .drawer.on').forEach(c => {
   if (!vis(c)) return; const cr = c.getBoundingClientRect();
   c.querySelectorAll('*').forEach(e => {
     if (!vis(e) || e.closest('svg') && e.tagName !== 'svg') return;
     let s = e.parentElement, scroll = false;
     while (s && s !== c) { if (/auto|scroll/.test(getComputedStyle(s).overflowX)) { scroll = true; break; } s = s.parentElement; }
     if (scroll) return;
     if (e.getBoundingClientRect().right > cr.right + 1) out.spill.push(name(e));
   });
 });
 // 文字溢出自身盒子 (会压到旁边的格子): 非 inline 且 overflow 可见的元素 scrollWidth > clientWidth
 out.textover = [];
 document.querySelectorAll('.page *, .drawer.on *').forEach(e => {
   if (!vis(e) || e.closest('svg')) return; const s = getComputedStyle(e);
   if (s.display === 'inline' || s.overflowX !== 'visible' || /^(TABLE|TBODY|THEAD|TR)$/.test(e.tagName)) return;
   if (e.scrollWidth > e.clientWidth + 1) out.textover.push(name(e));
 });
 const rg = document.querySelector('[data-regime]');
 out.regime = rg ? rg.innerText.replace(/\s+/g, ' ').trim() : '';
 out.demo = !!document.querySelector('[data-demo]') && /DEMO · 虚拟数据/.test(document.body.innerText);
 return out;
}'''


def main():
    SHOTS.mkdir(exist_ok=True)
    (SHOTS / "extra").mkdir(exist_ok=True)
    bad, regimes = False, {}
    with sync_playwright() as p:
        b = launch(p)
        for name, page_file, q, (w, h, mob), required in STATES:
            if not (ROOT / page_file).exists():
                print(json.dumps({"state": name, "ok": False, "missing": page_file}, ensure_ascii=False))
                bad = True
                continue
            ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if mob else 1,
                                is_mobile=mob, has_touch=mob)
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.goto("file://" + str(ROOT / page_file) + q)
            pg.wait_for_timeout(300)
            r = pg.evaluate(CHECK_JS)
            g = pg.evaluate(GEO_JS)
            drawer_open = pg.evaluate("!!document.querySelector('.drawer.on')")
            out = SHOTS / (name + ".png") if required else SHOTS / "extra" / (name + ".png")
            if drawer_open:
                ph = pg.evaluate("document.querySelector('.drawer.on').scrollHeight")
                pg.set_viewport_size({"width": w, "height": max(h, ph)})
                pg.screenshot(path=str(out))
            else:
                pg.screenshot(path=str(out), full_page=True)
            issues = {k: r[k] for k in ("low", "svgLow", "wrap", "clip", "sent") if r[k]}
            if errs:
                issues["errs"] = errs
            if r["hscroll"]:
                issues["hscroll"] = True
            for k in ("counts", "overlap", "spill", "textover"):
                if g[k]:
                    issues[k] = g[k]
            if not g["demo"]:
                issues["demo"] = "缺 DEMO · 虚拟数据 标注"
            regimes.setdefault(name.split("-")[0], set()).add(g["regime"])
            bad |= bool(issues)
            print(json.dumps({"state": name, "viewport": "%dx%d" % (w, h), "shot": str(out.relative_to(ROOT)),
                              "ok": not issues, **issues}, ensure_ascii=False))
            ctx.close()
        b.close()
    allreg = set().union(*regimes.values())
    same = len(allreg) == 1
    print(json.dumps({"state": "regime-consistency", "ok": same, "variants": len(allreg),
                      "text": sorted(allreg)[0][:120] if same else sorted(allreg)}, ensure_ascii=False))
    bad |= not same
    print("RESULT", "FAIL" if bad else "PASS (all checks 0 issues)")
    return 1 if bad else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
