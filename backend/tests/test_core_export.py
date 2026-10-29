"""开源核心版导出: 清单生效、许可收敛、禁止标记、降级默认值。只在私有完整版运行 (核心版不含导出脚本)。"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "export_core.py"

pytestmark = [
    pytest.mark.skipif(not SCRIPT.exists(), reason="export script is full-edition only"),
    pytest.mark.skipif(shutil.which("git") is None or shutil.which("uv") is None, reason="needs git and uv"),
]


def _load():
    spec = importlib.util.spec_from_file_location("export_core", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    out = tmp_path_factory.mktemp("core") / "tree"
    report = _load().export(out, "HEAD", force=False)
    return out, report


def test_export_excludes_restricted_components(exported):
    out, report = exported
    builtin = out / "backend" / "app" / "strategy" / "builtin"
    assert not list(builtin.glob("tv_*.py"))
    for gone in ("backend/app/backtest/tv_kernels.py", "backend/app/vendor/oasis_phantom",
                 "backend/app/services/phantom_4h.py", "backend/app/services/phantom_4h_source.py",
                 "backend/app/services/phantom_4h_gate.py", "backend/app/strategy/builtin/ibs_mean_reversion.py",
                 "backend/config/swing_rule_history.json", "backend/tests/tv_reference", "LICENSES",
                 "scripts/export_core.py", "scripts/core_manifest.toml"):
        assert not (out / gone).exists(), gone
    # 原创内置策略与核心模块保留
    for kept in ("backend/app/strategy/builtin/macd_golden.py", "backend/app/strategy/builtin/momentum_rsi2_pullback.py",
                 "backend/app/opportunity/direction_gate.py", "backend/app/services/kline_4h.py", "LICENSE",
                 "LICENSE.upstream"):
        assert (out / kept).exists(), kept
    assert report["source_revision"] and report["excluded"]


def test_export_licensing_is_mit_only(exported):
    out, _ = exported
    py = (out / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { text = "MIT" }' in py and "LicenseRef" not in py
    assert "backtest = [" not in py and "vectorbt" not in py
    assert 'name = "vectorbt"' not in (out / "backend" / "uv.lock").read_text(encoding="utf-8")
    assert 'licenses="MIT"' in (out / "Dockerfile").read_text(encoding="utf-8")
    for rel in ("NOTICE.md", "THIRD_PARTY_NOTICES.md"):
        top = (out / rel).read_bytes()
        assert top == (out / "backend" / rel).read_bytes()          # 根与 backend 副本一致 (check_docs 要求)
        text = top.decode("utf-8")
        assert "核心版" in text and "CC BY-NC-SA" not in text and "GPL" not in text
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "开源核心版" in readme and "CC BY-NC-SA" not in readme


def test_export_manifest_records_hashes(exported):
    out, report = exported
    data = json.loads((out / "CORE_EXPORT.json").read_text(encoding="utf-8"))
    assert data["edition"] == "core" and data["files"] == len(data["sha256"]) == report["files"]
    sample = "backend/app/opportunity/direction_gate.py"
    import hashlib
    assert data["sha256"][sample] == hashlib.sha256((out / sample).read_bytes()).hexdigest()


def test_core_tree_imports_and_degrades(exported):
    """核心版独立导入: 方向门回落到内置日线门, 默认 3+3 只含存在的策略, 规则目录不列 Phantom。"""
    out, _ = exported
    code = (
        "import json, tempfile; from pathlib import Path\n"
        "from app.opportunity import direction_gate as dg\n"
        "from app.services import swing_desk as sd, watchlist_table as wt\n"
        "from app.api import opportunities as opp\n"
        "from app import optional\n"
        "d = Path(tempfile.mkdtemp())\n"
        "print(json.dumps({'gate': dg.active(d).id, 'defaults': sd.strategies_from_ruleset(d),\n"
        "  'catalog': [r['id'] for r in wt.rule_catalog(None)],\n"
        "  'phantom': optional.available('app.services.phantom_4h_source')}))\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=out / "backend", capture_output=True, text=True,
                         env={"PYTHONPATH": str(out / "backend"), "PATH": "/usr/bin:/bin", "HOME": str(out)})
    assert res.returncode == 0, res.stderr[-2000:]
    got = json.loads(res.stdout.strip().splitlines()[-1])
    assert got["gate"] == "daily_ma50" and got["phantom"] is False
    assert got["catalog"] == ["breakout", "pullback"]
    builtin = {p.stem for p in (out / "backend" / "app" / "strategy" / "builtin").glob("*.py")}
    assert got["defaults"][0] and set(got["defaults"][0]) <= builtin
    assert set(got["defaults"][1]) <= builtin


def test_forbidden_reference_fails_export(tmp_path, monkeypatch):
    mod = _load()
    manifest = tmp_path / "m.toml"
    manifest.write_text(
        '[exclude]\npaths = ["frontend/", "backend/", "docs/", "screenshots/", "scripts/", ".github/"]\n'
        '[forbid]\npatterns = ["ZenTrade"]\nallow = []\n', encoding="utf-8")
    monkeypatch.setattr(mod, "MANIFEST", manifest)
    with pytest.raises(SystemExit, match="forbidden"):
        mod.export(tmp_path / "out", "HEAD", force=False)
