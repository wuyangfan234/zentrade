"""VERSION 文件必须与 app.__version__ 一致。"""

from pathlib import Path

import pytest

from app import __version__

VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def test_version_file_matches_dunder_version() -> None:
    if not VERSION_FILE.exists():
        pytest.skip("此布局没有仓库根 VERSION（例如容器内只同步了 backend）")
    raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    # /api/version 回退分支原样返回 VERSION 内容，因此保留 v 前缀写法
    assert raw.startswith("v"), f"VERSION 应写成 v<版本号>，实际 {raw!r}"
    assert raw[1:] == __version__.strip()


ROOT = Path(__file__).resolve().parents[2]


def _need(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"此布局没有 {path.name}")
    return path


def test_pyproject_and_lock_match_dunder_version() -> None:
    import re
    py = _need(ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^name = "zentrade-backend"$', py, re.M), "pyproject 包名应为 zentrade-backend"
    assert re.search(rf'^version = "{re.escape(__version__)}"$', py, re.M), "pyproject version 与 __version__ 不一致"
    lock = _need(ROOT / "backend" / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "zentrade-backend"\nversion = "{__version__}"\nsource = {{ editable = "." }}' in lock


def test_frontend_package_version_matches() -> None:
    import json
    pkg = json.loads(_need(ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == __version__


def test_changelog_has_current_version_and_empty_unreleased() -> None:
    import re
    cl = _need(ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(__version__)}\] - ", cl, re.M), "CHANGELOG 缺当前版本小节"
    m = re.search(r"^## \[未发布\]\n(.*?)(?=^## \[)", cl, re.M | re.S)
    assert m is not None
    assert not re.search(r"^\s*[-*] ", m.group(1), re.M), "[未发布] 应已归档到版本小节"
