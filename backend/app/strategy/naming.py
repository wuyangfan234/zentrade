"""策略显示名的轻量规则 (API 层与发布服务共用, 不引入回测 / 挖掘依赖)。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MINED_STRATEGY_PREFIX = "mined_factor_"


def is_renamable(meta: Mapping[str, Any], source: str | None) -> bool:
    """挖掘发布的策略 (custom 目录、mined_factor_ 前缀、带来源运行) 可在策略页改名。"""
    return (
        source == "custom"
        and str(meta.get("id", "")).startswith(MINED_STRATEGY_PREFIX)
        and bool(meta.get("origin_run_id"))
    )
