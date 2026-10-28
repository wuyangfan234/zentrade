#!/usr/bin/env python3
"""Check local Markdown links and image sources without network access."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)]+)\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
SKIP = ("http://", "https://", "mailto:", "tel:", "data:", "#")


def markdown_files() -> list[Path]:
    files = list(ROOT.glob("*.md"))
    for folder in (ROOT / "docs", ROOT / ".github"):
        if folder.exists():
            files.extend(folder.rglob("*.md"))
    return sorted(set(files))


def local_target(raw: str) -> str | None:
    target = raw.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    if not target or target.startswith(SKIP):
        return None
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return target or None


def main() -> int:
    broken: list[tuple[Path, int, str]] = []
    checked = 0
    files = markdown_files()
    for doc in files:
        text = doc.read_text(encoding="utf-8")
        targets: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            targets.extend((lineno, match.group(1)) for match in LINK.finditer(line))
            targets.extend((lineno, match.group(1)) for match in HTML_IMAGE.finditer(line))
        for lineno, raw in targets:
            target = local_target(raw)
            if target is None:
                continue
            checked += 1
            candidate = (doc.parent / target).resolve()
            if ROOT.resolve() not in candidate.parents and candidate != ROOT.resolve():
                broken.append((doc, lineno, raw))
            elif not candidate.exists():
                broken.append((doc, lineno, raw))
    for doc, lineno, raw in broken:
        print(f"{doc.relative_to(ROOT)}:{lineno}: broken local target: {raw}")
    if broken:
        print(f"Documentation check failed: {len(broken)} broken target(s).", file=sys.stderr)
        return 1

    legal_files = ["LICENSE", "LICENSE.upstream", "NOTICE.md", "THIRD_PARTY_NOTICES.md"]
    legal_files.extend(str(path.relative_to(ROOT)) for path in sorted((ROOT / "LICENSES").glob("*.txt")))
    mismatched = [rel for rel in legal_files if (ROOT / rel).read_bytes() != (ROOT / "backend" / rel).read_bytes()]
    if mismatched:
        for rel in mismatched:
            print(f"Legal copy out of sync: {rel} != backend/{rel}", file=sys.stderr)
        return 1

    print(f"Documentation check passed: {len(files)} Markdown files, {checked} local targets; {len(legal_files)} legal copies synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
