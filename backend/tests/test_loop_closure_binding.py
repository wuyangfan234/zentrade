"""循环里定义的闭包必须显式绑定循环变量 (ruff B023)。

10-10 全量 review: backtest/engine.py 的 _scheduled_reason / rank_key 与 decision_scan.py 的 _watch
都在循环体里定义、当轮立即调用, 现阶段结果正确, 但只要有人把调用挪到循环之后就会静默读到最后一轮的值。
修法是默认参数绑定; 本测试锁住生产代码 app/ 零 B023。行为不变由修前/修后同输入回测对比确认。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def test_app_has_no_unbound_loop_variable_closures():
    ruff = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    if not Path(ruff).exists():
        pytest.skip("ruff 未安装 (uv sync --extra dev)")
    proc = subprocess.run(
        [ruff, "check", "--select", "B023", "--output-format", "concise", "--no-cache", "app"],
        cwd=BACKEND, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
