"""可选组件探测。

开源核心版不含部分组件 (第三方移植、私有脚本)。调用方用 :func:`load` 取可选模块: 模块文件不存在 → None;
模块存在但导入出错 → 照常抛出 (不吞真实 bug)。
"""
from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType


def available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def load(name: str) -> ModuleType | None:
    if not available(name):
        return None
    return importlib.import_module(name)
