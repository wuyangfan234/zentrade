#!/usr/bin/env python3
"""Export the strict open-source core edition of ZenTrade.

The private full edition contains third-party strategy ports and data whose licences are not OSI
approved (or not verified). This script produces a source tree that contains only redistributable
content, following ``scripts/core_manifest.toml``:

* files from ``git archive <rev>`` minus the ``[exclude]`` paths;
* ``[replace]`` files swapped for the core-edition versions under ``scripts/core/``;
* a ``CORE_EXPORT.json`` manifest (source revision, excluded paths, sha256 of every file);
* a final scan: any ``[forbid]`` pattern outside the ``allow`` list fails the export.

Usage::

    python3 scripts/export_core.py --out /path/to/zentrade-core [--rev HEAD] [--force]
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "core_manifest.toml"
REPLACEMENTS = ROOT / "scripts" / "core"
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".toml", ".yml", ".yaml", ".txt", ".sh", ".ps1",
                 ".cfg", ".ini", ".html", ".css", ""}


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def _excluded(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/"):
            if path.startswith(pat):
                return True
        elif fnmatch.fnmatchcase(path, pat):
            return True
    return False


def _drop_extra(text: str, name: str) -> str:
    """Remove ``name = [ ... ]`` (and the comment block right above it) from [project.optional-dependencies]."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(name)}\s*=\s*\[", ln)), None)
    if start is None:
        raise SystemExit(f"pyproject: extra {name!r} not found")
    end = start
    while not lines[end].rstrip().endswith("]"):
        end += 1
    head = start
    while head > 0 and lines[head - 1].lstrip().startswith("#"):
        head -= 1
    if head > 0 and not lines[head - 1].strip():
        head -= 1
    return "".join(lines[:head] + lines[end + 1:])


def _lock_versions(path: Path) -> dict[str, set[str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for p in data.get("package", []):
        out.setdefault(p["name"], set()).add(p.get("version", ""))
    return out


def _assert_versions_unchanged(full_lock: Path, core_lock: Path) -> None:
    """核心版重锁只能删除包 (被移除的可选依赖独占的), 不能新增或改动任何版本。"""
    full, core = _lock_versions(full_lock), _lock_versions(core_lock)
    changed = sorted(n for n, v in core.items() if n not in full or not v <= full[n])
    if changed:
        raise SystemExit(f"core relock changed package versions: {changed[:10]}")


def _apply_pyproject(out: Path, cfg: dict, kept: dict[str, str]) -> None:
    if not cfg:
        return
    path = out / "backend" / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    for extra in cfg.get("drop_extras", []):
        text = _drop_extra(text, extra)
    if cfg.get("license"):
        text = re.sub(r'^license = \{ text = "[^"]*" \}$', f'license = {{ text = "{cfg["license"]}" }}', text, flags=re.M)
    if cfg.get("license_files"):
        files = ", ".join(f'"{f}"' for f in cfg["license_files"])
        text = re.sub(r"^license-files = \[.*\]$", f"license-files = [{files}]", text, flags=re.M)
    path.write_text(text, encoding="utf-8")
    # 比对基线必须是同一 rev 归档出来的锁文件, 不能读工作区 (工作区可能有未提交改动, 或导出的是旧 rev)
    baseline = out / "backend" / ".uv.lock.source"
    shutil.copyfile(out / "backend" / "uv.lock", baseline)
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_DEFAULT_INDEX", "UV_INDEX_URL"}}
    env["UV_PROJECT_ENVIRONMENT"] = str(out / "backend" / ".venv-lock")
    # 先离线重锁 (本机缓存足够时最快且不联网); 缓存不全 (如全新 CI 机器) 再联网。已有锁文件时 uv 保留原有版本。
    res = subprocess.run(["uv", "lock", "--offline", "--python", "3.11"], cwd=out / "backend", env=env,
                         capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["uv", "lock", "--python", "3.11"], cwd=out / "backend", env=env, check=True,
                       capture_output=True, text=True)
    try:
        _assert_versions_unchanged(baseline, out / "backend" / "uv.lock")
    finally:
        baseline.unlink(missing_ok=True)
        shutil.rmtree(out / "backend" / ".venv-lock", ignore_errors=True)
    for rel in ("backend/pyproject.toml", "backend/uv.lock"):
        kept[rel] = hashlib.sha256((out / rel).read_bytes()).hexdigest()


def export(out: Path, rev: str, force: bool) -> dict:
    manifest = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    excludes: list[str] = manifest["exclude"]["paths"]
    replaces: list[str] = manifest.get("replace", {}).get("files", [])
    forbid = [re.compile(p) for p in manifest.get("forbid", {}).get("patterns", [])]
    allow = set(manifest.get("forbid", {}).get("allow", []))

    if out.exists():
        if not force:
            raise SystemExit(f"{out} exists; pass --force to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    commit = _git("rev-parse", rev).decode().strip()
    archive = _git("archive", "--format=tar", commit)
    kept: dict[str, str] = {}
    dropped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if _excluded(member.name, excludes):
                dropped.append(member.name)
                continue
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
            target = out / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(member.mode & 0o777 or 0o644)
            kept[member.name] = hashlib.sha256(data).hexdigest()

    for rel in replaces:
        src = REPLACEMENTS / rel
        if not src.is_file():
            raise SystemExit(f"missing core replacement: scripts/core/{rel}")
        data = src.read_bytes()
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
        kept[rel] = hashlib.sha256(data).hexdigest()

    for edit in manifest.get("edit", []):
        path = out / edit["file"]
        text = path.read_text(encoding="utf-8")
        n = text.count(edit["old"])
        if n != 1:
            raise SystemExit(f"core edit on {edit['file']}: expected 1 match, found {n}")
        path.write_text(text.replace(edit["old"], edit["new"]), encoding="utf-8")
        kept[edit["file"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    _apply_pyproject(out, manifest.get("pyproject", {}), kept)

    violations: list[str] = []
    for rel in sorted(kept):
        if rel in allow or Path(rel).suffix not in TEXT_SUFFIXES:
            continue
        text = (out / rel).read_text(encoding="utf-8", errors="ignore")
        for rx in forbid:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                violations.append(f"{rel}:{line}: forbidden pattern {rx.pattern!r}")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        raise SystemExit(f"core export failed: {len(violations)} forbidden reference(s)")

    for rel in excludes:
        if not rel.endswith("/") and "*" not in rel and rel not in dropped and not rel.startswith("scripts/core"):
            # a stale manifest entry is harmless but worth surfacing
            print(f"note: exclude entry matched nothing: {rel}", file=sys.stderr)

    report = {
        "edition": "core",
        "source_revision": commit,
        "excluded": sorted(dropped),
        "replaced": replaces,
        "files": len(kept),
        "sha256": dict(sorted(kept.items())),
    }
    (out / "CORE_EXPORT.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # 文件哈希会被密钥扫描器当成泛型 API key: 在核心版里声明白名单 (只放行这一个文件)
    (out / ".gitleaks.toml").write_text(
        '[extend]\nuseDefault = true\n\n[allowlist]\ndescription = "file hashes in the export manifest"\n'
        'paths = [\'\'\'^CORE_EXPORT\\.json$\'\'\']\n', encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export the strict open-source core edition of ZenTrade.")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    report = export(args.out.resolve(), args.rev, args.force)
    print(f"core edition exported: {report['files']} files, {len(report['excluded'])} excluded, "
          f"source {report['source_revision'][:7]} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
