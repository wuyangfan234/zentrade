"""A 股同花顺概念/行业预设已退役 (美股系统无此数据源)。

回归 (2026-09-29): 数据页「扩展概念 / 扩展行业」两张卡来自上游作者的 A 股接口
(shy313.com 同花顺概念/行业导出, 代码形如 600000.SH), 美股标的永远关联不上。
删除后契约:
- 监控中心个股通知的 ext 标签默认不再指向同花顺字段 (未配置 = 不展示标签);
- 前后端代码不再引用这两个预设 id 与上游 A 股接口。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services import preferences

_REPO = Path(__file__).resolve().parents[2]
_RETIRED = re.compile(r"ext_gn_ths|ext_hy_ths|shy313\.com")


def test_monitor_ext_fields_default_to_none(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {})

    assert preferences.get_monitor_ext_fields() == {"concept": None, "industry": None}


def test_user_configured_monitor_ext_fields_still_respected(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {
        "monitor_ext_fields": {"concept": "my_tags.theme", "industry": None},
    })

    assert preferences.get_monitor_ext_fields() == {
        "concept": {"field": "my_tags.theme"},
        "industry": None,
    }


def test_no_code_references_retired_ths_presets():
    roots = [_REPO / "backend" / "app", _REPO / "frontend" / "src"]
    hits = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or path.name.startswith("."):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _RETIRED.search(line):
                    hits.append(f"{path.relative_to(_REPO)}:{lineno}")
    assert hits == []
