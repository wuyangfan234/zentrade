#!/usr/bin/env python3
"""把 docs/prototype/src 拼成两份单文件 HTML (内联 CSS / JS, 双击可开)。

用法: python3 docs/prototype/tools/build.py
输出: docs/prototype/opp-next.html, docs/prototype/factor-next.html
只用标准库, 兼容 Python 3.9。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PAGES = [
    ("opp-next.html", "机会 · 新版原型", "opp", "opp.css", "opp.js"),
    ("factor-next.html", "因子 · 新版原型", "factor", "factor.css", "factor.js"),
]


def read(name):
    return (SRC / name).read_text(encoding="utf-8")


def main():
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python >= 3.9")
    shell = read("shell.html")
    shared_css, shared_js = read("shared.css"), read("shared.js")
    built = []
    for out, title, page, css, js in PAGES:
        if not (SRC / js).exists():
            continue
        html = (shell.replace("__TITLE__", title).replace("__PAGE__", page)
                .replace("__CSS__", shared_css + "\n" + read(css))
                .replace("__SHARED__", shared_js).replace("__PAGEJS__", read(js)))
        (ROOT / out).write_text(html, encoding="utf-8")
        built.append(out)
    print("built: " + ", ".join(built))


if __name__ == "__main__":
    main()
